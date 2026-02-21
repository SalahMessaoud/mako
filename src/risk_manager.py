"""Risk management — position tracking, exits, loss cooldown, dynamic sizing, persistence."""

import time
import logging
from dataclasses import dataclass
from config import Config
from db import Database

log = logging.getLogger(__name__)


@dataclass
class Position:
    mint: str
    symbol: str
    entry_price: float
    entry_sol_price: float
    tokens: float
    sol_invested: float
    entry_time: float
    strategy: str
    peak_price: float = 0.0
    partial_taken: bool = False
    auto_sell_at: float = 0.0
    tx_signature: str = ""
    creator: str = ""
    graduated: bool = False
    graduated_at: float = 0.0       # timestamp when graduation detected
    tp2_taken: bool = False         # True after TP2 partial on graduated tokens
    curve_pct: float = 0.0          # bonding curve % (updated from polls)
    curve_pct_prev: float = 0.0     # previous curve % (for trend detection)
    curve_updated_at: float = 0.0   # last time curve_pct was updated
    original_sol_invested: float = 0.0  # original buy amount (never reduced)
    partial_sol_received: float = 0.0   # total SOL received from partial sells
    stop_loss_first_seen: float = 0.0   # timestamp when price first hit stop threshold

    @property
    def age_sec(self) -> float: return time.time() - self.entry_time
    @property
    def age_min(self) -> float: return self.age_sec / 60

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price <= 0: return 0.0
        return ((current_price / self.entry_price) - 1) * 100

    def update_peak(self, current_price: float):
        if current_price > self.peak_price:
            self.peak_price = current_price

    def drop_from_peak_pct(self, current_price: float) -> float:
        if self.peak_price <= 0: return 0.0
        return ((current_price / self.peak_price) - 1) * 100

    def update_curve(self, curve_pct: float):
        """Update bonding curve progress (called from graduation/momentum polls)."""
        self.curve_pct_prev = self.curve_pct
        self.curve_pct = curve_pct
        self.curve_updated_at = time.time()

    @property
    def curve_rising(self) -> bool:
        """True if curve is climbing (approaching graduation)."""
        return (self.curve_pct > 0 and self.curve_pct_prev > 0
                and self.curve_pct >= self.curve_pct_prev)

    @property
    def in_graduation_grace(self) -> bool:
        """True during 5-min grace period after graduation (Raydium migration dip)."""
        return (self.graduated and self.graduated_at > 0
                and (time.time() - self.graduated_at) < 300)


