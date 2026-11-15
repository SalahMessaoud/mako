"""Jupiter V6 swap routing — buy/sell graduated tokens through PumpSwap/Raydium."""

import asyncio
import logging
import time
import base64
import httpx
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from config import JUPITER_QUOTE_URL, JUPITER_SWAP_URL, WSOL
from txutil import (
    TradeResult, CONFIRMED, FAILED, UNKNOWN, PENDING, BLOCKHASH_EXPIRY_SEC,
    confirm_signature, send_signed_tx,
)

log = logging.getLogger(__name__)


class JupiterSwap:
    """
    Jupiter V6 API client for trading graduated tokens (post-pump.fun migration).
    Fully async — never blocks the event loop.
    Supports Jito bundle sending and direct RPC fallback.
    """

    MAX_RETRIES = 3
    RETRY_DELAY = 1.5
    SELL_CONFIRM_TIMEOUT = 30

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
                         use_jito: bool = True,
                         resend_on_timeout: bool = True) -> TradeResult:
        """Sell tokens through Jupiter (token -> SOL).

        `resend_on_timeout` must be False for partial sells — a second send of
        a fixed amount would sell it twice.
        """
        if not self.keypair:
            log.error("No keypair for Jupiter sell")
            return TradeResult(FAILED)

        slippage = slippage_bps if slippage_bps is not None else self.slippage_bps
        sigs: list[str] = []
        undecided = False

        for attempt in range(1, self.MAX_RETRIES + 1):
            sig = None
            try:
                sig = await self._execute_swap(mint, WSOL, raw_amount, slippage, use_jito)
            except Exception as e:
                log.error(f"  Jupiter sell error (attempt {attempt}): {type(e).__name__}: {e}")
            if sig:
                sigs.append(sig)
                status = await confirm_signature(
                    self.http, self._rpc_urls, sig,
                    timeout=self.SELL_CONFIRM_TIMEOUT, poll_interval=0.5, backoff=True,
                )
                if status == CONFIRMED:
                    log.info(f"  Jupiter sell confirmed: {sig[:16]}...")
                    return TradeResult(CONFIRMED, sig, False, sigs)
                if status in (PENDING, UNKNOWN):
                    undecided = True
                    log.warning(f"  Jupiter tx not confirmed (attempt {attempt}): {sig}")
                    if not resend_on_timeout:
                        return TradeResult(UNKNOWN, sig, False, sigs)
                else:
                    log.warning(f"  Jupiter sell failed on-chain (attempt {attempt}): {sig}")
            else:
                log.warning(f"  Jupiter sell failed (attempt {attempt}/{self.MAX_RETRIES})")

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * attempt)

        for sig in sigs:
            if await confirm_signature(self.http, self._rpc_urls, sig,
                                       timeout=0) == CONFIRMED:
                return TradeResult(CONFIRMED, sig, False, sigs)

        log.error(f"  Jupiter sell not confirmed after {self.MAX_RETRIES} attempts: {mint[:16]}")
        return TradeResult(UNKNOWN if undecided else FAILED, "", False, sigs)

    async def buy_token(self, mint: str, sol_amount: float, slippage_bps: int = None,
                        use_jito: bool = True) -> TradeResult:
        """Buy tokens through Jupiter (SOL -> token).

        Re-sent only after a definite failure. After a timeout the same
        signature is watched until its blockhash expires, so a slow
        transaction can never turn into two buys.
        """
        if not self.keypair:
            log.error("No keypair for Jupiter buy")
            return TradeResult(FAILED)

        slippage = slippage_bps if slippage_bps is not None else self.slippage_bps
        raw_sol = int(sol_amount * 1e9)  # SOL to lamports
        sigs: list[str] = []

        for attempt in range(1, self.MAX_RETRIES + 1):
            sig = None
            try:
                sig = await self._execute_swap(WSOL, mint, raw_sol, slippage, use_jito)
            except Exception as e:
                log.error(f"  Jupiter buy error (attempt {attempt}): {type(e).__name__}: {e}")
            if sig:
                sigs.append(sig)
                status = await confirm_signature(
                    self.http, self._rpc_urls, sig,
                    timeout=BLOCKHASH_EXPIRY_SEC, poll_interval=0.5, backoff=True,
                )
                if status == CONFIRMED:
                    log.info(f"  Jupiter buy confirmed: {sig[:16]}...")
                    return TradeResult(CONFIRMED, sig, False, sigs)
                if status == UNKNOWN:
                    log.warning(f"  Jupiter buy outcome unknown (RPC unreachable): {sig[:16]}...")
                    return TradeResult(UNKNOWN, sig, False, sigs)
                if status == PENDING:
                    log.warning(f"  Jupiter buy tx expired without landing: {sig[:16]}...")
                    return TradeResult(FAILED, sig, False, sigs)
                log.warning(f"  Jupiter buy failed on-chain (attempt {attempt}): {sig}")
            else:
                log.warning(f"  Jupiter buy failed (attempt {attempt}/{self.MAX_RETRIES})")

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * attempt)

        log.error(f"  Jupiter buy FAILED after {self.MAX_RETRIES} attempts: {mint[:16]}")
        return TradeResult(FAILED, "", False, sigs)

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

        # The transaction is signed: its signature is known, and every send
        # below submits the SAME transaction (idempotent).
        sig = str(tx.signatures[0])

        # Try Jito bundle first
        if use_jito and self._jito_sender:
            try:
                if await self._jito_sender(tx):
                    log.info(f"  Jupiter {direction} via Jito: {sig[:16]}...")
                    return sig
                log.warning(f"  Jito failed for Jupiter {direction}, falling back to direct RPC")
            except Exception as e:
                log.warning(f"  Jito error for Jupiter {direction}: {type(e).__name__}, "
                            f"falling back to direct RPC")

        # Direct RPC send. A send that timed out may still be in flight, so
        # the signature is returned and confirmation decides the outcome.
        _sig, accepted = await send_signed_tx(self.http, self._rpc_urls, tx)
        if accepted is False:
            return None
        return sig

    async def close(self):
        await self.http.aclose()
