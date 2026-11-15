"""Shared transaction helpers — trade outcomes, confirmation, tx parsing.

A trade has three possible outcomes, and callers must treat them differently:

- confirmed: the transaction landed without error.
- failed:    it definitely did not take effect (rejected, on-chain error, or
             expired without ever landing). Safe to retry.
- unknown:   we cannot tell (RPC unreachable, request timed out after being
             sent). NOT safe to retry a buy or a partial sell — the caller
             must verify by token balance instead.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from solders.commitment_config import CommitmentLevel
from solders.rpc.config import RpcSendTransactionConfig
from solders.rpc.requests import SendVersionedTransaction

log = logging.getLogger(__name__)

CONFIRMED = "confirmed"
FAILED = "failed"
UNKNOWN = "unknown"
PENDING = "pending"   # internal: RPC answered, signature not seen yet

# A transaction's blockhash is valid for ~150 slots (roughly 60-80s). Past
# this, a signature the RPC has never seen can no longer land.
BLOCKHASH_EXPIRY_SEC = 90


@dataclass
class TradeResult:
    """Outcome of a buy or sell attempt."""
    status: str = FAILED
    sig: str = ""                 # signature that decided the outcome
    via_lightning: bool = False
    sigs: list[str] = field(default_factory=list)  # every signature attempted

    @property
    def confirmed(self) -> bool:
        return self.status == CONFIRMED

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    @property
    def unknown(self) -> bool:
        return self.status == UNKNOWN


@dataclass
class TxOutcome:
    """What a transaction actually did to one wallet, read from getTransaction."""
    err: object = None            # on-chain error, or None on success
    owner_found: bool = False     # False = the tx was not signed/paid by this wallet
    sol_delta: float = 0.0        # SOL change for the wallet (negative = spent)
    fee: float = 0.0              # network fee in SOL
    token_delta: float = 0.0      # UI token change for (owner, mint)
    token_post: float = 0.0       # UI token balance after the tx


def _account_keys(tx_result: dict) -> list[str]:
    msg = (tx_result.get("transaction") or {}).get("message") or {}
    keys = []
    for k in msg.get("accountKeys") or []:
        keys.append(k.get("pubkey", "") if isinstance(k, dict) else str(k))
    # Raw (non-parsed) encodings list lookup-table addresses separately
    loaded = (tx_result.get("meta") or {}).get("loadedAddresses") or {}
    if loaded and not any(isinstance(k, dict)
                          for k in msg.get("accountKeys") or []):
        keys.extend(loaded.get("writable") or [])
        keys.extend(loaded.get("readonly") or [])
    return keys


def _token_total(balances: list, owner: str, mint: str) -> float:
    total = 0.0
    for b in balances or []:
        if b.get("owner") == owner and b.get("mint") == mint:
            ui = b.get("uiTokenAmount") or {}
            amount = ui.get("uiAmount")
            if amount is None:
                try:
                    amount = int(ui.get("amount", 0)) / (10 ** int(ui.get("decimals", 0)))
                except (TypeError, ValueError):
                    amount = 0
            total += float(amount or 0)
    return total


def parse_tx_outcome(tx_result: dict | None, owner: str, mint: str) -> TxOutcome | None:
    """Extract what a transaction did to `owner`'s SOL and `mint` balance.

    Returns None when there is no transaction data to read.
    """
    if not tx_result:
        return None
    meta = tx_result.get("meta") or {}
    out = TxOutcome(err=meta.get("err"), fee=(meta.get("fee") or 0) / 1e9)

    keys = _account_keys(tx_result)
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if owner in keys:
        idx = keys.index(owner)
        out.owner_found = True
        if idx < len(pre) and idx < len(post):
            out.sol_delta = (post[idx] - pre[idx]) / 1e9

    pre_tok = _token_total(meta.get("preTokenBalances"), owner, mint)
    post_tok = _token_total(meta.get("postTokenBalances"), owner, mint)
    out.token_delta = post_tok - pre_tok
    out.token_post = post_tok
    return out


async def _signature_status(http, rpc_urls: list[str], signature: str,
                            search_history: bool) -> str:
    """One status lookup. Returns CONFIRMED, FAILED, PENDING or UNKNOWN."""
    answered = False
    for rpc_url in rpc_urls:
        try:
            resp = await http.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignatureStatuses",
                "params": [[signature],
                           {"searchTransactionHistory": search_history}],
            })
            data = resp.json()
            if "result" not in data:
                continue  # RPC-level error — try the fallback
            answered = True
            value = (data["result"].get("value") or [None])[0]
            if value:
                if value.get("err"):
                    log.warning(f"  Tx failed on-chain: {value['err']}")
                    return FAILED
                if value.get("confirmationStatus", "") in ("confirmed", "finalized"):
                    return CONFIRMED
            break  # valid answer — no need to ask the fallback
        except Exception as e:
            log.debug(f"  Confirm poll error ({rpc_url[:30]}): {type(e).__name__}")
    return PENDING if answered else UNKNOWN


async def confirm_signature(http, rpc_urls: list[str], signature: str,
                            timeout: float = 15, poll_interval: float = 0.4,
                            backoff: bool = False) -> str:
    """Poll a signature until it confirms, fails, or `timeout` elapses.

    Returns CONFIRMED, FAILED (on-chain error), PENDING (RPC reachable but the
    signature has not been seen) or UNKNOWN (no RPC answered at all).
    """
    start = time.time()

    while time.time() - start < timeout:
        status = await _signature_status(http, rpc_urls, signature, False)
        if status in (CONFIRMED, FAILED):
            if status == CONFIRMED:
                log.info(f"  Tx confirmed in {time.time() - start:.1f}s: "
                         f"{signature[:16]}...")
            return status
        await asyncio.sleep(poll_interval)
        if backoff:
            poll_interval = min(poll_interval * 1.5, 3.0)

    # Last look, this time through the ledger history rather than the
    # recent-status cache, so a tx that landed is not reported as missing.
    # PENDING ("not seen") is only returned when THIS lookup answered: an
    # earlier answer says nothing about what happened since, and callers
    # treat PENDING-after-expiry as "definitely did not land".
    status = await _signature_status(http, rpc_urls, signature, True)
    if status in (PENDING, UNKNOWN):
        log.warning(f"  Tx confirmation timeout ({timeout:.0f}s): {signature[:16]}...")
    return status


async def send_signed_tx(http, rpc_urls: list[str], tx) -> tuple[str, bool | None]:
    """Send an already-signed transaction.

    Returns (signature, accepted). `accepted` is True when an RPC accepted it,
    False when every RPC explicitly rejected it, and None when at least one
    send raised (timeout/network) so the tx may or may not be in flight.
    Re-sending the same signed transaction is idempotent.
    """
    sig = str(tx.signatures[0])
    send_cfg = RpcSendTransactionConfig(
        skip_preflight=True,
        preflight_commitment=CommitmentLevel.Confirmed,
        max_retries=3,
    )
    body = SendVersionedTransaction(tx, send_cfg).to_json()
    ambiguous = False
    for rpc_url in rpc_urls:
        try:
            resp = await http.post(
                rpc_url, content=body,
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
            if "result" in data:
                return sig, True
            log.warning(f"  RPC send rejected ({rpc_url[:30]}): {data.get('error', data)}")
        except Exception as e:
            ambiguous = True
            log.warning(f"  RPC send error ({rpc_url[:30]}): {type(e).__name__}")
    return sig, (None if ambiguous else False)


async def get_tx_result(http, rpc_urls: list[str], signature: str,
                        retries: int = 1, delay: float = 1.0) -> dict | None:
    """Fetch a full transaction. Retries because RPCs index a tx slightly
    after its signature status becomes visible."""
    for attempt in range(max(retries, 1)):
        for rpc_url in rpc_urls:
            try:
                resp = await http.post(rpc_url, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [signature, {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    }],
                })
                result = resp.json().get("result")
                if result:
                    return result
            except Exception as e:
                log.debug(f"  Get tx error ({rpc_url[:30]}): {type(e).__name__}")
        if attempt < retries - 1:
            await asyncio.sleep(delay)
    return None
