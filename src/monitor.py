"""Live-updating terminal dashboard for Mako. Runs alongside the bot, read-only DB access.

Usage: python monitor.py [--db bot_state.db] [--interval 2]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time

# ── ANSI Constants ────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
WHITE = "\033[97m"
RESET = "\033[0m"
CLEAR = "\033[2J\033[H"

STRATEGIES = ["sniper", "momentum", "copy_trade", "graduation"]

# ── Shared Helpers ────────────────────────────────────────────────


def connect_db(path: str):
    """Open read-only SQLite connection. Returns None if DB missing."""
    if not os.path.exists(path):
        return None
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        return None


def format_age(ts: float) -> str:
    """Unix timestamp to '3m ago' style string."""
    if not ts or ts <= 0:
        return "n/a"
    delta = time.time() - ts
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def format_duration(seconds: float) -> str:
    """Seconds to '1h 20m' style string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    h = int(seconds / 3600)
    m = int((seconds % 3600) / 60)
    return f"{h}h {m}m" if m else f"{h}h"


def format_pnl(value: float, suffix: str = " SOL") -> str:
    """Colored +/- value."""
    if value > 0:
        return f"{GREEN}+{value:.4f}{suffix}{RESET}"
    if value < 0:
        return f"{RED}{value:.4f}{suffix}{RESET}"
    return f"{DIM}0.0000{suffix}{RESET}"


def format_pct(value: float) -> str:
    """Colored percentage."""
    if value > 0:
        return f"{GREEN}+{value:.1f}%{RESET}"
    if value < 0:
        return f"{RED}{value:.1f}%{RESET}"
    return f"{DIM}0.0%{RESET}"


def strip_ansi(text: str) -> int:
    """Return visible length of string (strip ANSI codes)."""
    return len(re.sub(r'\033\[[0-9;]*m', '', str(text)))


def print_table(headers: list[str], rows: list[list], indent: int = 2):
    """Print an aligned table with ANSI-aware column widths."""
    if not rows:
        print(f"{' ' * indent}{DIM}(no data){RESET}")
        return

    all_rows = [headers] + rows
    col_widths = []
    for col_idx in range(len(headers)):
        max_w = 0
        for row in all_rows:
            if col_idx < len(row):
                max_w = max(max_w, strip_ansi(row[col_idx]))
        col_widths.append(max_w)

    prefix = " " * indent

    # Header
    hdr = ""
    for i, h in enumerate(headers):
        hdr += f"{BOLD}{h:<{col_widths[i]}}{RESET}  "
    print(f"{prefix}{hdr}")
    print(f"{prefix}{DIM}{'─' * (sum(col_widths) + 2 * len(col_widths))}{RESET}")

    # Rows
    for row in rows:
        line = ""
        for i, cell in enumerate(row):
            visible = strip_ansi(cell)
            pad = col_widths[i] - visible
            line += f"{cell}{' ' * max(pad, 0)}  "
        print(f"{prefix}{line}")


# ── Dashboard Sections ────────────────────────────────────────────


def get_state(conn, key: str, default=None):
    """Get a value from bot_state table."""
    try:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default
    except (sqlite3.OperationalError, TypeError):
        return default


def section_header(conn):
    """Header: mode, last update, stale warning."""
    paper_bal = get_state(conn, "paper_balance")
    mode = f"{YELLOW}PAPER{RESET}" if paper_bal is not None else f"{RED}LIVE{RESET}"

    try:
        row = conn.execute("SELECT MAX(updated_at) as last FROM bot_state").fetchone()
        last_update = row["last"] if row and row["last"] else 0
    except sqlite3.OperationalError:
        last_update = 0

    stale = ""
    if last_update > 0:
        age = time.time() - last_update
        if age > 30:
            stale = f"  {RED}STALE ({int(age)}s){RESET}"
        update_str = format_age(last_update)
    else:
        update_str = "unknown"

    now = time.strftime("%H:%M:%S")
    print(f"{BOLD}{CYAN}  MAKO MONITOR{RESET}  |  {mode}  |  "
          f"Last DB update: {update_str}{stale}  |  {DIM}{now}{RESET}")
    print(f"{DIM}{'─' * 72}{RESET}")


