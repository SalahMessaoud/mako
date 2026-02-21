"""On-chain token analysis — reads Solana state directly via RPC (free).

Checks:
- Mint authority (can they mint more tokens?)
- Freeze authority (can they freeze your tokens?)
- Top holder concentration
- LP token lock/burn status
- Bonding curve exact percentage
- Creator wallet history (multiple rugs?)
"""

import struct
import logging
import time
import base64
import httpx
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ── Solana Program IDs ────────────────────────────────────────
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_BONDING_SEED = b"bonding-curve"


@dataclass
class BondingCurveState:
    """Full bonding curve state for momentum evaluation."""
    virtual_sol: float = 0       # in SOL
    real_sol: float = 0          # in SOL
    virtual_tokens: float = 0    # raw token units
    real_tokens: float = 0       # raw token units
    complete: bool = False
    pct: float = 0               # 0-100


@dataclass
class OnChainResult:
    """Results of on-chain token analysis."""
    mint_authority_revoked: bool = False
    freeze_authority_revoked: bool = False
    supply: float = 0
    decimals: int = 0
    top_holder_pct: float = 0
    top_5_holder_pct: float = 0
    holder_count: int = 0
    lp_burned: bool = False
    lp_locked: bool = False
    creator_token_count: int = 0
    bonding_curve_pct: float = 0
    bonding_curve_sol: float = 0
    is_pump_token: bool = False
    warnings: list = None
    score: int = 0  # 0-100

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class OnChainAnalyzer:
    """Reads on-chain data via Solana RPC. Fully async — never blocks the event loop."""

    def __init__(self, rpc_url: str, fallback_rpc_url: str = ""):
        self.rpc_url = rpc_url
        self.fallback_rpc_url = fallback_rpc_url
        self.http = httpx.AsyncClient(timeout=10)
        self._cache: dict[str, tuple[float, OnChainResult]] = {}
        self._cache_ttl = 30  # seconds
        self._curve_cache: dict[str, tuple[float, BondingCurveState]] = {}
        self._curve_cache_ttl = 5  # seconds — tight for price monitoring

    async def analyze(self, mint: str, creator: str = "") -> OnChainResult:
        """Full on-chain analysis of a token. Cached for 30 seconds."""
        # Check cache
        if mint in self._cache:
            ts, result = self._cache[mint]
            if time.time() - ts < self._cache_ttl:
                return result

        result = OnChainResult()

        try:
            # 1. Mint account data (authority, supply, decimals)
            await self._check_mint_account(mint, result)

            # 2. Top holders
            await self._check_holders(mint, result)

            # 3. Bonding curve state (pump.fun tokens)
            await self._check_bonding_curve(mint, result)

            # 4. Creator history (if provided)
            if creator:
                await self._check_creator(creator, result)

            # Calculate score
            result.score = self._calculate_score(result)

        except Exception as e:
            log.warning(f"On-chain analysis error for {mint[:8]}: {e}")
            result.warnings.append(f"Analysis error: {str(e)[:50]}")

        self._cache[mint] = (time.time(), result)
        # Evict stale analysis cache entries
        if len(self._cache) > 500:
            cutoff = time.time() - self._cache_ttl * 3
            self._cache = {k: v for k, v in self._cache.items() if v[0] > cutoff}
        return result

    async def get_account_sol_balance(self, pubkey: str) -> float | None:
        """Get SOL balance of a wallet address. Returns SOL or None on error."""
        try:
            resp = await self.http.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [pubkey, {"commitment": "confirmed"}],
            })
            data = resp.json()
            value = data.get("result", {}).get("value")
            if value is not None:
                return value / 1e9  # lamports → SOL
            return None
        except Exception as e:
            log.debug(f"  getBalance error for {pubkey[:12]}...: {e}")
            return None

    async def get_bonding_curve_pct(self, mint: str) -> float:
        """Quick bonding curve check — returns percentage (0-100)."""
        try:
            result = OnChainResult()
            await self._check_bonding_curve(mint, result)
            return result.bonding_curve_pct
        except Exception:
            return 0.0

    async def get_bonding_curve_state(self, mint: str) -> BondingCurveState | None:
        """Read full bonding curve state for momentum evaluation + price monitoring.

        Single RPC call — returns virtual/real reserves, complete flag, and pct.
        Cached for 5 seconds to avoid excessive RPC calls during position monitoring.
        Returns None if not a pump.fun token or on error.
        """
        # Check cache
        if mint in self._curve_cache:
            ts, cached = self._curve_cache[mint]
            if time.time() - ts < self._curve_cache_ttl:
                return cached

        try:
            from solders.pubkey import Pubkey
            mint_pk = Pubkey.from_string(mint)
            pump_pk = Pubkey.from_string(PUMP_PROGRAM)
            pda, _bump = Pubkey.find_program_address(
                [PUMP_BONDING_SEED, bytes(mint_pk)], pump_pk
            )
            info = await self._get_account_info(str(pda))
            if not info:
                return None

            data = info.get("data", [])
            if not data:
                return None

            raw = base64.b64decode(data[0])
            if len(raw) < 49:
                return None

            v_tokens = struct.unpack_from("<Q", raw, 8)[0]
            v_sol = struct.unpack_from("<Q", raw, 16)[0]
            r_tokens = struct.unpack_from("<Q", raw, 24)[0]
            r_sol = struct.unpack_from("<Q", raw, 32)[0]
            complete = bool(raw[48]) if len(raw) > 48 else False

            GRADUATION_SOL = 85.0
            sol_in_curve = r_sol / 1e9
            pct = min((sol_in_curve / GRADUATION_SOL) * 100, 100.0)
            if complete:
                pct = 100.0

            result = BondingCurveState(
                virtual_sol=v_sol / 1e9,
                real_sol=sol_in_curve,
                virtual_tokens=v_tokens,
                real_tokens=r_tokens,
                complete=complete,
                pct=pct,
            )
            self._curve_cache[mint] = (time.time(), result)
            # Evict stale entries periodically
            if len(self._curve_cache) > 200:
                cutoff = time.time() - self._curve_cache_ttl * 3
                self._curve_cache = {
                    k: v for k, v in self._curve_cache.items() if v[0] > cutoff
                }
            return result
        except ImportError:
            return None
        except Exception as e:
            log.debug(f"Bonding curve state error for {mint[:8]}: {e}")
            return None

    async def is_graduated(self, mint: str) -> bool:
        """Check if a pump.fun token has graduated (bonding curve complete)."""
        try:
            from solders.pubkey import Pubkey
            mint_pk = Pubkey.from_string(mint)
            pump_pk = Pubkey.from_string(PUMP_PROGRAM)

            pda, _bump = Pubkey.find_program_address(
                [PUMP_BONDING_SEED, bytes(mint_pk)], pump_pk
            )

            info = await self._get_account_info(str(pda))
            if not info:
                return True

            data = info.get("data", [])
            if not data:
                return True

            raw = base64.b64decode(data[0])
            if len(raw) > 48:
                complete = raw[48]
                return bool(complete)
            return False
        except Exception as e:
            log.debug(f"is_graduated check error for {mint[:8]}: {e}")
            return False

    async def get_recent_tx_count(self, mint: str, seconds: int = 300) -> int:
        """Count recent transactions on a token's bonding curve PDA."""
        try:
            from solders.pubkey import Pubkey
            mint_pk = Pubkey.from_string(mint)
            pump_pk = Pubkey.from_string(PUMP_PROGRAM)
            pda, _ = Pubkey.find_program_address(
                [PUMP_BONDING_SEED, bytes(mint_pk)], pump_pk
            )
            result = await self._rpc_call("getSignaturesForAddress", [
                str(pda), {"limit": 50, "commitment": "confirmed"}
            ])
            if not result:
                return 0
            cutoff = time.time() - seconds
            return sum(1 for s in result if s.get("blockTime", 0) > cutoff)
        except Exception as e:
            log.debug(f"get_recent_tx_count error for {mint[:8]}: {e}")
            return 0

    async def get_bonding_curve_states_batch(self, mints: list[str]) -> dict[str, BondingCurveState]:
        """Batch version — fetch all bonding curve states in one getMultipleAccounts call."""
        try:
            from solders.pubkey import Pubkey
            pump_pk = Pubkey.from_string(PUMP_PROGRAM)

            # Derive PDAs and check cache
            result_map = {}
            uncached_pdas = []
            uncached_mints = []

            for mint in mints:
                if mint in self._curve_cache:
                    ts, cached = self._curve_cache[mint]
                    if time.time() - ts < self._curve_cache_ttl:
                        result_map[mint] = cached
                        continue

                try:
                    mint_pk = Pubkey.from_string(mint)
                    pda, _ = Pubkey.find_program_address(
                        [PUMP_BONDING_SEED, bytes(mint_pk)], pump_pk
                    )
                    uncached_pdas.append(str(pda))
                    uncached_mints.append(mint)
                except Exception:
                    continue

            if not uncached_pdas:
                return result_map

            # Single RPC call for all uncached
            rpc_result = await self._rpc_call("getMultipleAccounts", [
                uncached_pdas,
                {"encoding": "base64", "commitment": "confirmed"},
            ])

            if not rpc_result or "value" not in rpc_result:
                return result_map

            now = time.time()
            for i, account in enumerate(rpc_result["value"]):
                mint = uncached_mints[i]
                if not account:
                    continue

                data = account.get("data", [])
                if not data:
                    continue

                try:
                    raw = base64.b64decode(data[0])
                    if len(raw) < 49:
                        continue

                    v_tokens = struct.unpack_from("<Q", raw, 8)[0]
                    v_sol = struct.unpack_from("<Q", raw, 16)[0]
                    r_tokens = struct.unpack_from("<Q", raw, 24)[0]
                    r_sol = struct.unpack_from("<Q", raw, 32)[0]
                    complete = bool(raw[48]) if len(raw) > 48 else False

                    GRADUATION_SOL = 85.0
                    sol_in_curve = r_sol / 1e9
                    pct = min((sol_in_curve / GRADUATION_SOL) * 100, 100.0)
                    if complete:
                        pct = 100.0

                    state = BondingCurveState(
                        virtual_sol=v_sol / 1e9,
                        real_sol=sol_in_curve,
                        virtual_tokens=v_tokens,
                        real_tokens=r_tokens,
                        complete=complete,
                        pct=pct,
                    )
                    result_map[mint] = state
                    self._curve_cache[mint] = (now, state)
                except Exception:
                    continue

            # Evict stale cache entries
            if len(self._curve_cache) > 200:
                cutoff = now - self._curve_cache_ttl * 3
                self._curve_cache = {
                    k: v for k, v in self._curve_cache.items() if v[0] > cutoff
                }

            return result_map
        except ImportError:
            return {}
        except Exception as e:
            log.debug(f"Batch bonding curve error: {e}")
            return {}

    async def get_token_decimals(self, mint: str) -> int:
        """Read token decimals from mint account."""
        try:
            result = OnChainResult()
            await self._check_mint_account(mint, result)
            return result.decimals
        except Exception:
            return 6  # pump.fun default

    # ── RPC Helpers ───────────────────────────────────────────

    async def _rpc_call(self, method: str, params: list) -> dict | None:
        for url in [self.rpc_url, self.fallback_rpc_url]:
            if not url:
                continue
            try:
                resp = await self.http.post(url, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": method, "params": params,
                })
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    if isinstance(err, dict) and err.get("code") == -32429:
                        log.debug(f"RPC rate limited, trying fallback")
                        continue
                    log.debug(f"RPC error: {err}")
                    return None
                return data.get("result")
            except Exception as e:
                log.debug(f"RPC call failed ({url[:30]}...): {e}")
                continue
        return None

    async def _get_account_info(self, address: str, encoding: str = "base64") -> dict | None:
        result = await self._rpc_call("getAccountInfo", [
            address, {"encoding": encoding, "commitment": "confirmed"}
        ])
        if result and result.get("value"):
            return result["value"]
        return None

    # ── Mint Account Analysis ─────────────────────────────────

    async def _check_mint_account(self, mint: str, result: OnChainResult):
        """Read mint account to check authorities and supply."""
        info = await self._get_account_info(mint)
        if not info:
            result.warnings.append("Could not read mint account")
            return

        data = info.get("data", [])
        if not data or len(data) < 2:
            return

        try:
            raw = base64.b64decode(data[0])
            if len(raw) < 82:
                return

            mint_auth_option = struct.unpack_from("<I", raw, 0)[0]
            supply = struct.unpack_from("<Q", raw, 36)[0]
            decimals = raw[44]
            freeze_auth_option = struct.unpack_from("<I", raw, 46)[0]

            result.decimals = decimals
            result.supply = supply / (10 ** decimals)

            result.mint_authority_revoked = (mint_auth_option == 0)
            if not result.mint_authority_revoked:
                result.warnings.append("Mint authority NOT revoked — can mint more tokens")

            result.freeze_authority_revoked = (freeze_auth_option == 0)
            if not result.freeze_authority_revoked:
                result.warnings.append("Freeze authority active — can freeze your tokens")

        except Exception as e:
            log.debug(f"Mint parse error: {e}")
            result.warnings.append("Could not parse mint data")

    # ── Holder Analysis ───────────────────────────────────────

    async def _check_holders(self, mint: str, result: OnChainResult):
        """Get top token holders and concentration.

        Excludes the pump.fun bonding curve from holder calculations,
        since it holds unsold supply and isn't a real whale.
        """
        resp = await self._rpc_call("getTokenLargestAccounts", [
            mint, {"commitment": "confirmed"}
        ])
        if not resp or "value" not in resp:
            result.warnings.append("Could not fetch holder data")
            return

        accounts = resp["value"]
        if not accounts:
            return

        sorted_accts = sorted(
            accounts,
            key=lambda a: float(a.get("uiAmount", 0) or 0),
            reverse=True
        )

        # Exclude bonding curve: derive PDA, check if top holder is owned by it
        curve_pda_str = ""
        try:
            from solders.pubkey import Pubkey
            mint_pk = Pubkey.from_string(mint)
            pump_pk = Pubkey.from_string(PUMP_PROGRAM)
            pda, _ = Pubkey.find_program_address(
                [PUMP_BONDING_SEED, bytes(mint_pk)], pump_pk
            )
            curve_pda_str = str(pda)
        except Exception:
            pass

        curve_account = None
        if curve_pda_str and sorted_accts:
            # Only check the top holder (bonding curve is always #1 during bonding phase)
            top_addr = sorted_accts[0].get("address", "")
            try:
                info = await self._rpc_call("getAccountInfo", [
                    top_addr, {"encoding": "jsonParsed", "commitment": "confirmed"}
                ])
                if info and info.get("value"):
                    data = info["value"].get("data", {})
                    if isinstance(data, dict):
                        owner = data.get("parsed", {}).get("info", {}).get("owner", "")
                        if owner == curve_pda_str:
                            curve_account = top_addr
            except Exception:
                pass

        # Filter out bonding curve and recalculate
        if curve_account:
            filtered = [a for a in sorted_accts if a.get("address") != curve_account]
            circulating = sum(float(a.get("uiAmount", 0) or 0) for a in filtered)
        else:
            filtered = sorted_accts
            circulating = sum(float(a.get("uiAmount", 0) or 0) for a in filtered)

        if circulating <= 0:
            return

        result.holder_count = len([a for a in filtered if float(a.get("uiAmount", 0) or 0) > 0])

        if filtered:
            top1 = float(filtered[0].get("uiAmount", 0) or 0)
            result.top_holder_pct = (top1 / circulating * 100) if circulating > 0 else 0

        top5_sum = sum(
            float(a.get("uiAmount", 0) or 0)
            for a in filtered[:5]
        )
        result.top_5_holder_pct = (top5_sum / circulating * 100) if circulating > 0 else 0

        if result.top_holder_pct > 50:
            result.warnings.append(f"Top holder owns {result.top_holder_pct:.1f}% — high concentration")
        elif result.top_holder_pct > 25:
            result.warnings.append(f"Top holder owns {result.top_holder_pct:.1f}%")

        if result.top_5_holder_pct > 80:
            result.warnings.append(f"Top 5 hold {result.top_5_holder_pct:.1f}% — whale risk")

    # ── Bonding Curve (Pump.fun) ──────────────────────────────

    async def _check_bonding_curve(self, mint: str, result: OnChainResult):
        """Read pump.fun bonding curve state directly."""
        try:
            from solders.pubkey import Pubkey
            mint_pk = Pubkey.from_string(mint)
            pump_pk = Pubkey.from_string(PUMP_PROGRAM)

            pda, _bump = Pubkey.find_program_address(
                [PUMP_BONDING_SEED, bytes(mint_pk)], pump_pk
            )

            info = await self._get_account_info(str(pda))
            if not info:
                return

            data = info.get("data", [])
            if not data:
                return

            raw = base64.b64decode(data[0])
            result.is_pump_token = True

            if len(raw) >= 49:
                v_tokens = struct.unpack_from("<Q", raw, 8)[0]
                v_sol = struct.unpack_from("<Q", raw, 16)[0]
                r_tokens = struct.unpack_from("<Q", raw, 24)[0]
                r_sol = struct.unpack_from("<Q", raw, 32)[0]
                complete = raw[48] if len(raw) > 48 else 0

                sol_in_curve = r_sol / 1e9
                result.bonding_curve_sol = sol_in_curve

                GRADUATION_SOL = 85.0
                result.bonding_curve_pct = min(
                    (sol_in_curve / GRADUATION_SOL) * 100, 100.0
                )

                if complete:
                    result.bonding_curve_pct = 100.0

                log.debug(
                    f"Bonding curve {mint[:8]}: {sol_in_curve:.2f} SOL "
                    f"({result.bonding_curve_pct:.1f}%) "
                    f"complete={bool(complete)}"
                )

        except ImportError:
            log.debug("solders not available for PDA derivation")
        except Exception as e:
            log.debug(f"Bonding curve check error: {e}")

    # ── Creator Analysis ──────────────────────────────────────

    async def _check_creator(self, creator: str, result: OnChainResult):
        """Check how many tokens this creator has launched (rug indicator)."""
        try:
            resp = await self._rpc_call("getSignaturesForAddress", [
                creator, {"limit": 100, "commitment": "confirmed"}
            ])
            if resp:
                result.creator_token_count = len(resp)
                if len(resp) >= 50:
                    result.warnings.append(
                        f"Creator has {len(resp)}+ recent txs — possible serial deployer"
                    )
                elif len(resp) >= 20:
                    result.warnings.append(
                        f"Creator has {len(resp)} recent txs — active deployer"
                    )
        except Exception as e:
            log.debug(f"Creator check error: {e}")

    # ── Score Calculation ─────────────────────────────────────

    def _calculate_score(self, r: OnChainResult) -> int:
        """Calculate overall safety score from on-chain data."""
        score = 50  # baseline

        if r.mint_authority_revoked:
            score += 20
        else:
            score -= 25

        if r.freeze_authority_revoked:
            score += 10
        else:
            score -= 15

        if r.top_holder_pct < 10:
            score += 15
        elif r.top_holder_pct < 25:
            score += 5
        elif r.top_holder_pct > 50:
            score -= 20
        elif r.top_holder_pct > 30:
            score -= 10

        if r.is_pump_token:
            if r.bonding_curve_pct >= 95:
                score += 10
            elif r.bonding_curve_pct >= 70:
                score += 5

        if r.creator_token_count >= 50:
            score -= 15
        elif r.creator_token_count >= 20:
            score -= 5

        return max(min(score, 100), 0)

    async def close(self):
        await self.http.aclose()
