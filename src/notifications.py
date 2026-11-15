"""Telegram notifications for trade alerts."""

import logging
from concurrent.futures import ThreadPoolExecutor
import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    """Send trade alerts via Telegram."""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self.http = httpx.Client(timeout=5)
        self._update_offset = 0
        # Alerts are sent from a single background thread so a slow Telegram
        # call can never stall the trading event loop. One worker keeps
        # messages in order.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify")
        if self.enabled:
            log.info("📱 Telegram notifications enabled")

    def send(self, message: str):
        """Send a message to the configured chat."""
        self.send_to(self.chat_id, message)

    def send_to(self, chat_id: str, message: str):
        """Queue a message to a specific chat_id (non-blocking)."""
        if not self.enabled:
            return
        try:
            self._pool.submit(self._send_now, chat_id, message)
        except RuntimeError:
            self._send_now(chat_id, message)  # pool already shut down

    def _send_now(self, chat_id: str, message: str):
        try:
            resp = self.http.post(
                TELEGRAM_API.format(token=self.token, method="sendMessage"),
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            )
            if resp.status_code != 200 or not resp.json().get("ok"):
                log.warning(f"Telegram API error: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            log.warning(f"Telegram error: {e}")

    def send_with_keyboard(self, text: str, keyboard: list[list[dict]],
                           chat_id: str = None, parse_mode: str = "HTML") -> int | None:
        """Send message with inline keyboard. Returns message_id."""
        if not self.enabled:
            return None
        cid = chat_id or self.chat_id
        try:
            resp = self.http.post(
                TELEGRAM_API.format(token=self.token, method="sendMessage"),
                json={
                    "chat_id": cid, "text": text, "parse_mode": parse_mode,
                    "reply_markup": {"inline_keyboard": keyboard},
                },
            )
            data = resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            log.warning(f"Telegram send_with_keyboard error: {data}")
        except Exception as e:
            log.warning(f"Telegram send_with_keyboard error: {e}")
        return None

    def edit_message(self, chat_id: str, message_id: int, text: str,
                     keyboard: list[list[dict]] = None, parse_mode: str = "HTML"):
        """Edit an existing message text and/or keyboard."""
        if not self.enabled:
            return
        payload: dict = {
            "chat_id": chat_id, "message_id": message_id,
            "text": text, "parse_mode": parse_mode,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            resp = self.http.post(
                TELEGRAM_API.format(token=self.token, method="editMessageText"),
                json=payload,
            )
            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "")
                # Ignore "message is not modified" — harmless
                if "message is not modified" not in desc:
                    log.warning(f"Telegram edit error: {desc}")
        except Exception as e:
            log.warning(f"Telegram edit error: {e}")

    def answer_callback(self, callback_query_id: str, text: str = ""):
        """Acknowledge a callback query (removes loading spinner)."""
        if not self.enabled:
            return
        try:
            self.http.post(
                TELEGRAM_API.format(token=self.token, method="answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
            )
        except Exception as e:
            log.warning(f"Telegram answerCallbackQuery error: {e}")

    def poll_events(self) -> list[dict]:
        """Poll for commands, text messages, and callback queries.

        Returns list of event dicts with type: command | text | callback.
        """
        if not self.enabled:
            return []
        try:
            resp = self.http.get(
                TELEGRAM_API.format(token=self.token, method="getUpdates"),
                params={"offset": self._update_offset, "timeout": 0},
            )
            data = resp.json()
            if not data.get("ok"):
                return []
            results = []
            for update in data.get("result", []):
                self._update_offset = update["update_id"] + 1

                # Callback query (button press)
                cb = update.get("callback_query")
                if cb:
                    chat_id = str(cb["message"]["chat"]["id"])
                    results.append({
                        "type": "callback",
                        "data": cb.get("data", ""),
                        "chat_id": chat_id,
                        "message_id": cb["message"]["message_id"],
                        "callback_id": cb["id"],
                    })
                    continue

                # Regular message or channel post
                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue
                text = msg.get("text", "")
                chat_id = str(msg["chat"]["id"])
                if text.startswith("/"):
                    command = text.split()[0].split("@")[0].lower()
                    results.append({
                        "type": "command", "cmd": command,
                        "args": text[len(command):].strip(),
                        "chat_id": chat_id,
                    })
                elif text.strip():
                    results.append({
                        "type": "text", "text": text.strip(),
                        "chat_id": chat_id,
                    })
            return results
        except Exception as e:
            log.info(f"Telegram poll error: {e}")
            return []

    def poll_commands(self) -> list[tuple[str, str]]:
        """Poll Telegram for bot commands. Returns list of (command, chat_id).

        Legacy wrapper around poll_events() for backward compatibility.
        """
        events = self.poll_events()
        return [(e["cmd"], e["chat_id"]) for e in events if e["type"] == "command"]

    def trade_alert(self, action: str, symbol: str, sol: float,
                     pnl: float = None, reason: str = "",
                     tx: str = "", balance: float = None,
                     daily_pnl: float = None):
        """Format and send a trade alert."""
        emoji = "🟢" if action == "buy" else ("🔴" if pnl and pnl < 0 else "✅")
        msg = f"{emoji} <b>{action.upper()}</b> ${symbol}\n"
        msg += f"💰 {sol:.4f} SOL\n"
        if pnl is not None:
            msg += f"📊 PnL: {pnl:+.4f} SOL\n"
        if reason:
            msg += f"📝 {reason}\n"
        if tx:
            msg += f'🔗 <a href="https://solscan.io/tx/{tx}">View TX</a>\n'
        if balance is not None:
            msg += f"💵 Balance: {balance:.4f} SOL\n"
        if daily_pnl is not None:
            msg += f"📈 Daily PnL: {daily_pnl:+.4f} SOL"
        self.send(msg)

    def status_alert(self, status: dict):
        """Send periodic status update."""
        msg = "📊 <b>Bot Status</b>\n"
        balance = status.get('balance', 0)
        roi = status.get('roi_pct', 0)
        msg += f"💵 Balance: {balance:.4f} SOL\n"
        msg += f"📈 ROI: {roi:+.1f}%\n"
        msg += f"Positions: {status.get('positions', 0)}\n"
        msg += f"Invested: {status.get('invested_sol', 0):.4f} SOL\n"
        msg += f"Daily PnL: {status.get('daily_pnl', 0):+.4f} SOL\n"
        msg += f"Trades: {status.get('daily_trades', 0)}\n"
        msg += f"Win rate: {status.get('win_rate', '0/0')}\n"
        self.send(msg)

    def candidate_alert(self, symbol: str, mint: str, confidence: float,
                         sol_in_curve: float, txns: int, curve_growth: float,
                         curve_pct: float = 0, strategy: str = "momentum"):
        """Notify about a strong candidate that passed filters."""
        msg = f"🔍 <b>CANDIDATE</b> ${symbol}\n"
        msg += f"Strategy: {strategy}\n"
        msg += f"Confidence: {confidence:.0%}\n"
        msg += f"SOL in curve: {sol_in_curve:.1f}\n"
        msg += f"Transactions: {txns}\n"
        msg += f"Curve growth: +{curve_growth:.0f}%\n"
        if curve_pct > 0:
            msg += f"Curve fill: {curve_pct:.0f}%\n"
        msg += f'🔗 <a href="https://pump.fun/{mint}">pump.fun</a>'
        self.send(msg)

    def graduation_alert(self, symbol: str, mint: str, curve_pct: float):
        """Notify when a token is approaching graduation."""
        msg = f"🎓 <b>NEAR GRADUATION</b> ${symbol}\n"
        msg += f"Curve: {curve_pct:.1f}%\n"
        msg += f'🔗 <a href="https://pump.fun/{mint}">pump.fun</a>'
        self.send(msg)

    def close(self):
        self._pool.shutdown(wait=True)  # flush queued alerts
        self.http.close()
