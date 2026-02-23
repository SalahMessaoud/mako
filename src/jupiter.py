"""Jupiter V6 swap routing — buy/sell graduated tokens through PumpSwap/Raydium."""

import asyncio
import logging
import time
import base64
import httpx
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from config import JUPITER_QUOTE_URL, JUPITER_SWAP_URL, WSOL

log = logging.getLogger(__name__)


class JupiterSwap:
    """
    Jupiter V6 API client for trading graduated tokens (post-pump.fun migration).
    Fully async — never blocks the event loop.
    Supports Jito bundle sending and direct RPC fallback.
    """

    MAX_RETRIES = 3
    RETRY_DELAY = 1.5

    def __init__(self, rpc_url: str, keypair: Keypair = None,
                 slippage_bps: int = 1500, jito_sender=None,
                 fallback_rpc_url: str = ""):
        self.rpc_url = rpc_url
        self._fallback_rpc_url = fallback_rpc_url
        self.keypair = keypair
        self.slippage_bps = slippage_bps
        self._jito_sender = jito_sender  # async callable: (VersionedTransaction) -> str | None
        self.http = httpx.AsyncClient(timeout=20)

    @property
    def _rpc_urls(self) -> list[str]:
        urls = [self.rpc_url]
        if self._fallback_rpc_url:
            urls.append(self._fallback_rpc_url)
        return urls

    async def sell_token(self, mint: str, raw_amount: int, slippage_bps: int = None,
                         use_jito: bool = True) -> str | None:
        """Sell tokens through Jupiter V6 (token -> SOL)."""
        if not self.keypair:
            log.error("No keypair for Jupiter sell")
            return None

        slippage = slippage_bps if slippage_bps is not None else self.slippage_bps

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                sig = await self._execute_swap(mint, WSOL, raw_amount, slippage, use_jito)
                if sig:
                    confirmed = await self._confirm_tx(sig)
                    if confirmed:
                        log.info(f"  Jupiter sell confirmed: {sig[:16]}...")
                        return sig
                    log.warning(f"  Jupiter tx not confirmed (attempt {attempt}): {sig}")
                else:
                    log.warning(f"  Jupiter sell failed (attempt {attempt}/{self.MAX_RETRIES})")
            except Exception as e:
                log.error(f"  Jupiter sell error (attempt {attempt}): {e}")

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * attempt)

        log.error(f"  Jupiter sell FAILED after {self.MAX_RETRIES} attempts: {mint[:16]}")
        return None

    async def buy_token(self, mint: str, sol_amount: float, slippage_bps: int = None,
                        use_jito: bool = True) -> str | None:
        """Buy tokens through Jupiter V6 (SOL -> token)."""
        if not self.keypair:
            log.error("No keypair for Jupiter buy")
            return None

        slippage = slippage_bps if slippage_bps is not None else self.slippage_bps
        raw_sol = int(sol_amount * 1e9)  # SOL to lamports

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                sig = await self._execute_swap(WSOL, mint, raw_sol, slippage, use_jito)
                if sig:
                    confirmed = await self._confirm_tx(sig)
                    if confirmed:
                        log.info(f"  Jupiter buy confirmed: {sig[:16]}...")
                        return sig
                    log.warning(f"  Jupiter buy tx not confirmed (attempt {attempt}): {sig}")
                else:
                    log.warning(f"  Jupiter buy failed (attempt {attempt}/{self.MAX_RETRIES})")
            except Exception as e:
                log.error(f"  Jupiter buy error (attempt {attempt}): {e}")

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * attempt)

        log.error(f"  Jupiter buy FAILED after {self.MAX_RETRIES} attempts: {mint[:16]}")
        return None

    async def get_quote_out_amount(self, mint: str, raw_amount: int) -> float:
        """Get estimated SOL output for a token sell. Returns SOL amount."""
        try:
            resp = await self.http.get(JUPITER_QUOTE_URL, params={
                "inputMint": mint,
                "outputMint": WSOL,
                "amount": str(raw_amount),
                "slippageBps": str(self.slippage_bps),
            })
            if resp.status_code == 200:
                data = resp.json()
                out_amount = int(data.get("outAmount", 0))
                return out_amount / 1e9  # lamports to SOL
        except Exception as e:
            log.debug(f"  Jupiter quote error: {e}")
        return 0.0

    async def _execute_swap(self, input_mint: str, output_mint: str,
                            raw_amount: int, slippage_bps: int,
                            use_jito: bool) -> str | None:
        """Generic swap: get quote, build tx, sign, send."""
        pubkey = str(self.keypair.pubkey())
        direction = "sell" if output_mint == WSOL else "buy"

        # 1. Get quote
        quote_resp = await self.http.get(JUPITER_QUOTE_URL, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(raw_amount),
            "slippageBps": str(slippage_bps),
        })
        if quote_resp.status_code != 200:
            log.error(f"  Jupiter {direction} quote error: {quote_resp.status_code} "
                      f"{quote_resp.text[:100]}")
            return None

        quote_data = quote_resp.json()
        out_amount = int(quote_data.get("outAmount", 0))
        if out_amount <= 0:
            token = output_mint if direction == "buy" else input_mint
            log.warning(f"  Jupiter {direction} quote returned 0 for {token[:16]}")
            return None

        log.debug(f"  Jupiter {direction} quote: {raw_amount} -> {out_amount}")

        # 2. Get swap transaction
        swap_resp = await self.http.post(JUPITER_SWAP_URL, json={
            "quoteResponse": quote_data,
            "userPublicKey": pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        })
        if swap_resp.status_code != 200:
            log.error(f"  Jupiter {direction} swap error: {swap_resp.status_code} "
                      f"{swap_resp.text[:100]}")
            return None

        swap_data = swap_resp.json()
        swap_tx_b64 = swap_data.get("swapTransaction")
        if not swap_tx_b64:
            log.error(f"  Jupiter {direction} swap returned no transaction")
            return None

        # 3. Deserialize, sign, send
        tx_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction(
            VersionedTransaction.from_bytes(tx_bytes).message,
            [self.keypair],
        )

        # Try Jito bundle first
        if use_jito and self._jito_sender:
            try:
                sig = await self._jito_sender(tx)
                if sig:
                    log.info(f"  Jupiter {direction} via Jito: {sig[:16]}...")
                    return sig
                log.warning(f"  Jito failed for Jupiter {direction}, falling back to direct RPC")
            except Exception as e:
                log.warning(f"  Jito error for Jupiter {direction}: {e}, falling back to direct RPC")

        # Direct RPC send
        from solders.rpc.requests import SendVersionedTransaction
        from solders.rpc.config import RpcSendTransactionConfig
        from solders.commitment_config import CommitmentLevel

        send_cfg = RpcSendTransactionConfig(
            skip_preflight=True,
            preflight_commitment=CommitmentLevel.Confirmed,
            max_retries=3,
        )
        send_req = SendVersionedTransaction(tx, send_cfg)
        for rpc_url in self._rpc_urls:
            try:
                rpc_resp = await self.http.post(
                    rpc_url, content=send_req.to_json(),
                    headers={"Content-Type": "application/json"},
                )
                rpc_data = rpc_resp.json()
                if "result" in rpc_data:
                    return rpc_data["result"]
                log.warning(f"  Jupiter {direction} RPC send ({rpc_url[:30]}): "
                            f"{rpc_data.get('error', rpc_data)}")
            except Exception as e:
                log.warning(f"  Jupiter {direction} RPC error ({rpc_url[:30]}): {e}")
        return None

    async def _confirm_tx(self, signature: str, timeout: int = 30) -> bool:
        """Wait for transaction confirmation (async)."""
        start = time.time()
        poll_interval = 0.5

        while time.time() - start < timeout:
            for rpc_url in self._rpc_urls:
                try:
                    resp = await self.http.post(rpc_url, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getSignatureStatuses",
                        "params": [[signature], {"searchTransactionHistory": False}],
                    })
                    data = resp.json()
                    value = data.get("result", {}).get("value", [None])[0]

                    if value:
                        if value.get("err"):
                            log.warning(f"  Jupiter tx failed on-chain: {value['err']}")
                            return False
                        status = value.get("confirmationStatus", "")
                        if status in ("confirmed", "finalized"):
                            elapsed = time.time() - start
                            log.info(f"  Jupiter tx confirmed ({status}) in {elapsed:.1f}s")
                            return True
                    break  # got a valid response, no need to try fallback
                except Exception as e:
                    log.debug(f"  Jupiter confirm poll error ({rpc_url[:30]}): {e}")

            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 3.0)

        log.warning(f"  Jupiter tx confirmation timeout: {signature[:16]}...")
        return False

    async def close(self):
        await self.http.aclose()