class RiskManager:
    """
    Advanced risk management with:
    - Hard stop-loss / take-profit / trailing stop
    - Loss streak cooldowns (slow down after consecutive losses)
    - Dynamic position sizing (by confidence + strategy performance)
    - SQLite persistence (positions survive crashes)
    - Daily circuit breaker
    """

    def __init__(self, config: Config, db: Database):
        self.cfg = config
        self.db = db
        self.positions: dict[str, Position] = {}
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.closed_trades: list[dict] = []

        # Load persisted daily pnl
        self.daily_pnl = db.get_daily_pnl()

    # ── Crash Recovery ────────────────────────────────────────

    def recover_positions(self) -> int:
        """Load positions from database after crash/restart."""
        saved = self.db.get_all_positions()
        recovered = 0
        for row in saved:
            extra = {}
            try:
                import json
                extra = json.loads(row.get("extra", "{}") or "{}")
            except Exception:
                pass
            pos = Position(
                mint=row["mint"], symbol=row["symbol"],
                entry_price=row["entry_price"],
                entry_sol_price=row["entry_sol_price"],
                tokens=row["tokens"], sol_invested=row["sol_invested"],
                entry_time=row["entry_time"], strategy=row["strategy"],
                peak_price=row["peak_price"],
                partial_taken=bool(row["partial_taken"]),
                auto_sell_at=row["auto_sell_at"],
                tx_signature=row.get("tx_signature", ""),
                creator=extra.get("creator", ""),
                original_sol_invested=extra.get("original_sol_invested", row["sol_invested"]),
                partial_sol_received=extra.get("partial_sol_received", 0.0),
            )
            self.positions[pos.mint] = pos
            recovered += 1
            log.info(f"  Recovered: {pos.symbol} | {pos.sol_invested:.4f} SOL | "
                     f"age {pos.age_min:.0f}m | {pos.strategy}")
        if recovered:
            log.info(f"  Recovered {recovered} positions from database")
        return recovered

    # ── Position Management ───────────────────────────────────

    def can_open(self, sol_amount: float, strategy: str = "") -> tuple[bool, str]:
        if self.daily_pnl <= -self.cfg.daily_loss_limit:
            return False, f"Daily loss limit ({self.daily_pnl:.4f} SOL)"
        if (self.cfg.max_daily_trades > 0
                and self.daily_trades >= self.cfg.max_daily_trades):
            return False, f"Daily trade cap ({self.daily_trades}/{self.cfg.max_daily_trades})"
        if len(self.positions) >= self.cfg.max_concurrent:
            return False, f"Max positions ({self.cfg.max_concurrent})"
        total = sum(p.sol_invested for p in self.positions.values())
        if total + sol_amount > self.cfg.max_portfolio_sol:
            return False, f"Portfolio limit ({total:.3f} + {sol_amount:.3f})"

        # Loss cooldown check (per-strategy)
        if strategy:
            on_cd, secs = self.db.is_on_cooldown(strategy)
            if on_cd:
                return False, f"Loss cooldown: {strategy} ({secs:.0f}s remaining)"

        # Global cooldown: if total recent losses across ALL strategies >= trigger
        global_on_cd, global_secs = self.db.is_on_cooldown("_global")
        if global_on_cd:
            return False, f"Global loss cooldown ({global_secs:.0f}s remaining)"

        return True, "ok"

    def calc_size(self, sol_balance: float, confidence: float = 0.5,
                  strategy: str = "", wallet: str = "") -> float:
        """
        Dynamic position sizing based on:
        - Available balance
        - Signal confidence (0-1)
        - Strategy recent performance
        - Loss streak (reduce size after losses)
        - Wallet score (for copy trades)
        """
        total = sum(p.sol_invested for p in self.positions.values())
        remaining = self.cfg.max_portfolio_sol - total
        # Strategy-specific size caps
        if strategy == "momentum":
            cap = self.cfg.momentum_max_buy_sol
        elif strategy == "graduation":
            cap = self.cfg.graduation_max_buy_sol
        elif strategy == "copy_trade":
            cap = self.cfg.copy_max_buy_sol
        else:
            cap = self.cfg.max_buy_sol
        base = min(cap, sol_balance * 0.15, remaining)

        # Confidence multiplier: 0.5x at low confidence, 1.5x at high
        conf_mult = 0.5 + confidence  # 0.5 to 1.5

        # Strategy performance multiplier
        strat_mult = 1.0
        if strategy:
            stats = self.db.get_strategy_stats(strategy, hours=24)
            if stats["trades"] >= 5:
                wr = stats["win_rate"]
                if wr >= 60: strat_mult = 1.3
                elif wr >= 50: strat_mult = 1.1
                elif wr < 35: strat_mult = 0.6
                elif wr < 45: strat_mult = 0.8

            # Loss streak reduction
            streak = self.db.get_loss_streak(strategy)
            if streak >= 5: strat_mult *= 0.4
            elif streak >= 3: strat_mult *= 0.6
            elif streak >= 2: strat_mult *= 0.8

        # Wallet score multiplier (copy trades only)
        wallet_mult = 1.0
        if wallet and strategy == "copy_trade":
            ws = self.db.get_wallet_score(wallet)
            if ws and ws["total_trades"] >= 3:
                # Scale: score 0-100 → multiplier 0.25-1.5
                wallet_mult = max(0.25, min(1.5, ws["score"] / 50))
                if ws["streak"] <= -3:
                    # 3+ consecutive losses → hard cap at 0.25x
                    wallet_mult = 0.25
                    log.info(f"  Wallet {wallet[:8]}... on loss streak "
                             f"({ws['streak']}) — 0.25x size")

        size = base * conf_mult * strat_mult * wallet_mult
        size = min(size, cap, remaining)
        return max(size, 0)

    def open(self, mint: str, symbol: str, price_usd: float,
             price_sol: float, tokens: float, sol: float,
             strategy: str, auto_sell_sec: int = 0,
             tx_signature: str = "", creator: str = "") -> Position:
        if mint in self.positions:
            return self.positions[mint]
        pos = Position(
            mint=mint, symbol=symbol, entry_price=price_usd,
            entry_sol_price=price_sol, tokens=tokens,
            sol_invested=sol, entry_time=time.time(),
            strategy=strategy, peak_price=price_usd,
            auto_sell_at=(time.time() + auto_sell_sec) if auto_sell_sec > 0 else 0,
            tx_signature=tx_signature, creator=creator,
            original_sol_invested=sol,
        )
        self.positions[mint] = pos
        self.daily_trades += 1

        # Persist to DB
        self.db.save_position(
            mint=mint, symbol=symbol, entry_price=price_usd,
            entry_sol_price=price_sol, tokens=tokens, sol_invested=sol,
            entry_time=pos.entry_time, strategy=strategy,
            peak_price=price_usd, partial_taken=False,
            auto_sell_at=pos.auto_sell_at, tx_signature=tx_signature,
            extra={"creator": creator} if creator else None,
        )
        self.db.log_trade(
            mint=mint, symbol=symbol, strategy=strategy, action="buy",
            sol_amount=sol, tokens=tokens, price_usd=price_usd,
            tx_signature=tx_signature,
        )

        log.info(f"  Position opened: {symbol} | {sol:.4f} SOL | "
                 f"${price_usd:.10f} | {strategy}")
        return pos

    def close(self, mint: str, sol_received: float, reason: str = "",
              tx_signature: str = "") -> float:
        pos = self.positions.pop(mint, None)
        if not pos:
            return 0.0

        # Total PnL = all SOL received (partials + final) - original investment
        total_received = pos.partial_sol_received + sol_received
        original_cost = pos.original_sol_invested or pos.sol_invested
        pnl = total_received - original_cost
        pnl_pct = (pnl / original_cost * 100) if original_cost > 0 else 0
        self.daily_pnl += pnl
        self.closed_trades.append({
            "symbol": pos.symbol, "mint": mint, "strategy": pos.strategy,
            "pnl_sol": pnl, "pnl_pct": pnl_pct, "held_min": pos.age_min,
            "time": time.time(),
        })
        # Keep only last 200 trades in memory (full history in DB)
        if len(self.closed_trades) > 200:
            self.closed_trades = self.closed_trades[-200:]

        # Persist
        self.db.delete_position(mint)
        self.db.log_trade(
            mint=mint, symbol=pos.symbol, strategy=pos.strategy,
            action="sell", sol_amount=sol_received, tokens=pos.tokens,
            price_usd=pos.entry_price, pnl_sol=pnl, pnl_pct=pnl_pct,
            reason=reason, tx_signature=tx_signature,
        )

        # Loss streak tracking (per-strategy + global)
        if pnl >= 0:
            self.db.record_win(pos.strategy)
            self.db.record_win("_global")
        else:
            losses = self.db.record_loss(
                pos.strategy,
                trigger=self.cfg.cooldown_trigger_losses,
                duration=self.cfg.cooldown_duration_sec,
            )
            if losses >= self.cfg.cooldown_trigger_losses:
                log.warning(f"  {pos.strategy} loss streak: {losses} — cooldown active")
            # Global cooldown across all strategies
            global_losses = self.db.record_loss(
                "_global",
                trigger=self.cfg.cooldown_trigger_losses,
                duration=self.cfg.cooldown_duration_sec * 2,  # 2x duration
            )
            if global_losses >= self.cfg.cooldown_trigger_losses:
                log.warning(f"  GLOBAL loss streak: {global_losses} — cooldown active")

        emoji = "+" if pnl >= 0 else ""
        log.info(f"  Closed {pos.symbol}: {emoji}{pnl:.4f} SOL "
                 f"({pnl_pct:+.1f}%) held {pos.age_min:.1f}min [{reason}]")
        return pnl

    def partial_close(self, mint: str, sol_received: float = 0,
                      reason: str = "") -> bool:
        pos = self.positions.get(mint)
        if not pos:
            return False
        if not pos.partial_taken:
            pos.partial_taken = True
        if sol_received > 0:
            pos.partial_sol_received += sol_received
            # Log partial sell as a trade
            self.db.log_trade(
                mint=mint, symbol=pos.symbol, strategy=pos.strategy,
                action="partial_sell", sol_amount=sol_received,
                tokens=0, price_usd=pos.entry_price,
                pnl_sol=0, pnl_pct=0, reason=reason,
            )
            log.info(f"  Partial sell {pos.symbol}: +{sol_received:.4f} SOL [{reason}] "
                     f"(total partials: {pos.partial_sol_received:.4f} SOL)")
        import json
        extra = json.dumps({
            "creator": pos.creator,
            "original_sol_invested": pos.original_sol_invested,
            "partial_sol_received": pos.partial_sol_received,
        })
        self.db.update_position(mint, partial_taken=1, extra=extra)
        return True

    # ── Exit Signal Checks ────────────────────────────────────

    def check_exits(self, mint: str, current_price: float) -> tuple[str, float]:
        pos = self.positions.get(mint)
        if not pos:
            return "none", 0

        pos.update_peak(current_price)
        pnl = pos.pnl_pct(current_price)
        drop = pos.drop_from_peak_pct(current_price)

        # Persist peak price updates
        if current_price >= pos.peak_price:
            self.db.update_position(mint, peak_price=pos.peak_price)

        # Auto-sell timer
        if pos.auto_sell_at > 0 and time.time() >= pos.auto_sell_at:
            return "auto_sell", 100

        # Hard stop-loss (momentum gets wider stop)
        stop = (self.cfg.momentum_stop_loss_pct
                if pos.strategy == "momentum"
                else self.cfg.stop_loss_pct)

        # No-stop window: meme coins swing 15-20% routinely early on.
        # Only catastrophic override active during this window.
        in_nostop_window = pos.age_sec < self.cfg.nostop_window_sec
        if in_nostop_window:
            stop = stop * self.cfg.nostop_multiplier

        # Curve-aware stop-loss: if curve is >60% and still rising toward
        # graduation, suppress the price-based stop-loss. The token is
        # filling up — price dips are temporary while buyers accumulate.
        curve_protected = (
            pos.curve_pct > 60
            and pos.curve_rising
            and not pos.graduated
            and (time.time() - pos.curve_updated_at) < 30  # fresh data
        )

        # Post-graduation grace: 5 min after graduation, suppress stop-loss
        # to survive the Raydium migration dip (price always drops temporarily).
        grad_grace = pos.in_graduation_grace

        if curve_protected or grad_grace:
            # Only exit on catastrophic dumps
            if pnl <= -80:
                return "stop_loss", 100
            if curve_protected:
                curve_drop = pos.curve_pct_prev - pos.curve_pct
                if curve_drop > 15:
                    return "curve_dump", 100
            # Otherwise, let it ride through the dip
            pos.stop_loss_first_seen = 0.0  # reset while protected
        elif pnl <= -stop:
            now = time.time()
            # Confirm dip: require price to stay below stop for 8s
            # Filters momentary volatility spikes on pump.fun
            if pos.stop_loss_first_seen == 0.0:
                pos.stop_loss_first_seen = now
            if now - pos.stop_loss_first_seen >= 8:
                return "stop_loss", 100
            # Catastrophic dump: exit immediately if -3x the stop threshold
            if pnl <= -(stop * 3):
                return "stop_loss", 100
        else:
            # Price recovered above stop — reset confirmation timer
            pos.stop_loss_first_seen = 0.0

        # First take-profit: partial
        if pnl >= self.cfg.take_profit_pct and not pos.partial_taken:
            return "take_profit_partial", self.cfg.partial_take_pct

        # Second take-profit
        if pnl >= self.cfg.take_profit_2_pct and pos.partial_taken and not pos.tp2_taken:
            if pos.graduated:
                # Post-graduation hold: sell only 50% at TP2, let the rest ride
                # with a wider trailing stop. Graduated tokens with volume can 10x+.
                pos.tp2_taken = True
                return "take_profit_graduated", 50
            return "take_profit_full", 100

        # Post-graduation wide trailing: after TP2 partial on graduated tokens,
        # use a configurable drop-from-peak threshold (default 35%).
        if pos.tp2_taken and pnl > 50 and drop <= -self.cfg.graduated_trailing_2_pct:
            return "trailing_stop_graduated", 100

        # Tighter trailing stop after partial profit taken (protect winners)
        # Graduated tokens get 30% trailing — post-migration volatility is normal
        tight_trail = -30 if pos.graduated else -12
        if pos.partial_taken and not pos.tp2_taken and pnl > 10 and drop <= tight_trail:
            return "trailing_stop_tight", 100

        # Trailing stop (activates once position has gained meaningfully)
        # Graduated tokens get wider trailing (30%) — regular trailing_stop must
        # not fire before trailing_stop_tight's graduated threshold
        trail_pct = max(self.cfg.trailing_stop_pct, 30) if pos.graduated else self.cfg.trailing_stop_pct
        if pnl > 20 and drop <= -trail_pct:
            return "trailing_stop", 100

        # Stale position exit: flat after N min = dead money
        stale_min = self.cfg.stale_exit_minutes
        stale_pct = self.cfg.stale_exit_pct
        if pos.age_min >= stale_min and -stale_pct <= pnl <= stale_pct:
            return "stale_exit", 100

        # Time-based exit
        if self.cfg.max_hold_minutes > 0 and pos.age_min >= self.cfg.max_hold_minutes:
            return "time_exit", 100

        return "none", 0

    # ── Reporting ─────────────────────────────────────────────

    def get_status(self) -> dict:
        invested = sum(p.sol_invested for p in self.positions.values())
        wins = sum(1 for t in self.closed_trades if t["pnl_sol"] > 0)
        total = len(self.closed_trades)
        return {
            "positions": len(self.positions),
            "invested_sol": invested,
            "daily_pnl": self.daily_pnl,
            "daily_trades": self.daily_trades,
            "win_rate": f"{wins}/{total}" if total > 0 else "0/0",
            "avg_pnl_pct": (sum(t["pnl_pct"] for t in self.closed_trades) / total) if total > 0 else 0,
        }

    def reset_daily(self):
        log.info(f"  Day end: PnL={self.daily_pnl:+.4f} SOL | Trades={self.daily_trades}")
        self.daily_pnl = 0.0
        self.daily_trades = 0
