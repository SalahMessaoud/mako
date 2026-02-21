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
from solders.commitment_config import CommitmentLevel
from solders.rpc.requests import SendVersionedTransaction
from solders.rpc.config import RpcSendTransactionConfig
from config import (
    PUMPPORTAL_TRADE_URL, PUMPPORTAL_LIGHTNING_URL,
    PUMPPORTAL_WS_URL, JITO_BUNDLE_URL,
)

log = logging.getLogger(__name__)


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
    CONFIRM_TIMEOUT = 15       # seconds to wait for confirmation
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
                 use_jito: bool = False, jito_tip_sol: float = 0.001):
        self.rpc_url = rpc_url
        self.keypair = keypair
        self.api_key = api_key
        self.use_jito = use_jito
        self.jito_tip_sol = jito_tip_sol
        self.http = httpx.AsyncClient(timeout=15)
        self.tx_history: list[dict] = []

    # ── Trading with Retry ────────────────────────────────────

    async def buy(self, mint: str, sol_amount: float, slippage: int = 15,
                  priority_fee: float = 0.005, pool: str = "auto") -> str | None:
        """Buy with retry logic. Returns confirmed tx signature or None."""
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                sig = await self._execute_trade(
                    "buy", mint, sol_amount, denominated_in_sol=True,
                    slippage=slippage, priority_fee=priority_fee, pool=pool,
                )
                if sig:
                    confirmed = await self._confirm_tx(sig, fixed_interval=True)
                    if confirmed:
                        self._record_tx("buy", mint, sol_amount, sig, True)
                        return sig
                    else:
                        log.warning(f"  Buy tx not confirmed (attempt {attempt}): {sig}")
                        self._record_tx("buy", mint, sol_amount, sig, False)
                else:
                    log.warning(f"  Buy failed (attempt {attempt}/{self.MAX_RETRIES})")

            except Exception as e:
                log.error(f"  Buy error (attempt {attempt}): {e}")

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * attempt)

        log.error(f"  Buy FAILED after {self.MAX_RETRIES} attempts: {mint[:16]}")
        return None

    async def sell(self, mint: str, token_amount: float = None,
                   sell_pct: int = 100, slippage: int = 15,
                   priority_fee: float = 0.005, pool: str = "auto",
                   skip_jito: bool = False) -> str | None:
        """Sell with retry logic. Returns confirmed tx signature or None."""
        if token_amount is not None:
            amount = token_amount
            denom_sol = False
        else:
            amount = f"{sell_pct}%"
            denom_sol = False

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                sig = await self._execute_trade(
                    "sell", mint, amount, denominated_in_sol=denom_sol,
                    slippage=slippage, priority_fee=priority_fee, pool=pool,
                    skip_jito=skip_jito,
                )
                if sig:
                    confirmed = await self._confirm_tx(
                        sig, timeout=self.SELL_CONFIRM_TIMEOUT,
                        fixed_interval=True,
                    )
                    if confirmed:
                        self._record_tx("sell", mint, 0, sig, True)
                        return sig
                    else:
                        log.warning(f"  Sell tx not confirmed (attempt {attempt}): {sig}")
                        self._record_tx("sell", mint, 0, sig, False)
                else:
                    log.warning(f"  Sell failed (attempt {attempt}/{self.MAX_RETRIES})")

            except Exception as e:
                log.error(f"  Sell error (attempt {attempt}): {e}")

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * attempt)

        log.error(f"  Sell FAILED after {self.MAX_RETRIES} attempts: {mint[:16]}")
        return None

    # ── Transaction Confirmation ──────────────────────────────

    async def _confirm_tx(self, signature: str, timeout: int = None,
                          fixed_interval: bool = False) -> bool:
        """Wait for transaction confirmation. Non-blocking."""
        timeout = timeout or self.CONFIRM_TIMEOUT
        start = time.time()
        poll_interval = 0.4 if fixed_interval else 0.5

        while time.time() - start < timeout:
            try:
                resp = await self.http.post(self.rpc_url, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[signature], {"searchTransactionHistory": False}],
                })
                data = resp.json()
                result = data.get("result", {})
                value = result.get("value", [None])[0]

                if value:
                    status = value.get("confirmationStatus", "")
                    err = value.get("err")

                    if err:
                        log.warning(f"  Tx failed on-chain: {err}")
                        return False

                    if status in ("confirmed", "finalized"):
                        elapsed = time.time() - start
                        log.info(f"  Tx confirmed ({status}) in {elapsed:.1f}s: {signature[:16]}...")
                        return True

            except Exception as e:
                log.debug(f"  Confirm poll error: {e}")

            await asyncio.sleep(poll_interval)
            if not fixed_interval:
                poll_interval = min(poll_interval * 1.5, 3.0)

        log.warning(f"  Tx confirmation timeout ({timeout}s): {signature[:16]}...")
        return False

    async def get_tx_result(self, signature: str) -> dict | None:
        """Get full transaction result (for balance change calculation)."""
        try:
            resp = await self.http.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTransaction",
                "params": [signature, {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                }],
            })
            data = resp.json()
            return data.get("result")
        except Exception as e:
            log.debug(f"  Get tx error: {e}")
            return None

    # ── Trade Execution ───────────────────────────────────────

    async def _execute_trade(self, action: str, mint: str, amount,
                             denominated_in_sol: bool, slippage: int,
                             priority_fee: float, pool: str,
                             skip_jito: bool = False) -> str | None:
        if self.api_key:
            return await self._lightning_trade(action, mint, amount,
                                               denominated_in_sol, slippage,
                                               priority_fee, pool)
        else:
            return await self._local_trade(action, mint, amount,
                                            denominated_in_sol, slippage,
                                            priority_fee, pool,
                                            skip_jito=skip_jito)

    async def _lightning_trade(self, action, mint, amount, denom_sol,
                               slippage, priority_fee, pool) -> str | None:
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
            if resp.status_code == 200:
                return resp.json().get("signature", "")
            log.error(f"Lightning {action}: {resp.status_code} {resp.text[:100]}")
            return None
        except Exception as e:
            log.error(f"Lightning error: {e}")
            return None

    async def _local_trade(self, action, mint, amount, denom_sol,
                            slippage, priority_fee, pool,
                            skip_jito: bool = False) -> str | None:
        if not self.keypair:
            log.error("No keypair for local trade")
            return None
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
                return None

            tx_bytes = resp.content
            tx = VersionedTransaction(
                VersionedTransaction.from_bytes(tx_bytes).message,
                [self.keypair],
            )

            # Try Jito bundle if enabled (skip for emergency sells)
            if self.use_jito and not skip_jito:
                try:
                    sig = await self._send_jito_bundle(tx)
                    if sig:
                        log.info(f"  Jito bundle sent: {sig[:16]}...")
                        return sig
                    log.warning("  Jito bundle failed, falling back to direct RPC")
                except Exception as e:
                    log.warning(f"  Jito error: {e}, falling back to direct RPC")

            # Direct RPC send (default or Jito fallback)
            send_cfg = RpcSendTransactionConfig(
                skip_preflight=True,
                preflight_commitment=CommitmentLevel.Confirmed,
                max_retries=3,
            )
            send_req = SendVersionedTransaction(tx, send_cfg)
            rpc_resp = await self.http.post(
                self.rpc_url, content=send_req.to_json(),
                headers={"Content-Type": "application/json"},
            )
            rpc_data = rpc_resp.json()
            if "result" in rpc_data:
                return rpc_data["result"]
            log.error(f"RPC send: {rpc_data.get('error', rpc_data)}")
            return None
        except Exception as e:
            log.error(f"Local trade error: {e}")
            return None

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


class PumpPortalStream:
    """WebSocket stream for real-time Pump.fun events with reconnect resilience."""

    def __init__(self):
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
        while True:
            try:
                async with websockets.connect(PUMPPORTAL_WS_URL) as ws:
                    self.ws = ws
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
                log.warning(f"WS disconnected: {e}, reconnecting in {delay:.1f}s (attempt {attempt})...")
                await asyncio.sleep(delay)
            except Exception as e:
                attempt += 1
                delay = min(3 * (2 ** attempt), 60) + random.uniform(0, 2)
                log.error(f"WS error: {e}, reconnecting in {delay:.1f}s (attempt {attempt})...")
                await asyncio.sleep(delay)

    async def _dispatch(self, data: dict):
        if "initialBuy" in data:
            for h in self._handlers["new_token"]:
                try:
                    if asyncio.iscoroutinefunction(h): await h(data)
                    else: h(data)
                except Exception as e:
                    log.error(f"Handler error: {e}")
            return

        if "traderPublicKey" in data:
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