def section_portfolio(conn):
    """Portfolio: balance, invested, daily P&L, overall P&L, ROI."""
    paper_bal = get_state(conn, "paper_balance")
    live_bal = get_state(conn, "live_balance")
    if paper_bal is not None:
        available = paper_bal
    elif live_bal is not None:
        available = live_bal
    else:
        available = None

    try:
        row = conn.execute("SELECT SUM(sol_invested) as total FROM positions").fetchone()
        invested = row["total"] or 0.0 if row else 0.0
    except sqlite3.OperationalError:
        invested = 0.0

    # Daily P&L (sells + partial_sells)
    today_start = time.time() - (time.time() % 86400)
    try:
        row = conn.execute(
            "SELECT SUM(pnl_sol) as pnl FROM trades "
            "WHERE action IN ('sell', 'partial_sell') AND timestamp > ?",
            (today_start,)
        ).fetchone()
        daily_pnl = row["pnl"] or 0.0 if row else 0.0
    except sqlite3.OperationalError:
        daily_pnl = 0.0

    # Overall P&L: use balance-based calculation (accurate even with partial sells)
    initial_balance = get_state(conn, "paper_initial_balance") or get_state(conn, "live_initial_balance")
    if initial_balance and available is not None:
        overall_pnl = (available + invested) - initial_balance
    else:
        # Fallback: sum from trades table
        try:
            row = conn.execute(
                "SELECT SUM(pnl_sol) as pnl FROM trades "
                "WHERE action IN ('sell', 'partial_sell')"
            ).fetchone()
            overall_pnl = row["pnl"] or 0.0 if row else 0.0
        except sqlite3.OperationalError:
            overall_pnl = 0.0

    print(f"\n{BOLD}  Portfolio{RESET}")
    bal_str = f"{available:.4f} SOL" if available is not None else f"{DIM}n/a (live){RESET}"
    initial = initial_balance or 2.0
    roi = (overall_pnl / initial * 100) if initial > 0 else 0.0

    print(f"    Available: {bal_str}  |  Invested: {invested:.4f} SOL")
    print(f"    Daily P&L: {format_pnl(daily_pnl)}  |  "
          f"Overall P&L: {format_pnl(overall_pnl)}  |  "
          f"ROI: {format_pct(roi)}")


