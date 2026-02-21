"""Telegram GUI control panel — Trojan/BonkBot-style button-driven interface."""

import asyncio
import time
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ── Settable config map ──────────────────────────────────────
# key: (cfg_attr, type, presets, min, max, label)
SETTINGS = {
    "sl":   ("momentum_stop_loss_pct", float, [10, 15, 18, 25], 5, 50, "Stop Loss %"),
    "tp":   ("take_profit_pct", float, [60, 80, 100, 150], 20, 500, "Take Profit %"),
    "tp2":  ("take_profit_2_pct", float, [150, 200, 300, 500], 50, 1000, "TP2 %"),
    "trail":("trailing_stop_pct", float, [15, 20, 25, 35], 5, 60, "Trailing Stop %"),
    "mb":   ("max_buy_sol", float, [0.01, 0.02, 0.03, 0.05], 0.005, 0.5, "Max Buy SOL"),
    "conf": ("min_momentum_confidence", float, [0.55, 0.65, 0.75, 0.85], 0.3, 0.95, "Confidence"),
    "grow": ("min_momentum_curve_growth", float, [10, 15, 20, 30], 5, 200, "Curve Growth %"),
    "rate": ("min_momentum_growth_rate", float, [3, 5, 8, 12], 1, 30, "Growth Rate %/min"),
    "hold": ("max_hold_minutes", int, [10, 20, 30, 60], 5, 120, "Max Hold Min"),
    "slip": ("slippage_pct", int, [10, 15, 20, 25], 5, 50, "Slippage %"),
}


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


@dataclass
class UserState:
    screen: str = "home"
    menu_message_id: int = 0
    awaiting_input: str = ""
    context: dict = field(default_factory=dict)


