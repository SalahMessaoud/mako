"""PumpPortal API — trade on Pump.fun with tx confirmation + retry + Jito bundles."""

import json
import logging
import asyncio
import time
import random
import base58
import websockets
import httpx
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.hash import Hash
from solders.message import MessageV0
from dataclasses import dataclass
from config import (
    PUMPPORTAL_TRADE_URL, PUMPPORTAL_LIGHTNING_URL,
    PUMPPORTAL_WS_URL, JITO_BUNDLE_URL,
)
from txutil import (
    TradeResult, CONFIRMED, FAILED, UNKNOWN, PENDING, BLOCKHASH_EXPIRY_SEC,
    confirm_signature, send_signed_tx, get_tx_result,
)

log = logging.getLogger(__name__)


@dataclass
class _Sent:
    """Result of submitting one trade request."""
    sig: str = ""
    via_lightning: bool = False
    ambiguous: bool = False   # submitted-or-not is unknown (timeout after send)


class PumpPortal:
    """
    PumpPortal trading client with:
    - Async I/O — never blocks the event loop
    - Transaction confirmation (verify tx actually landed)
    - Retry logic (3 attempts with backoff)
    - Timeout handling
    - Jito bundle support for MEV protection
    """

    MAX_RETRIES = 3
    BUY_MAX_WAIT = BLOCKHASH_EXPIRY_SEC  # watch a buy until it can no longer land
    SELL_CONFIRM_TIMEOUT = 15  # seconds for sells
    RETRY_DELAY = 1.5     # seconds between retries

    # Official Jito tip accounts
    JITO_TIP_ACCOUNTS = [
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4bVqkfRtQ7NmXwkiY894RA7",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSUije2BZGVHBhMiAMz",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    ]

    def __init__(self, rpc_url: str, keypair: Keypair = None, api_key: str = "",
                 use_jito: bool = False, jito_tip_sol: float = 0.001,
                 fallback_rpc_url: str = ""):
        self.rpc_url = rpc_url
        self._fallback_rpc_url = fallback_rpc_url
        self.keypair = keypair
        self.api_key = api_key
        self.use_jito = use_jito
        self.jito_tip_sol = jito_tip_sol
        self.http = httpx.AsyncClient(timeout=15)
        self.tx_history: list[dict] = []

    @property
    def _rpc_urls(self) -> list[str]:
        urls = [self.rpc_url]
        if self._fallback_rpc_url:
            urls.append(self._fallback_rpc_url)
        return urls

    # ── Trading with Retry ────────────────────────────────────

    async def buy(self, mint: str, sol_amount: float, slippage: int = 15,
                  priority_fee: float = 0.005, pool: str = "auto") -> TradeResult:
        """Buy. Returns a TradeResult (confirmed / failed / unknown).

        A buy is only re-sent after a DEFINITE failure (rejected, or an
        on-chain error). After a timeout we keep watching the same signature
        until its blockhash expires — sending a second transaction while the
        first may still land would buy twice.
        """
        sigs: list[str] = []
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                sent = await self._execute_trade(
                    "buy", mint, sol_amount, denominated_in_sol=True,
                    slippage=slippage, priority_fee=priority_fee, pool=pool,
                )
            except Exception as e:
                log.error(f"  Buy error (attempt {attempt}): {self._redact(e)}")
                sent = _Sent()

            if sent.sig:
                sigs.append(sent.sig)
                status = await confirm_signature(
                    self.http, self._rpc_urls, sent.sig,
                    timeout=self.BUY_MAX_WAIT,
                )
                self._record_tx("buy", mint, sol_amount, sent.sig,
                                status == CONFIRMED)
                if status == CONFIRMED:
                    return TradeResult(CONFIRMED, sent.sig, sent.via_lightning, sigs)
                if status == UNKNOWN:
                    log.warning(f"  Buy outcome unknown (RPC unreachable): {sent.sig[:16]}...")
                    return TradeResult(UNKNOWN, sent.sig, sent.via_lightning, sigs)
                if status == PENDING:
                    # Never seen within the blockhash lifetime — it expired.
                    # The signal is stale by now; do not chase it.
                    log.warning(f"  Buy tx expired without landing: {sent.sig[:16]}...")
                    return TradeResult(FAILED, sent.sig, sent.via_lightning, sigs)
                log.warning(f"  Buy tx failed on-chain (attempt {attempt}): {sent.sig[:16]}...")
            elif sent.ambiguous:
                # The request may have been submitted but we got no signature.
                log.warning("  Buy outcome unknown (no response after submit)")
                return TradeResult(UNKNOWN, "", sent.via_lightning, sigs)
            else:
                log.warning(f"  Buy rejected (attempt {attempt}/{self.MAX_RETRIES})")

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * attempt)

        log.error(f"  Buy FAILED after {self.MAX_RETRIES} attempts: {mint[:16]}")
        return TradeResult(FAILED, "", False, sigs)

    async def sell(self, mint: str, token_amount: float = None,
                   sell_pct: int = 100, slippage: int = 15,
                   priority_fee: float = 0.005, pool: str = "auto",
                   skip_jito: bool = False,
                   resend_on_timeout: bool = True) -> TradeResult:
        """Sell. Returns a TradeResult (confirmed / failed / unknown).

        `resend_on_timeout` must be False for partial sells: selling "50%"
        twice sells 75%. A full sell is safe to re-send — if the first one
        landed, the second simply fails.
        """
        amount = token_amount if token_amount is not None else f"{sell_pct}%"
        sigs: list[str] = []
        undecided = False

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                sent = await self._execute_trade(
                    "sell", mint, amount, denominated_in_sol=False,
                    slippage=slippage, priority_fee=priority_fee, pool=pool,
                    skip_jito=skip_jito,
                    allow_fallback_when_ambiguous=resend_on_timeout,
                )
            except Exception as e:
                log.error(f"  Sell error (attempt {attempt}): {self._redact(e)}")
                sent = _Sent()

            if sent.sig:
                sigs.append(sent.sig)
                status = await confirm_signature(
                    self.http, self._rpc_urls, sent.sig,
                    timeout=self.SELL_CONFIRM_TIMEOUT,
                )
                self._record_tx("sell", mint, 0, sent.sig, status == CONFIRMED)
                if status == CONFIRMED:
                    return TradeResult(CONFIRMED, sent.sig, sent.via_lightning, sigs)
                if status in (PENDING, UNKNOWN):
                    undecided = True
                    log.warning(f"  Sell tx not confirmed (attempt {attempt}): {sent.sig[:16]}...")
                    if not resend_on_timeout:
                        return TradeResult(UNKNOWN, sent.sig, sent.via_lightning, sigs)
                else:
                    log.warning(f"  Sell tx failed on-chain (attempt {attempt}): {sent.sig[:16]}...")
            elif sent.ambiguous:
                undecided = True
                log.warning("  Sell outcome unknown (no response after submit)")
                if not resend_on_timeout:
                    return TradeResult(UNKNOWN, "", sent.via_lightning, sigs)
            else:
                log.warning(f"  Sell rejected (attempt {attempt}/{self.MAX_RETRIES})")

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * attempt)

        # An earlier attempt may have landed while we were sending later ones.
        for sig in sigs:
            if await confirm_signature(self.http, self._rpc_urls, sig,
                                       timeout=0) == CONFIRMED:
                return TradeResult(CONFIRMED, sig, False, sigs)

        log.error(f"  Sell not confirmed after {self.MAX_RETRIES} attempts: {mint[:16]}")
        return TradeResult(UNKNOWN if undecided else FAILED, "", False, sigs)

    # ── Transaction lookup ────────────────────────────────────

    async def get_tx_result(self, signature: str, retries: int = 1) -> dict | None:
        """Get full transaction result (for balance change calculation)."""
        return await get_tx_result(self.http, self._rpc_urls, signature,
                                   retries=retries)

    # ── Trade Execution ───────────────────────────────────────

    def _redact(self, value) -> str:
        """Stringify for logging with the API key removed (it rides in a URL)."""
        text = f"{type(value).__name__}: {value}" if isinstance(value, Exception) else str(value)
        if self.api_key:
            text = text.replace(self.api_key, "***")
        return text

    async def _execute_trade(self, action: str, mint: str, amount,
                             denominated_in_sol: bool, slippage: int,
                             priority_fee: float, pool: str,
                             skip_jito: bool = False,
                             allow_fallback_when_ambiguous: bool = False) -> "_Sent":
        """Submit one trade. Lightning first (server-side), then local signing.

        Falls back from Lightning to local ONLY when Lightning clearly did not
        submit anything. If the Lightning request timed out after being sent,
        the trade may be in flight, and a local send would duplicate it.
        """
        if self.api_key:
            sent = await self._lightning_trade(action, mint, amount,
                                               denominated_in_sol, slippage,
                                               priority_fee, pool)
            if sent.sig:
                return sent
            if sent.ambiguous and not allow_fallback_when_ambiguous:
                return sent
            if self.keypair:
                log.warning(f"  Lightning {action} failed, falling back to local RPC")

        if self.keypair:
            return await self._local_trade(action, mint, amount,
                                           denominated_in_sol, slippage,
                                           priority_fee, pool,
                                           skip_jito=skip_jito)
        return _Sent()

    async def _lightning_trade(self, action, mint, amount, denom_sol,
                               slippage, priority_fee, pool) -> "_Sent":
        try:
            resp = await self.http.post(
                f"{PUMPPORTAL_LIGHTNING_URL}?api-key={self.api_key}",
                data={
                    "action": action, "mint": mint, "amount": amount,
                    "denominatedInSol": str(denom_sol).lower(),
                    "slippage": slippage, "priorityFee": priority_fee,
                    "pool": pool,
                },
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Never reached the server — nothing was submitted.
            log.error(f"Lightning connect error: {self._redact(e)}")
            return _Sent(via_lightning=True)
        except Exception as e:
            # Sent, but no answer: the trade may or may not have been placed.
            log.error(f"Lightning error after submit: {self._redact(e)}")
            return _Sent(via_lightning=True, ambiguous=True)

        if resp.status_code >= 500:
            # Server-side failure mid-request: cannot tell if it was placed.
            log.error(f"Lightning {action}: {resp.status_code} {resp.text[:100]}")
            return _Sent(via_lightning=True, ambiguous=True)
        if resp.status_code != 200:
            log.error(f"Lightning {action}: {resp.status_code} {resp.text[:100]}")
            return _Sent(via_lightning=True)
        try:
            body = resp.json()
        except Exception:
            body = None
        sig = (body.get("signature") or "") if isinstance(body, dict) else ""
        if sig:
            return _Sent(sig=sig, via_lightning=True)
        # A 200 without a signature is a clean rejection ONLY if the body
        # says so. Otherwise we cannot tell whether a trade was placed.
        rejected = isinstance(body, dict) and bool(body.get("errors") or body.get("error"))
        log.error(f"Lightning {action}: no signature in response: {resp.text[:100]}")
        return _Sent(via_lightning=True, ambiguous=not rejected)

    async def _local_trade(self, action, mint, amount, denom_sol,
                            slippage, priority_fee, pool,
                            skip_jito: bool = False) -> "_Sent":
        if not self.keypair:
            log.error("No keypair for local trade")
            return _Sent()
        try:
            resp = await self.http.post(PUMPPORTAL_TRADE_URL, data={
                "publicKey": str(self.keypair.pubkey()),
                "action": action, "mint": mint, "amount": amount,
                "denominatedInSol": str(denom_sol).lower(),
                "slippage": slippage, "priorityFee": priority_fee,
                "pool": pool,
            })
            if resp.status_code != 200:
                log.error(f"Local trade: {resp.status_code} {resp.text[:100]}")
                return _Sent()

            tx = VersionedTransaction(
                VersionedTransaction.from_bytes(resp.content).message,
                [self.keypair],
            )
        except Exception as e:
            # Nothing has been signed or sent yet — a clean failure.
            log.error(f"Local trade build error: {self._redact(e)}")
            return _Sent()

        # From here the transaction is signed: its signature is known, and
        # every send below submits the SAME transaction (idempotent).
        sig = str(tx.signatures[0])

        # Try Jito bundle if enabled (skip for emergency sells)
        if self.use_jito and not skip_jito:
            try:
                if await self._send_jito_bundle(tx):
                    log.info(f"  Jito bundle sent: {sig[:16]}...")
                    return _Sent(sig=sig)
                log.warning("  Jito bundle failed, falling back to direct RPC")
            except Exception as e:
                log.warning(f"  Jito error: {type(e).__name__}, falling back to direct RPC")

        # Direct RPC send (default or Jito fallback)
        _sig, accepted = await send_signed_tx(self.http, self._rpc_urls, tx)
        if accepted is False:
            return _Sent()            # every RPC explicitly rejected it
        # Accepted, or ambiguous (a send timed out): either way the signature
        # is what we watch — confirmation decides the outcome.
        return _Sent(sig=sig)

    # ── Jito Bundle Support ────────────────────────────────────

    def _build_tip_tx(self, blockhash: Hash) -> VersionedTransaction:
        """Build a separate tip transaction for Jito validators."""
        tip_account = Pubkey.from_string(random.choice(self.JITO_TIP_ACCOUNTS))
        tip_lamports = int(self.jito_tip_sol * 1e9)

        tip_ix = transfer(TransferParams(
            from_pubkey=self.keypair.pubkey(),
            to_pubkey=tip_account,
            lamports=tip_lamports,
        ))

        msg = MessageV0.try_compile(
            payer=self.keypair.pubkey(),
            instructions=[tip_ix],
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        return VersionedTransaction(msg, [self.keypair])

    async def _send_jito_bundle(self, trade_tx: VersionedTransaction) -> str | None:
        """Send trade tx + tip tx as a Jito bundle. Returns trade tx signature."""
        blockhash = trade_tx.message.recent_blockhash
        tip_tx = self._build_tip_tx(blockhash)

        trade_b58 = base58.b58encode(bytes(trade_tx)).decode("ascii")
        tip_b58 = base58.b58encode(bytes(tip_tx)).decode("ascii")

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [[trade_b58, tip_b58]],
        }

        resp = await self.http.post(
            JITO_BUNDLE_URL, json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        data = resp.json()

        if "error" in data:
            log.warning(f"  Jito bundle error: {data['error']}")
            return None

        trade_sig = str(trade_tx.signatures[0])
        log.debug(f"  Jito bundle accepted, bundle_id={data.get('result', '?')}")
        return trade_sig

    # ── Helpers ────────────────────────────────────────────────

    def _record_tx(self, action: str, mint: str, amount: float,
                   sig: str, confirmed: bool):
        self.tx_history.append({
            "action": action, "mint": mint, "amount": amount,
            "signature": sig, "confirmed": confirmed,
            "time": time.time(),
        })
        if len(self.tx_history) > 100:
            self.tx_history = self.tx_history[-100:]

    def get_recent_failures(self, minutes: int = 10) -> int:
        cutoff = time.time() - minutes * 60
        return sum(1 for tx in self.tx_history
                   if tx["time"] > cutoff and not tx["confirmed"])

    async def close(self):
        await self.http.aclose()


# The websockets library logs the handshake request line at DEBUG, and with
# a keyed connection that line contains the API key. Never let it through.
for _name in ("websockets", "websockets.client"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class PumpPortalStream:
    """WebSocket stream for real-time Pump.fun events with reconnect resilience."""

    def __init__(self, api_key: str = ""):
        # PumpPortal only serves subscribeTokenTrade / subscribeAccountTrade
        # to connections that carry a funded API key. Without it the server
        # answers with a notice and sends no trade events at all.
        self.api_key = api_key
        self._key_disabled = False         # keyed connection failed; fell back
        self.trade_feed_rejected = False   # server refused the trade subscriptions
        self.trade_events = 0              # trade events received
        self.ws = None
        self._handlers: dict[str, list] = {
            "new_token": [], "token_trade": [], "account_trade": [],
        }
        self._watched_wallets: set[str] = set()
        self._watched_tokens: set[str] = set()
        self._reconnect_callback = None

    def on_new_token(self, cb): self._handlers["new_token"].append(cb)
    def on_token_trade(self, cb): self._handlers["token_trade"].append(cb)
    def on_account_trade(self, cb): self._handlers["account_trade"].append(cb)
    def on_reconnect(self, cb): self._reconnect_callback = cb

    async def connect(self, watch_tokens=None, watch_wallets=None):
        log.info("  Connecting to PumpPortal WebSocket...")
        if watch_wallets:
            self._watched_wallets.update(watch_wallets)
        if watch_tokens:
            self._watched_tokens.update(watch_tokens)

        attempt = 0
        keyed_failures = 0
        while True:
            connected = False
            try:
                async with websockets.connect(self._url()) as ws:
                    self.ws = ws
                    connected = True
                    keyed_failures = 0
                    reconnected = attempt > 0
                    attempt = 0
                    log.info("  PumpPortal WS connected")

                    # Re-subscribe all tracked subscriptions on every connect
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    if self._watched_tokens:
                        await ws.send(json.dumps({
                            "method": "subscribeTokenTrade",
                            "keys": list(self._watched_tokens),
                        }))
                        log.info(f"  Re-subscribed {len(self._watched_tokens)} token feeds")
                    if self._watched_wallets:
                        await ws.send(json.dumps({
                            "method": "subscribeAccountTrade",
                            "keys": list(self._watched_wallets),
                        }))
                        log.info(f"  Re-subscribed {len(self._watched_wallets)} smart wallets")

                    # Trigger reconnect audit if this was a reconnection
                    if reconnected and self._reconnect_callback:
                        try:
                            await self._reconnect_callback()
                        except Exception as e:
                            log.error(f"  Reconnect audit error: {e}")

                    async for message in ws:
                        try:
                            data = json.loads(message)
                            await self._dispatch(data)
                        except json.JSONDecodeError:
                            continue

            except (websockets.ConnectionClosed, ConnectionError) as e:
                attempt += 1
                delay = min(3 * (2 ** attempt), 60) + random.uniform(0, 2)
                log.warning(f"WS disconnected: {self._redact(e)}, reconnecting in {delay:.1f}s (attempt {attempt})...")
                await asyncio.sleep(delay)
            except Exception as e:
                attempt += 1
                delay = min(3 * (2 ** attempt), 60) + random.uniform(0, 2)
                # A connection that is refused outright while carrying the
                # key must not take the free new-token feed down with it.
                if not connected and self.api_key and not self._key_disabled:
                    keyed_failures += 1
                    if keyed_failures >= 2:
                        self._key_disabled = True
                        self.trade_feed_rejected = True
                        log.warning("  PumpPortal would not accept the connection with "
                                    "the API key — reconnecting without it "
                                    "(no trade events)")
                log.error(f"WS error: {self._redact(e)}, reconnecting in {delay:.1f}s (attempt {attempt})...")
                await asyncio.sleep(delay)

    def _url(self) -> str:
        if self.api_key and not self._key_disabled:
            return f"{PUMPPORTAL_WS_URL}?api-key={self.api_key}"
        return PUMPPORTAL_WS_URL

    def _redact(self, text) -> str:
        text = str(text)
        return text.replace(self.api_key, "***") if self.api_key else text

    def _server_notice(self, message: str):
        """Handle a plain {"message": ...} reply from the server."""
        if "only available" in message or "API key" in message:
            if not self.trade_feed_rejected:
                self.trade_feed_rejected = True
                log.warning(
                    "  PumpPortal refused the trade subscriptions: "
                    f"{self._redact(message)[:200]} "
                    "— no token-trade or wallet-trade events will arrive. "
                    "Copy-trade then relies on RPC polling only, and creator-sell "
                    "rug detection, buyer-uniqueness checks and wallet discovery "
                    "get no data.")
        else:
            log.debug(f"  PumpPortal: {self._redact(message)[:200]}")

    async def _dispatch(self, data: dict):
        if isinstance(data, dict) and isinstance(data.get("message"), str) \
                and "traderPublicKey" not in data and "initialBuy" not in data:
            self._server_notice(data["message"])
            return

        if "initialBuy" in data:
            for h in self._handlers["new_token"]:
                try:
                    if asyncio.iscoroutinefunction(h): await h(data)
                    else: h(data)
                except Exception as e:
                    log.error(f"Handler error: {e}")
            return

        if "traderPublicKey" in data:
            self.trade_events += 1
            for h in self._handlers["token_trade"]:
                try:
                    if asyncio.iscoroutinefunction(h): await h(data)
                    else: h(data)
                except Exception as e:
                    log.error(f"Handler error: {e}")

            for h in self._handlers["account_trade"]:
                try:
                    if asyncio.iscoroutinefunction(h): await h(data)
                    else: h(data)
                except Exception as e:
                    log.error(f"Handler error: {e}")

    async def subscribe_token(self, mint: str):
        self._watched_tokens.add(mint)
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "subscribeTokenTrade", "keys": [mint],
            }))

    async def unsubscribe_token(self, mint: str):
        self._watched_tokens.discard(mint)
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "unsubscribeTokenTrade", "keys": [mint],
            }))

    async def subscribe_account(self, wallet: str):
        self._watched_wallets.add(wallet)
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "subscribeAccountTrade", "keys": [wallet],
            }))

    async def unsubscribe_account(self, wallet: str):
        self._watched_wallets.discard(wallet)
        if self.ws:
            await self.ws.send(json.dumps({
                "method": "unsubscribeAccountTrade", "keys": [wallet],
            }))