def section_positions(conn):
    """Open positions table."""
    try:
        rows = conn.execute(
            "SELECT symbol, strategy, sol_invested, entry_price, entry_time, peak_price "
            "FROM positions ORDER BY entry_time DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    print(f"\n{BOLD}  Open Positions ({len(rows)}){RESET}")
    headers = ["Symbol", "Strategy", "SOL", "Entry $", "Age", "Peak Gain%"]
    table_rows = []
    for r in rows:
        peak_gain = ((r["peak_price"] / r["entry_price"] - 1) * 100
                     if r["entry_price"] and r["peak_price"] else 0)
        table_rows.append([
            f"{WHITE}{r['symbol']}{RESET}",
            r["strategy"] or "?",
            f"{r['sol_invested']:.4f}",
            f"${r['entry_price']:.8f}" if r["entry_price"] else "n/a",
            format_duration(time.time() - r["entry_time"]) if r["entry_time"] else "?",
            format_pct(peak_gain),
        ])
    print_table(headers, table_rows, indent=4)


def section_trades(conn, limit: int = 10):
    """Recent trades table."""
    try:
        rows = conn.execute(
            "SELECT timestamp, action, symbol, strategy, sol_amount, pnl_sol, reason "
            "FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    print(f"\n{BOLD}  Recent Trades (last {limit}){RESET}")
    headers = ["Time", "Action", "Symbol", "Strategy", "SOL", "P&L", "Reason"]
    table_rows = []
    for r in rows:
        action = r["action"] or ""
        if action == "buy":
            action_str = f"{GREEN}BUY{RESET}"
        elif action == "sell":
            action_str = f"{RED}SELL{RESET}"
        elif action == "partial_sell":
            action_str = f"{YELLOW}PARTIAL{RESET}"
        else:
            action_str = action

        pnl_str = format_pnl(r["pnl_sol"]) if r["action"] in ("sell", "partial_sell") else f"{DIM}—{RESET}"
        reason = (r["reason"] or "")[:20]

        table_rows.append([
            format_age(r["timestamp"]),
            action_str,
            r["symbol"] or "?",
            r["strategy"] or "?",
            f"{r['sol_amount']:.4f}",
            pnl_str,
            reason,
        ])
    print_table(headers, table_rows, indent=4)


def section_strategy_perf(conn):
    """Strategy performance (24h) — per-strategy trades, win rate, P&L."""
    print(f"\n{BOLD}  Strategy Performance (24h){RESET}")
    since = time.time() - 86400
    headers = ["Strategy", "Trades", "Wins", "Win Rate", "P&L"]
    table_rows = []

    for strat in STRATEGIES:
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt, SUM(pnl_sol) as pnl FROM trades "
                "WHERE strategy = ? AND action IN ('sell', 'partial_sell') AND timestamp > ?",
                (strat, since)
            ).fetchone()
            total = row["cnt"] if row else 0
            pnl = row["pnl"] or 0.0 if row else 0.0

            wins_row = conn.execute(
                "SELECT COUNT(*) as w FROM trades "
                "WHERE strategy = ? AND action = 'sell' AND pnl_sol > 0 AND timestamp > ?",
                (strat, since)
            ).fetchone()
            wins = wins_row["w"] if wins_row else 0
        except sqlite3.OperationalError:
            total, wins, pnl = 0, 0, 0.0

        if total == 0:
            table_rows.append([strat, f"{DIM}0{RESET}", f"{DIM}0{RESET}",
                               f"{DIM}—{RESET}", f"{DIM}—{RESET}"])
        else:
            wr = wins / total * 100
            wr_color = GREEN if wr >= 50 else RED
            table_rows.append([
                strat, str(total), str(wins),
                f"{wr_color}{wr:.0f}%{RESET}",
                format_pnl(pnl),
            ])

    print_table(headers, table_rows, indent=4)


def section_wallets(conn):
    """Tracked wallets: auto wallet count, top 5 by score."""
    auto_wallets = get_state(conn, "auto_wallets")
    auto_count = len(auto_wallets) if isinstance(auto_wallets, list) else 0

    try:
        rows = conn.execute(
            "SELECT wallet, score, total_trades, winning_trades, total_pnl_sol "
            "FROM wallet_scores ORDER BY score DESC LIMIT 5"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    total_wallets = 0
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM wallet_scores").fetchone()
        total_wallets = row["cnt"] if row else 0
    except sqlite3.OperationalError:
        pass

    print(f"\n{BOLD}  Tracked Wallets{RESET}  "
          f"({total_wallets} scored, {auto_count} auto-discovered)")
    headers = ["Wallet", "Score", "Trades", "Win Rate", "P&L"]
    table_rows = []
    for r in rows:
        wr = (r["winning_trades"] / r["total_trades"] * 100
              if r["total_trades"] > 0 else 0)
        table_rows.append([
            f"{r['wallet'][:12]}...",
            f"{CYAN}{r['score']:.0f}{RESET}",
            str(r["total_trades"]),
            f"{wr:.0f}%",
            format_pnl(r["total_pnl_sol"]),
        ])
    print_table(headers, table_rows, indent=4)


def section_system(conn):
    """System: open positions count, loss streaks, cooldowns, blacklist count."""
    try:
        pos_count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    except sqlite3.OperationalError:
        pos_count = 0

    try:
        bl_count = conn.execute("SELECT COUNT(*) FROM token_blacklist").fetchone()[0]
    except sqlite3.OperationalError:
        bl_count = 0

    print(f"\n{BOLD}  System{RESET}")
    print(f"    Monitoring: {pos_count} open positions  |  "
          f"Blacklisted: {bl_count} tokens")

    # Loss streaks + cooldowns
    try:
        streaks = conn.execute(
            "SELECT strategy, consecutive_losses, cooldown_until FROM loss_streaks "
            "WHERE consecutive_losses > 0"
        ).fetchall()
    except sqlite3.OperationalError:
        streaks = []

    if streaks:
        parts = []
        for s in streaks:
            cd_remaining = s["cooldown_until"] - time.time()
            cd_str = f" {RED}(CD {int(cd_remaining)}s){RESET}" if cd_remaining > 0 else ""
            parts.append(f"{s['strategy']}: {s['consecutive_losses']} losses{cd_str}")
        print(f"    Loss streaks: {', '.join(parts)}")
    else:
        print(f"    Loss streaks: {DIM}none{RESET}")


# ── Main Loop ─────────────────────────────────────────────────────


def draw(db_path: str):
    """Draw one frame of the dashboard."""
    conn = connect_db(db_path)
    if not conn:
        print(f"{CLEAR}{RED}Database not found: {db_path}{RESET}")
        print(f"{DIM}Is the bot running? Expected DB at: {os.path.abspath(db_path)}{RESET}")
        return

    try:
        print(CLEAR, end="")
        section_header(conn)
        section_portfolio(conn)
        section_positions(conn)
        section_trades(conn)
        section_strategy_perf(conn)
        section_wallets(conn)
        section_system(conn)
        print(f"\n{DIM}  Press Ctrl+C to exit{RESET}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Mako live dashboard")
    parser.add_argument("--db", default="bot_state.db", help="Path to bot database")
    parser.add_argument("--interval", type=int, default=2, help="Refresh interval in seconds")
    args = parser.parse_args()

    try:
        while True:
            draw(args.db)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n{DIM}Dashboard stopped.{RESET}")
        sys.exit(0)


if __name__ == "__main__":
    main()
