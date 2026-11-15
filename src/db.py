"""Persistent storage via SQLite — positions, trades, wallet scores survive crashes."""

import sqlite3
import json
import time
import logging
import os
import threading
from dataclasses import asdict

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "bot_state.db"


class Database:
    """SQLite persistence for bot state. Zero cost, zero dependencies."""

    def __init__(self, path: str | None = None):
        # Resolved at construction, not import, so --db actually takes effect.
        self.path = path or os.getenv("BOT_DB_PATH", DEFAULT_DB_PATH)
        path = self.path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")  # better concurrent access
        self._init_tables()
        log.info(f"Database initialized: {path}")

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                mint TEXT PRIMARY KEY,
                symbol TEXT,
                entry_price REAL,
                entry_sol_price REAL,
                tokens REAL,
                sol_invested REAL,
                entry_time REAL,
                strategy TEXT,
                peak_price REAL DEFAULT 0,
                partial_taken INTEGER DEFAULT 0,
                auto_sell_at REAL DEFAULT 0,
                tx_signature TEXT DEFAULT '',
                extra TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT,
                symbol TEXT,
                strategy TEXT,
                action TEXT,
                sol_amount REAL,
                tokens REAL,
                price_usd REAL,
                pnl_sol REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                reason TEXT DEFAULT '',
                tx_signature TEXT DEFAULT '',
                timestamp REAL
            );

            CREATE TABLE IF NOT EXISTS wallet_scores (
                wallet TEXT PRIMARY KEY,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                total_pnl_sol REAL DEFAULT 0,
                last_trade_time REAL DEFAULT 0,
                avg_hold_sec REAL DEFAULT 0,
                tokens_traded TEXT DEFAULT '[]',
                streak INTEGER DEFAULT 0,
                score REAL DEFAULT 50.0
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            );

            CREATE TABLE IF NOT EXISTS token_blacklist (
                mint TEXT PRIMARY KEY,
                reason TEXT,
                added_at REAL
            );

            CREATE TABLE IF NOT EXISTS creator_blacklist (
                creator TEXT PRIMARY KEY,
                reason TEXT,
                added_at REAL
            );

            CREATE TABLE IF NOT EXISTS loss_streaks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT,
                consecutive_losses INTEGER DEFAULT 0,
                last_loss_time REAL DEFAULT 0,
                cooldown_until REAL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
        """)
        self.conn.commit()

    # ── Positions ─────────────────────────────────────────────

    def save_position(self, mint: str, symbol: str, entry_price: float,
                      entry_sol_price: float, tokens: float, sol_invested: float,
                      entry_time: float, strategy: str, peak_price: float = 0,
                      partial_taken: bool = False, auto_sell_at: float = 0,
                      tx_signature: str = "", extra: dict = None):
        with self._lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO positions
                (mint, symbol, entry_price, entry_sol_price, tokens, sol_invested,
                 entry_time, strategy, peak_price, partial_taken, auto_sell_at,
                 tx_signature, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (mint, symbol, entry_price, entry_sol_price, tokens, sol_invested,
                  entry_time, strategy, peak_price, int(partial_taken), auto_sell_at,
                  tx_signature, json.dumps(extra or {})))
            self.conn.commit()

    _POSITION_COLUMNS = {
        "symbol", "entry_price", "entry_sol_price", "tokens", "sol_invested",
        "entry_time", "strategy", "peak_price", "partial_taken", "auto_sell_at",
        "tx_signature", "extra",
    }

    def update_position(self, mint: str, **kwargs):
        if not kwargs:
            return
        # Whitelist column names to prevent SQL injection
        safe_kwargs = {k: v for k, v in kwargs.items() if k in self._POSITION_COLUMNS}
        if not safe_kwargs:
            log.warning(f"update_position: invalid columns {set(kwargs) - self._POSITION_COLUMNS}")
            return
        sets = ", ".join(f"{k} = ?" for k in safe_kwargs)
        vals = list(safe_kwargs.values()) + [mint]
        with self._lock:
            self.conn.execute(f"UPDATE positions SET {sets} WHERE mint = ?", vals)
            self.conn.commit()

    def delete_position(self, mint: str):
        with self._lock:
            self.conn.execute("DELETE FROM positions WHERE mint = ?", (mint,))
            self.conn.commit()

    def get_all_positions(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return [dict(r) for r in rows]

    def get_position(self, mint: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE mint = ?", (mint,)
        ).fetchone()
        return dict(row) if row else None

    def position_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    # ── Trade History ─────────────────────────────────────────

    def log_trade(self, mint: str, symbol: str, strategy: str, action: str,
                  sol_amount: float, tokens: float = 0, price_usd: float = 0,
                  pnl_sol: float = 0, pnl_pct: float = 0, reason: str = "",
                  tx_signature: str = ""):
        with self._lock:
            self.conn.execute("""
                INSERT INTO trades (mint, symbol, strategy, action, sol_amount,
                                   tokens, price_usd, pnl_sol, pnl_pct, reason,
                                   tx_signature, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (mint, symbol, strategy, action, sol_amount, tokens, price_usd,
                  pnl_sol, pnl_pct, reason, tx_signature, time.time()))
            self.conn.commit()

    def get_recent_trades(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_strategy_stats(self, strategy: str, hours: int = 24) -> dict:
        since = time.time() - hours * 3600
        rows = self.conn.execute("""
            SELECT action, COUNT(*) as cnt, SUM(pnl_sol) as total_pnl,
                   AVG(pnl_sol) as avg_pnl
            FROM trades WHERE strategy = ? AND timestamp > ? AND action = 'sell'
            GROUP BY action
        """, (strategy, since)).fetchall()
        if not rows:
            return {"trades": 0, "pnl": 0, "avg_pnl": 0, "win_rate": 0}
        row = rows[0]
        wins = self.conn.execute("""
            SELECT COUNT(*) FROM trades
            WHERE strategy = ? AND timestamp > ? AND action = 'sell' AND pnl_sol > 0
        """, (strategy, since)).fetchone()[0]
        total = row["cnt"]
        return {
            "trades": total, "pnl": row["total_pnl"] or 0,
            "avg_pnl": row["avg_pnl"] or 0,
            "win_rate": (wins / total * 100) if total > 0 else 0,
        }

    @staticmethod
    def _today_start() -> float:
        """Local midnight — the same day boundary the bot's daily reset uses."""
        import datetime
        midnight = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
        return midnight.timestamp()

    def get_daily_pnl(self) -> float:
        """Get today's total PnL."""
        row = self.conn.execute(
            "SELECT SUM(pnl_sol) FROM trades WHERE action='sell' AND timestamp > ?",
            (self._today_start(),)
        ).fetchone()
        return row[0] or 0.0

    def count_buys_today(self) -> int:
        """Buys opened today — restores the daily trade cap after a restart."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE action='buy' AND timestamp > ?",
            (self._today_start(),)
        ).fetchone()
        return row[0] or 0

    def get_recently_closed_mints(self, hours: int = 24) -> set[str]:
        """Mints fully sold recently — restores no-re-entry after a restart."""
        rows = self.conn.execute(
            "SELECT DISTINCT mint FROM trades WHERE action='sell' AND timestamp > ?",
            (time.time() - hours * 3600,)
        ).fetchall()
        return {r[0] for r in rows}

    # ── Wallet Scoring ────────────────────────────────────────

    def update_wallet_score(self, wallet: str, pnl_sol: float,
                            hold_sec: float, mint: str, won: bool):
        row = self.conn.execute(
            "SELECT * FROM wallet_scores WHERE wallet = ?", (wallet,)
        ).fetchone()

        if row:
            total = row["total_trades"] + 1
            wins = row["winning_trades"] + (1 if won else 0)
            pnl = row["total_pnl_sol"] + pnl_sol
            avg_hold = (row["avg_hold_sec"] * row["total_trades"] + hold_sec) / total
            tokens = json.loads(row["tokens_traded"])
            if mint not in tokens:
                tokens.append(mint)
            streak = (row["streak"] + 1) if won else min(row["streak"] - 1, 0)
            # Score: win_rate * 60 + profitability * 30 + streak * 10
            wr = wins / total * 100
            prof = min(max(pnl / max(total, 1) * 100, -50), 50) + 50  # normalize 0-100
            score = wr * 0.6 + prof * 0.3 + min(max(streak * 5 + 50, 0), 100) * 0.1
        else:
            total = 1; wins = 1 if won else 0; pnl = pnl_sol
            avg_hold = hold_sec; tokens = [mint]
            streak = 1 if won else -1; score = 50.0

        with self._lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO wallet_scores
                (wallet, total_trades, winning_trades, total_pnl_sol, last_trade_time,
                 avg_hold_sec, tokens_traded, streak, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (wallet, total, wins, pnl, time.time(), avg_hold,
                  json.dumps(tokens[-50:]), streak, score))
            self.conn.commit()

    def get_wallet_score(self, wallet: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM wallet_scores WHERE wallet = ?", (wallet,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_wallet_scores(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM wallet_scores ORDER BY score DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Loss Streak Tracking ──────────────────────────────────

    def record_loss(self, strategy: str, trigger: int = 5,
                    duration: int = 120) -> int:
        """Record a loss, return current consecutive loss count.

        Cooldown activates after `trigger` consecutive losses for `duration` seconds.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM loss_streaks WHERE strategy = ?", (strategy,)
            ).fetchone()
            if row:
                losses = row["consecutive_losses"] + 1
                # Only activate cooldown after trigger threshold
                cooldown_until = (time.time() + duration) if losses >= trigger else 0
                self.conn.execute("""
                    UPDATE loss_streaks SET consecutive_losses = ?,
                    last_loss_time = ?, cooldown_until = ?
                    WHERE id = ?
                """, (losses, time.time(), cooldown_until, row["id"]))
                # Clean up any duplicate rows for this strategy
                self.conn.execute("""
                    DELETE FROM loss_streaks
                    WHERE strategy = ? AND id != ?
                """, (strategy, row["id"]))
            else:
                losses = 1
                self.conn.execute("""
                    INSERT INTO loss_streaks (strategy, consecutive_losses,
                    last_loss_time, cooldown_until) VALUES (?, 1, ?, 0)
                """, (strategy, time.time()))
            self.conn.commit()
        return losses

    def record_win(self, strategy: str):
        """Reset loss streak on a win."""
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM loss_streaks WHERE strategy = ?", (strategy,)
            ).fetchone()
            if row:
                self.conn.execute("""
                    UPDATE loss_streaks SET consecutive_losses = 0,
                    last_loss_time = 0, cooldown_until = 0 WHERE strategy = ?
                """, (strategy,))
            else:
                self.conn.execute("""
                    INSERT INTO loss_streaks (strategy, consecutive_losses,
                    last_loss_time, cooldown_until) VALUES (?, 0, 0, 0)
                """, (strategy,))
            self.conn.commit()

    def is_on_cooldown(self, strategy: str) -> tuple[bool, float]:
        """Check if strategy is on loss cooldown. Returns (on_cooldown, seconds_remaining)."""
        row = self.conn.execute(
            "SELECT cooldown_until, consecutive_losses FROM loss_streaks WHERE strategy = ?",
            (strategy,)
        ).fetchone()
        if not row:
            return False, 0
        remaining = row["cooldown_until"] - time.time()
        return remaining > 0, max(remaining, 0)

    def get_loss_streak(self, strategy: str) -> int:
        row = self.conn.execute(
            "SELECT consecutive_losses FROM loss_streaks WHERE strategy = ?",
            (strategy,)
        ).fetchone()
        return row["consecutive_losses"] if row else 0

    # ── Bot State ─────────────────────────────────────────────

    def set_state(self, key: str, value):
        with self._lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO bot_state (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, json.dumps(value), time.time()))
            self.conn.commit()

    def get_state(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    # ── Blacklist ─────────────────────────────────────────────

    def blacklist_token(self, mint: str, reason: str = ""):
        with self._lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO token_blacklist (mint, reason, added_at)
                VALUES (?, ?, ?)
            """, (mint, reason, time.time()))
            self.conn.commit()

    def is_blacklisted(self, mint: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM token_blacklist WHERE mint = ?", (mint,)
        ).fetchone()
        return row is not None

    def blacklist_creator(self, creator: str, reason: str = ""):
        with self._lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO creator_blacklist (creator, reason, added_at)
                VALUES (?, ?, ?)
            """, (creator, reason, time.time()))
            self.conn.commit()

    def is_creator_blacklisted(self, creator: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM creator_blacklist WHERE creator = ?", (creator,)
        ).fetchone()
        return row is not None

    # ── Cleanup ───────────────────────────────────────────────

    def close(self):
        self.conn.close()

    def cleanup_old_data(self, days: int = 7):
        cutoff = time.time() - days * 86400
        with self._lock:
            self.conn.execute("DELETE FROM trades WHERE timestamp < ?", (cutoff,))
            self.conn.execute(
                "DELETE FROM token_blacklist WHERE added_at < ?", (cutoff,)
            )
            self.conn.execute(
                "DELETE FROM creator_blacklist WHERE added_at < ?",
                (time.time() - 30 * 86400,)
            )
            self.conn.commit()
        log.info(f"Cleaned data older than {days} days")