class TelegramUI:
    """Button-driven Telegram GUI. Edits a single persistent message per chat."""

    def __init__(self, bot):
        self.bot = bot
        self.states: dict[str, UserState] = {}
        self._boot_time = time.time()

    def _state(self, chat_id: str) -> UserState:
        if chat_id not in self.states:
            self.states[chat_id] = UserState()
        return self.states[chat_id]

    # ── Event entry point ────────────────────────────────────

    async def handle_event(self, event: dict):
        chat_id = event["chat_id"]

        # Security: only allow configured chat
        if chat_id != self.bot.cfg.telegram_chat_id:
            return

        if event["type"] == "command":
            if event["cmd"] in ("/start", "/menu"):
                await self._send_home(chat_id)
            elif event["cmd"] == "/status":
                await self._send_home(chat_id)
            elif event["cmd"] == "/positions":
                await self._send_fresh(chat_id, "pos")
            elif event["cmd"] == "/trades":
                await self._send_fresh(chat_id, "trades")
            elif event["cmd"] == "/settings":
                await self._send_fresh(chat_id, "settings")

        elif event["type"] == "callback":
            self.bot.notify.answer_callback(event["callback_id"])
            await self._route_callback(chat_id, event["data"], event["message_id"])

        elif event["type"] == "text":
            await self._handle_text_input(chat_id, event["text"])

    # ── Sending / editing ────────────────────────────────────

    async def _send_home(self, chat_id: str):
        """Send a new dashboard message (becomes the persistent message)."""
        text, kb = self._render_home()
        msg_id = self.bot.notify.send_with_keyboard(text, kb, chat_id=chat_id)
        if msg_id:
            st = self._state(chat_id)
            st.menu_message_id = msg_id
            st.screen = "home"
            st.awaiting_input = ""

    async def _send_fresh(self, chat_id: str, screen: str):
        """Send a new message for a screen (when no persistent message exists)."""
        text, kb = await self._render_screen(screen)
        msg_id = self.bot.notify.send_with_keyboard(text, kb, chat_id=chat_id)
        if msg_id:
            st = self._state(chat_id)
            st.menu_message_id = msg_id
            st.screen = screen
            st.awaiting_input = ""

    async def _edit_screen(self, chat_id: str, screen: str, message_id: int = None):
        """Edit the persistent message to show a screen."""
        st = self._state(chat_id)
        mid = message_id or st.menu_message_id
        if not mid:
            await self._send_fresh(chat_id, screen)
            return
        text, kb = await self._render_screen(screen)
        self.bot.notify.edit_message(chat_id, mid, text, kb)
        st.screen = screen
        st.awaiting_input = ""

    async def _render_screen(self, screen: str) -> tuple[str, list]:
        if screen == "home":
            return self._render_home()
        elif screen == "pos":
            return await self._render_positions()
        elif screen == "trades":
            return self._render_trades()
        elif screen == "stats":
            return self._render_stats()
        elif screen == "settings":
            return self._render_settings()
        elif screen.startswith("set_"):
            return self._render_setting_edit(screen[4:])
        elif screen == "pause_confirm":
            return self._render_pause_confirm()
        elif screen == "sellall_confirm":
            return self._render_sellall_confirm()
        elif screen == "copywallets":
            return self._render_copy_wallets()
        elif screen == "strategies":
            return self._render_strategies()
        return self._render_home()

    # ── Callback router ──────────────────────────────────────

    async def _route_callback(self, chat_id: str, data: str, message_id: int):
        st = self._state(chat_id)
        st.menu_message_id = message_id  # track which message we're editing

        # Navigation
        if data == "home":
            await self._edit_screen(chat_id, "home", message_id)
        elif data == "pos":
            await self._edit_screen(chat_id, "pos", message_id)
        elif data == "trades":
            await self._edit_screen(chat_id, "trades", message_id)
        elif data == "stats":
            await self._edit_screen(chat_id, "stats", message_id)
        elif data == "settings":
            await self._edit_screen(chat_id, "settings", message_id)
        elif data == "refresh":
            await self._edit_screen(chat_id, st.screen, message_id)

        # Settings edit
        elif data.startswith("set_"):
            param = data[4:]
            if param in SETTINGS:
                await self._edit_screen(chat_id, f"set_{param}", message_id)

        # Save setting value (preset)
        elif data.startswith("sv_"):
            await self._save_setting(chat_id, data, message_id)

        # Custom value input prompt
        elif data.startswith("cust_"):
            param = data[5:]
            if param in SETTINGS:
                st.awaiting_input = param
                cfg_attr, typ, _, mn, mx, label = SETTINGS[param]
                text = f"Enter new <b>{label}</b> ({mn}-{mx}):"
                kb = [[_btn("Cancel", "settings")]]
                self.bot.notify.edit_message(chat_id, message_id, text, kb)

        # Sell position
        elif data.startswith("sell_"):
            await self._handle_sell(chat_id, data, message_id)

        # Sell all
        elif data == "sellall":
            await self._edit_screen(chat_id, "sellall_confirm", message_id)
        elif data == "sellall_yes":
            await self._execute_sell_all(chat_id, message_id)

        # Strategies
        elif data == "strategies":
            await self._edit_screen(chat_id, "strategies", message_id)
        elif data.startswith("strat_"):
            self._toggle_strategy(data[6:])
            await self._edit_screen(chat_id, "strategies", message_id)

        # Copy wallets
        elif data == "copywallets":
            await self._edit_screen(chat_id, "copywallets", message_id)
        elif data == "cw_add":
            st.awaiting_input = "cw_add"
            text = "Paste the wallet address to add:"
            kb = [[_btn("Cancel", "copywallets")]]
            self.bot.notify.edit_message(chat_id, message_id, text, kb)
        elif data.startswith("cw_rm_"):
            await self._remove_copy_wallet(chat_id, data[6:], message_id)

        # Pause / Resume
        elif data == "pause":
            await self._edit_screen(chat_id, "pause_confirm", message_id)
        elif data == "pause_yes":
            self.bot.cfg.notify_only = True
            text = "Bot paused. Monitoring continues but no trades will execute."
            kb = [[_btn("Resume Trading", "resume"), _btn("Home", "home")]]
            self.bot.notify.edit_message(chat_id, message_id, text, kb)
            st.screen = "paused"
        elif data == "resume":
            self.bot.cfg.notify_only = False
            text = "Bot resumed. Trading is active again."
            kb = [[_btn("Home", "home")]]
            self.bot.notify.edit_message(chat_id, message_id, text, kb)
            st.screen = "home"

    # ── Screen renderers ─────────────────────────────────────

    def _render_home(self) -> tuple[str, list]:
        status = self.bot.risk.get_status()
        sol = self.bot.wallet.get_sol_balance()

        # Uptime
        uptime_sec = time.time() - self._boot_time
        hrs, remainder = divmod(int(uptime_sec), 3600)
        mins = remainder // 60

        paused = self.bot.cfg.notify_only
        mode = "PAPER" if self.bot.cfg.paper_trading else "LIVE"
        pause_tag = " | PAUSED" if paused else ""

        # Wallet address (truncated for display)
        pubkey = getattr(self.bot.wallet, "pubkey", "")
        if pubkey and pubkey != "PAPER_WALLET":
            wallet_str = f"{pubkey[:6]}...{pubkey[-4:]}"
        else:
            wallet_str = "Paper Wallet"

        text = f"<b>Mako v3.0</b> [{mode}{pause_tag}]\n"
        text += f"Wallet: <code>{wallet_str}</code>\n\n"
        text += f"Balance: {sol:.4f} SOL\n"
        text += f"Daily PnL: {status['daily_pnl']:+.4f} SOL\n"
        text += f"Positions: {status['positions']} active\n"
        text += f"Win Rate: {status['win_rate']}\n"
        text += f"Trades today: {status['daily_trades']}\n"
        text += f"Uptime: {hrs}h {mins}m"

        pause_btn = _btn("Resume", "resume") if paused else _btn("Pause", "pause")
        kb = [
            [_btn("Positions", "pos"), _btn("Trades", "trades")],
            [_btn("Settings", "settings"), _btn("Stats", "stats")],
            [pause_btn, _btn("Refresh", "refresh")],
        ]
        return text, kb

    async def _render_positions(self) -> tuple[str, list]:
        positions = self.bot.risk.positions
        if not positions:
            text = "<b>Open Positions</b>\n\nNo open positions."
            kb = [[_btn("Home", "home"), _btn("Refresh", "refresh")]]
            return text, kb

        lines = [f"<b>Open Positions ({len(positions)})</b>\n"]
        kb = []
        for i, (mint, pos) in enumerate(positions.items(), 1):
            token = await self.bot.market.get_token(mint)
            pnl = pos.pnl_pct(token.price_usd) if token else 0
            emoji = "+" if pnl >= 0 else ""
            held = f"{pos.age_min:.0f}m"
            sl = self.bot.cfg.momentum_stop_loss_pct
            tp = self.bot.cfg.take_profit_pct

            lines.append(
                f"{i}. <b>${pos.symbol}</b> {emoji}{pnl:.0f}%\n"
                f"   {pos.sol_invested:.4f} SOL | {held} | {pos.strategy}\n"
                f"   SL: {sl:.0f}% | TP: {tp:.0f}%"
            )

            mint8 = mint[:8]
            kb.append([
                _btn(f"Sell {pos.symbol} 50%", f"sell_{mint8}_50"),
                _btn(f"Sell {pos.symbol} 100%", f"sell_{mint8}_100"),
            ])

        text = "\n".join(lines)
        kb.append([_btn("Sell All", "sellall"), _btn("Home", "home")])
        return text, kb

    def _render_trades(self) -> tuple[str, list]:
        sells = [t for t in self.bot.db.get_recent_trades(20) if t["action"] == "sell"][:8]
        if not sells:
            text = "<b>Recent Trades</b>\n\nNo recent trades."
            kb = [[_btn("Home", "home"), _btn("Refresh", "refresh")]]
            return text, kb

        lines = ["<b>Recent Trades</b>\n"]
        now = time.time()
        for t in sells:
            pnl = t.get("pnl_sol", 0) or 0
            pnl_pct = t.get("pnl_pct", 0) or 0
            emoji = "+" if pnl >= 0 else ""
            reason = t.get("reason", "")
            age_sec = now - t.get("timestamp", now)
            if age_sec < 3600:
                age_str = f"{age_sec / 60:.0f}m ago"
            else:
                age_str = f"{age_sec / 3600:.1f}h ago"
            strategy = t.get("strategy", "")
            lines.append(
                f"{'OK' if pnl >= 0 else 'X'} <b>${t['symbol']}</b> {emoji}{pnl:+.4f} SOL ({pnl_pct:+.0f}%)\n"
                f"   {strategy} | {reason} | {age_str}"
            )

        text = "\n".join(lines)
        kb = [[_btn("Home", "home"), _btn("Refresh", "refresh")]]
        return text, kb

    def _render_stats(self) -> tuple[str, list]:
        status = self.bot.risk.get_status()
        trades_all = self.bot.db.get_recent_trades(200)
        sells = [t for t in trades_all if t["action"] == "sell"]

        now = time.time()
        day_ago = now - 86400
        week_ago = now - 604800

        day_sells = [t for t in sells if t.get("timestamp", 0) > day_ago]
        week_sells = [t for t in sells if t.get("timestamp", 0) > week_ago]

        day_pnl = sum(t.get("pnl_sol", 0) or 0 for t in day_sells)
        week_pnl = sum(t.get("pnl_sol", 0) or 0 for t in week_sells)
        day_wins = sum(1 for t in day_sells if (t.get("pnl_sol", 0) or 0) > 0)
        week_wins = sum(1 for t in week_sells if (t.get("pnl_sol", 0) or 0) > 0)
        day_wr = f"{day_wins}/{len(day_sells)}" if day_sells else "0/0"
        week_wr = f"{week_wins}/{len(week_sells)}" if week_sells else "0/0"

        text = "<b>Bot Statistics</b>\n\n"
        text += f"Today: {len(day_sells)} trades | {day_pnl:+.4f} SOL\n"
        text += f"Week: {len(week_sells)} trades | {week_pnl:+.4f} SOL\n"
        text += f"Win Rate: {day_wr} (today) | {week_wr} (week)\n\n"

        # By strategy
        strat_stats: dict[str, list] = {}
        for t in week_sells:
            s = t.get("strategy", "unknown")
            strat_stats.setdefault(s, []).append(t.get("pnl_sol", 0) or 0)

        if strat_stats:
            text += "<b>By Strategy (week):</b>\n"
            for s, pnls in sorted(strat_stats.items()):
                text += f"  {s}: {len(pnls)} trades | {sum(pnls):+.4f} SOL\n"

        kb = [[_btn("Home", "home"), _btn("Refresh", "refresh")]]
        return text, kb

    def _render_settings(self) -> tuple[str, list]:
        cfg = self.bot.cfg
        text = "<b>Bot Settings</b>\n\n"
        text += f"Stop Loss: {cfg.momentum_stop_loss_pct:.0f}%\n"
        text += f"Take Profit: {cfg.take_profit_pct:.0f}%\n"
        text += f"TP2: {cfg.take_profit_2_pct:.0f}%\n"
        text += f"Trailing: {cfg.trailing_stop_pct:.0f}%\n"
        text += f"Max Buy: {cfg.max_buy_sol} SOL\n"
        text += f"Confidence: {cfg.min_momentum_confidence:.0%}\n"
        text += f"Growth: {cfg.min_momentum_curve_growth:.0f}%\n"
        text += f"Growth Rate: {cfg.min_momentum_growth_rate:.0f}%/min\n"
        text += f"Max Hold: {cfg.max_hold_minutes}m\n"
        text += f"Slippage: {cfg.slippage_pct}%"

        n_cw = len(self.bot.cfg.copy_wallets)
        kb = [
            [_btn("Stop Loss", "set_sl"), _btn("TP", "set_tp")],
            [_btn("TP2", "set_tp2"), _btn("Trailing", "set_trail")],
            [_btn("Max Buy", "set_mb"), _btn("Confidence", "set_conf")],
            [_btn("Growth", "set_grow"), _btn("Rate", "set_rate")],
            [_btn("Max Hold", "set_hold")],
            [_btn("Slippage", "set_slip"), _btn(f"Copy Wallets ({n_cw})", "copywallets")],
            [_btn("Strategies", "strategies"), _btn("Home", "home")],
        ]
        return text, kb

    def _render_setting_edit(self, param: str) -> tuple[str, list]:
        if param not in SETTINGS:
            return self._render_settings()
        cfg_attr, typ, presets, mn, mx, label = SETTINGS[param]
        current = getattr(self.bot.cfg, cfg_attr)

        if typ == float and current == int(current) and cfg_attr != "max_buy_sol":
            cur_str = f"{current:.0f}"
        elif cfg_attr == "max_buy_sol":
            cur_str = f"{current}"
        elif cfg_attr == "min_momentum_confidence":
            cur_str = f"{current:.0%}"
        else:
            cur_str = f"{current}"

        text = f"<b>Set {label}</b> (current: {cur_str})\n\nRange: {mn} - {mx}"

        # Preset buttons
        row = []
        for p in presets:
            if cfg_attr == "min_momentum_confidence":
                lbl = f"{p:.0%}"
            else:
                lbl = str(p)
            row.append(_btn(lbl, f"sv_{param}_{p}"))
        kb = [row, [_btn("Custom", f"cust_{param}"), _btn("Back", "settings")]]
        return text, kb

    def _render_pause_confirm(self) -> tuple[str, list]:
        text = (
            "<b>Pause Trading?</b>\n\n"
            "Bot will continue monitoring but won't execute "
            "any trades. Notifications will still be sent."
        )
        kb = [[_btn("Yes, Pause", "pause_yes"), _btn("Cancel", "home")]]
        return text, kb

    def _render_sellall_confirm(self) -> tuple[str, list]:
        n = len(self.bot.risk.positions)
        text = (
            f"<b>Sell All Positions?</b>\n\n"
            f"This will sell all {n} open position(s) at market price."
        )
        kb = [[_btn("Yes, Sell All", "sellall_yes"), _btn("Cancel", "pos")]]
        return text, kb

    # All strategies the bot can run (label, key)
    ALL_STRATEGIES = [
        ("Momentum", "momentum"),
        ("Graduation", "graduation"),
        ("Copy Trade", "copy_trade"),
        ("Sniper", "sniper"),
    ]

    def _render_strategies(self) -> tuple[str, list]:
        enabled = self.bot.strategies_enabled
        text = "<b>Strategies</b>\n\n"
        kb = []
        for label, key in self.ALL_STRATEGIES:
            on = key in enabled
            # Only show strategies that were initialized at boot
            initialized = hasattr(self.bot, {
                "momentum": "momentum",
                "graduation": "graduation",
                "copy_trade": "copy_trade",
                "sniper": "sniper",
            }.get(key, ""))
            status = "ON" if on else "OFF"
            marker = "[x]" if on else "[ ]"
            text += f"{marker} {label}\n"
            if initialized:
                btn_label = f"{'Disable' if on else 'Enable'} {label}"
                kb.append([_btn(btn_label, f"strat_{key}")])
            else:
                kb.append([_btn(f"{label} (not loaded)", "strategies")])
        kb.append([_btn("Back", "settings")])
        return text, kb

    def _toggle_strategy(self, key: str):
        valid = {k for _, k in self.ALL_STRATEGIES}
        if key not in valid:
            return
        if key in self.bot.strategies_enabled:
            self.bot.strategies_enabled.discard(key)
            log.info(f"Strategy disabled via Telegram: {key}")
        else:
            # Only enable if the strategy object was initialized at boot
            attr = {"momentum": "momentum", "graduation": "graduation",
                    "copy_trade": "copy_trade", "sniper": "sniper"}.get(key)
            if attr and hasattr(self.bot, attr):
                self.bot.strategies_enabled.add(key)
                log.info(f"Strategy enabled via Telegram: {key}")

    def _render_copy_wallets(self) -> tuple[str, list]:
        wallets = self.bot.cfg.copy_wallets
        if not wallets:
            text = "<b>Copy Trade Wallets</b>\n\nNo wallets configured."
        else:
            lines = [f"<b>Copy Trade Wallets ({len(wallets)})</b>\n"]
            for i, w in enumerate(wallets, 1):
                lines.append(f"{i}. <code>{w[:6]}...{w[-4:]}</code>")
            text = "\n".join(lines)

        kb = []
        for w in wallets:
            kb.append([
                _btn(f"{w[:6]}...{w[-4:]}", "copywallets"),
                _btn("Remove", f"cw_rm_{w[:8]}"),
            ])
        kb.append([_btn("Add Wallet", "cw_add"), _btn("Back", "settings")])
        return text, kb

    # ── Action handlers ──────────────────────────────────────

    async def _remove_copy_wallet(self, chat_id: str, prefix: str, message_id: int):
        """Remove a copy wallet by its 8-char prefix."""
        target = None
        for w in self.bot.cfg.copy_wallets:
            if w.startswith(prefix):
                target = w
                break
        if not target:
            text = "Wallet not found."
            kb = [[_btn("Back", "copywallets")]]
            self.bot.notify.edit_message(chat_id, message_id, text, kb)
            return

        # Remove from config
        self.bot.cfg.copy_wallets.remove(target)
        # Remove from CopyTradeStrategy if active
        if hasattr(self.bot, 'copy_trade'):
            self.bot.copy_trade.wallets.discard(target)
        # Unsubscribe from WS
        try:
            asyncio.get_event_loop().create_task(
                self.bot.stream.unsubscribe_account(target)
            )
        except Exception:
            pass
        log.info(f"Copy wallet removed via Telegram: {target[:16]}...")

        text = f"Removed <code>{target[:6]}...{target[-4:]}</code>"
        kb = [[_btn("Back", "copywallets"), _btn("Home", "home")]]
        self.bot.notify.edit_message(chat_id, message_id, text, kb)

    async def _add_copy_wallet(self, chat_id: str, address: str):
        """Add a copy wallet from text input."""
        st = self._state(chat_id)
        st.awaiting_input = ""

        # Basic validation: Solana addresses are 32-44 base58 chars
        address = address.strip()
        if len(address) < 32 or len(address) > 44 or not all(
            c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
            for c in address
        ):
            msg = "Invalid Solana address. Try again or cancel."
            kb = [[_btn("Cancel", "copywallets")]]
            self.bot.notify.edit_message(chat_id, st.menu_message_id, msg, kb)
            st.awaiting_input = "cw_add"
            return

        if address in self.bot.cfg.copy_wallets:
            msg = "Wallet already in list."
            kb = [[_btn("Back", "copywallets")]]
            self.bot.notify.edit_message(chat_id, st.menu_message_id, msg, kb)
            return

        # Add to config
        self.bot.cfg.copy_wallets.append(address)
        # Add to CopyTradeStrategy if active
        if hasattr(self.bot, 'copy_trade'):
            self.bot.copy_trade.wallets.add(address)
        # Subscribe on WS
        try:
            asyncio.get_event_loop().create_task(
                self.bot.stream.subscribe_account(address)
            )
        except Exception:
            pass
        log.info(f"Copy wallet added via Telegram: {address[:16]}...")

        msg = f"Added <code>{address[:6]}...{address[-4:]}</code>"
        kb = [[_btn("Back", "copywallets"), _btn("Home", "home")]]
        self.bot.notify.edit_message(chat_id, st.menu_message_id, msg, kb)

    async def _save_setting(self, chat_id: str, data: str, message_id: int):
        """Parse sv_<param>_<value> and apply."""
        # data format: sv_sl_15 or sv_mb_0.03
        parts = data.split("_", 2)  # ["sv", "sl", "15"]
        if len(parts) < 3:
            return
        param = parts[1]
        val_str = parts[2]
        if param not in SETTINGS:
            return

        cfg_attr, typ, _, mn, mx, label = SETTINGS[param]
        try:
            val = typ(val_str)
        except (ValueError, TypeError):
            return

        if val < mn or val > mx:
            return

        old_val = getattr(self.bot.cfg, cfg_attr)
        setattr(self.bot.cfg, cfg_attr, val)

        if typ == float and val == int(val) and cfg_attr not in ("max_buy_sol", "min_momentum_confidence"):
            new_str = f"{val:.0f}"
        elif cfg_attr == "min_momentum_confidence":
            new_str = f"{val:.0%}"
        else:
            new_str = str(val)

        text = f"<b>{label}</b> updated: {old_val} -> {new_str}"
        kb = [[_btn("Back to Settings", "settings"), _btn("Home", "home")]]
        self.bot.notify.edit_message(chat_id, message_id, text, kb)
        log.info(f"Setting changed via Telegram: {cfg_attr} = {val}")

    async def _handle_text_input(self, chat_id: str, text: str):
        """Handle free-text input when awaiting a custom setting value."""
        st = self._state(chat_id)
        if not st.awaiting_input:
            return

        # Copy wallet add flow
        if st.awaiting_input == "cw_add":
            await self._add_copy_wallet(chat_id, text)
            return

        param = st.awaiting_input
        if param not in SETTINGS:
            st.awaiting_input = ""
            return

        cfg_attr, typ, _, mn, mx, label = SETTINGS[param]
        try:
            val = typ(text)
        except (ValueError, TypeError):
            msg = f"Invalid value. Enter a number ({mn}-{mx}):"
            kb = [[_btn("Cancel", "settings")]]
            self.bot.notify.edit_message(chat_id, st.menu_message_id, msg, kb)
            return

        if val < mn or val > mx:
            msg = f"Out of range. Enter a value between {mn} and {mx}:"
            kb = [[_btn("Cancel", "settings")]]
            self.bot.notify.edit_message(chat_id, st.menu_message_id, msg, kb)
            return

        old_val = getattr(self.bot.cfg, cfg_attr)
        setattr(self.bot.cfg, cfg_attr, val)
        st.awaiting_input = ""

        msg = f"<b>{label}</b> updated: {old_val} -> {val}"
        kb = [[_btn("Back to Settings", "settings"), _btn("Home", "home")]]
        self.bot.notify.edit_message(chat_id, st.menu_message_id, msg, kb)
        log.info(f"Setting changed via Telegram (custom): {cfg_attr} = {val}")

    async def _handle_sell(self, chat_id: str, data: str, message_id: int):
        """Handle sell_<mint8>_<pct> callbacks."""
        # data: sell_AbCdEfGh_50
        parts = data.split("_")
        if len(parts) < 3:
            return
        mint8 = parts[1]
        try:
            sell_pct = int(parts[2])
        except ValueError:
            return

        # Find position by mint prefix
        target_mint = None
        for mint in self.bot.risk.positions:
            if mint.startswith(mint8):
                target_mint = mint
                break

        if not target_mint:
            text = f"Position not found (may have already been sold)."
            kb = [[_btn("Positions", "pos"), _btn("Home", "home")]]
            self.bot.notify.edit_message(chat_id, message_id, text, kb)
            return

        pos = self.bot.risk.positions[target_mint]
        text = f"Selling {sell_pct}% of ${pos.symbol}..."
        self.bot.notify.edit_message(chat_id, message_id, text, [])

        try:
            await self.bot._execute_sell(target_mint, sell_pct, "manual_telegram")
            text = f"Sold {sell_pct}% of ${pos.symbol}."
        except Exception as e:
            text = f"Sell failed: {e}"
            log.warning(f"Telegram manual sell error: {e}")

        kb = [[_btn("Positions", "pos"), _btn("Home", "home")]]
        self.bot.notify.edit_message(chat_id, message_id, text, kb)

    async def _execute_sell_all(self, chat_id: str, message_id: int):
        """Sell all open positions."""
        mints = list(self.bot.risk.positions.keys())
        if not mints:
            text = "No positions to sell."
            kb = [[_btn("Home", "home")]]
            self.bot.notify.edit_message(chat_id, message_id, text, kb)
            return

        text = f"Selling all {len(mints)} positions..."
        self.bot.notify.edit_message(chat_id, message_id, text, [])

        sold = 0
        for mint in mints:
            try:
                await self.bot._execute_sell(mint, 100, "manual_sell_all")
                sold += 1
            except Exception as e:
                log.warning(f"Sell all error for {mint[:8]}: {e}")

        text = f"Sold {sold}/{len(mints)} positions."
        kb = [[_btn("Positions", "pos"), _btn("Home", "home")]]
        self.bot.notify.edit_message(chat_id, message_id, text, kb)
