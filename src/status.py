"""On-demand query CLI for Mako — inspect positions, trades, P&L, wallets, strategies.

Usage:
    python status.py                   # Quick summary
    python status.py positions         # Detailed open positions
    python status.py trades            # Trade history (--limit N, --strategy STR)
    python status.py pnl               # P&L breakdown by strategy (--hours N)
    python status.py wallets           # Wallet scores (--min-score N)
    python status.py strategies        # Per-strategy 1h/6h/24h performance
    python status.py log               # Tail bot.log (--lines N, --filter STR)
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


def get_state(conn, key: str, default=None):
    """Get a value from bot_state table."""
    try:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default
    except (sqlite3.OperationalError, TypeError):
        return default


def require_db(path: str):
    """Open DB or exit with error."""
    conn = connect_db(path)
    if not conn:
        print(f"{RED}Database not found: {path}{RESET}")
        print(f"{DIM}Is the bot running? Expected DB at: {os.path.abspath(path)}{RESET}")
        sys.exit(1)
    return conn


# ── Subcommands ───────────────────────────────────────────────────


def cmd_summary(args):
    """Default: quick summary (portfolio + key stats)."""
    conn = require_db(args.db)
    try:
        # Mode
        paper_bal = get_state(conn, "paper_balance")
        mode = f"{YELLOW}PAPER{RESET}" if paper_bal is not None else f"{RED}LIVE{RESET}"

        # Portfolio
        available = paper_bal if paper_bal is not None else None
        row = conn.execute("SELECT SUM(sol_invested) as t, COUNT(*) as c FROM positions").fetchone()
        invested = row["t"] or 0.0
        pos_count = row["c"]

        # P&L
        today_start = time.time() - (time.time() % 86400)
        daily_row = conn.execute(
            "SELECT SUM(pnl_sol) as pnl FROM trades WHERE action='sell' AND timestamp > ?",
            (today_start,)
        ).fetchone()
        daily_pnl = daily_row["pnl"] or 0.0 if daily_row else 0.0

        overall_row = conn.execute(
            "SELECT SUM(pnl_sol) as pnl, COUNT(*) as cnt FROM trades WHERE action='sell'"
        ).fetchone()
        overall_pnl = overall_row["pnl"] or 0.0 if overall_row else 0.0
        total_sells = overall_row["cnt"] if overall_row else 0

        total_buys_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE action='buy'"
        ).fetchone()
        total_buys = total_buys_row["cnt"] if total_buys_row else 0

        wins_row = conn.execute(
            "SELECT COUNT(*) as w FROM trades WHERE action='sell' AND pnl_sol > 0"
        ).fetchone()
        total_wins = wins_row["w"] if wins_row else 0
        win_rate = (total_wins / total_sells * 100) if total_sells > 0 else 0

        bl_count = conn.execute("SELECT COUNT(*) FROM token_blacklist").fetchone()[0]

        # Print
        print(f"\n{BOLD}{CYAN}  MAKO STATUS{RESET}  |  {mode}")
        print(f"{DIM}{'─' * 50}{RESET}")

        bal_str = f"{available:.4f} SOL" if available is not None else f"{DIM}n/a (live){RESET}"
        print(f"  Available:    {bal_str}")
        print(f"  Invested:     {invested:.4f} SOL")
        print(f"  Positions:    {pos_count} open")
        print()
        print(f"  Daily P&L:    {format_pnl(daily_pnl)}")
        print(f"  Overall P&L:  {format_pnl(overall_pnl)}")
        print(f"  Trades:       {total_buys} buys / {total_sells} sells")
        if total_sells > 0:
            wr_color = GREEN if win_rate >= 50 else RED
            print(f"  Win Rate:     {wr_color}{win_rate:.1f}%{RESET} ({total_wins}/{total_sells})")
        else:
            print(f"  Win Rate:     {DIM}n/a{RESET}")
        print(f"  Blacklisted:  {bl_count} tokens")
        print()
    finally:
        conn.close()


def cmd_positions(args):
    """Detailed open positions table."""
    conn = require_db(args.db)
    try:
        rows = conn.execute(
            "SELECT mint, symbol, strategy, sol_invested, entry_price, "
            "entry_sol_price, tokens, entry_time, peak_price, partial_taken "
            "FROM positions ORDER BY entry_time DESC"
        ).fetchall()

        print(f"\n{BOLD}  Open Positions ({len(rows)}){RESET}")
        if not rows:
            print(f"  {DIM}No open positions{RESET}\n")
            return

        headers = ["Symbol", "Strategy", "SOL", "Entry $", "Tokens",
                    "Age", "Peak Gain%", "Partial", "Mint"]
        table_rows = []
        for r in rows:
            peak_gain = ((r["peak_price"] / r["entry_price"] - 1) * 100
                         if r["entry_price"] and r["peak_price"] else 0)
            table_rows.append([
                f"{WHITE}{r['symbol']}{RESET}",
                r["strategy"] or "?",
                f"{r['sol_invested']:.4f}",
                f"${r['entry_price']:.8f}" if r["entry_price"] else "n/a",
                f"{r['tokens']:.0f}",
                format_duration(time.time() - r["entry_time"]) if r["entry_time"] else "?",
                format_pct(peak_gain),
                f"{YELLOW}yes{RESET}" if r["partial_taken"] else f"{DIM}no{RESET}",
                f"{DIM}{r['mint'][:16]}...{RESET}",
            ])
        print_table(headers, table_rows, indent=2)
        print()
    finally:
        conn.close()


def cmd_trades(args):
    """Trade history."""
    conn = require_db(args.db)
    try:
        query = "SELECT timestamp, action, symbol, strategy, sol_amount, pnl_sol, pnl_pct, reason FROM trades"
        params = []

        if args.strategy:
            query += " WHERE strategy = ?"
            params.append(args.strategy)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(args.limit)

        rows = conn.execute(query, params).fetchall()

        title = f"Trade History (last {args.limit})"
        if args.strategy:
            title += f" — {args.strategy}"
        print(f"\n{BOLD}  {title}{RESET}")

        headers = ["Time", "Action", "Symbol", "Strategy", "SOL", "P&L", "P&L%", "Reason"]
        table_rows = []
        for r in rows:
            action = r["action"] or ""
            if action == "buy":
                action_str = f"{GREEN}BUY{RESET}"
            elif action == "sell":
                action_str = f"{RED}SELL{RESET}"
            else:
                action_str = action

            pnl_str = format_pnl(r["pnl_sol"]) if r["action"] == "sell" else f"{DIM}—{RESET}"
            pct_str = format_pct(r["pnl_pct"]) if r["action"] == "sell" else f"{DIM}—{RESET}"
            reason = (r["reason"] or "")[:25]

            table_rows.append([
                format_age(r["timestamp"]),
                action_str,
                r["symbol"] or "?",
                r["strategy"] or "?",
                f"{r['sol_amount']:.4f}",
                pnl_str,
                pct_str,
                reason,
            ])
        print_table(headers, table_rows, indent=2)
        print()
    finally:
        conn.close()


def cmd_pnl(args):
    """P&L breakdown by strategy."""
    conn = require_db(args.db)
    try:
        since = time.time() - args.hours * 3600
        print(f"\n{BOLD}  P&L Breakdown (last {args.hours}h){RESET}")

        headers = ["Strategy", "Trades", "Wins", "Losses", "Win Rate", "Total P&L", "Avg P&L", "Best", "Worst"]
        table_rows = []
        grand_pnl = 0.0
        grand_trades = 0

        for strat in STRATEGIES:
            sells = conn.execute(
                "SELECT pnl_sol FROM trades "
                "WHERE strategy = ? AND action = 'sell' AND timestamp > ?",
                (strat, since)
            ).fetchall()

            if not sells:
                table_rows.append([strat] + [f"{DIM}—{RESET}"] * 8)
                continue

            pnls = [r["pnl_sol"] for r in sells]
            total = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            losses = total - wins
            total_pnl = sum(pnls)
            avg_pnl = total_pnl / total
            best = max(pnls)
            worst = min(pnls)
            wr = wins / total * 100

            grand_pnl += total_pnl
            grand_trades += total

            wr_color = GREEN if wr >= 50 else RED
            table_rows.append([
                strat,
                str(total),
                f"{GREEN}{wins}{RESET}",
                f"{RED}{losses}{RESET}" if losses > 0 else f"{DIM}0{RESET}",
                f"{wr_color}{wr:.0f}%{RESET}",
                format_pnl(total_pnl),
                format_pnl(avg_pnl),
                format_pnl(best),
                format_pnl(worst),
            ])

        print_table(headers, table_rows, indent=2)
        print(f"\n  {BOLD}Total:{RESET} {grand_trades} trades, {format_pnl(grand_pnl)}")
        print()
    finally:
        conn.close()


def cmd_wallets(args):
    """Wallet scores table."""
    conn = require_db(args.db)
    try:
        rows = conn.execute(
            "SELECT wallet, score, total_trades, winning_trades, total_pnl_sol, "
            "avg_hold_sec, last_trade_time, streak "
            "FROM wallet_scores WHERE score >= ? ORDER BY score DESC",
            (args.min_score,)
        ).fetchall()

        print(f"\n{BOLD}  Wallet Scores{RESET}  "
              f"(min score: {args.min_score}, showing {len(rows)})")

        headers = ["Wallet", "Score", "Trades", "Win Rate", "P&L",
                    "Avg Hold", "Streak", "Last Trade"]
        table_rows = []
        for r in rows:
            wr = (r["winning_trades"] / r["total_trades"] * 100
                  if r["total_trades"] > 0 else 0)
            streak = r["streak"]
            if streak > 0:
                streak_str = f"{GREEN}+{streak}{RESET}"
            elif streak < 0:
                streak_str = f"{RED}{streak}{RESET}"
            else:
                streak_str = f"{DIM}0{RESET}"

            table_rows.append([
                f"{r['wallet'][:16]}...",
                f"{CYAN}{r['score']:.0f}{RESET}",
                str(r["total_trades"]),
                f"{wr:.0f}%",
                format_pnl(r["total_pnl_sol"]),
                format_duration(r["avg_hold_sec"]) if r["avg_hold_sec"] else "?",
                streak_str,
                format_age(r["last_trade_time"]),
            ])
        print_table(headers, table_rows, indent=2)
        print()
    finally:
        conn.close()


def cmd_strategies(args):
    """Per-strategy 1h/6h/24h performance + loss streaks."""
    conn = require_db(args.db)
    try:
        print(f"\n{BOLD}  Strategy Performance{RESET}")
        headers = ["Strategy", "1h Trades", "1h P&L", "6h Trades", "6h P&L",
                    "24h Trades", "24h P&L", "Losses", "Cooldown"]
        table_rows = []

        for strat in STRATEGIES:
            cells = [strat]
            for hours in [1, 6, 24]:
                since = time.time() - hours * 3600
                row = conn.execute(
                    "SELECT COUNT(*) as cnt, SUM(pnl_sol) as pnl FROM trades "
                    "WHERE strategy = ? AND action = 'sell' AND timestamp > ?",
                    (strat, since)
                ).fetchone()
                cnt = row["cnt"] if row else 0
                pnl = row["pnl"] or 0.0 if row else 0.0
                cells.append(str(cnt))
                cells.append(format_pnl(pnl) if cnt > 0 else f"{DIM}—{RESET}")

            # Loss streak
            streak_row = conn.execute(
                "SELECT consecutive_losses, cooldown_until FROM loss_streaks WHERE strategy = ?",
                (strat,)
            ).fetchone()
            if streak_row and streak_row["consecutive_losses"] > 0:
                cells.append(f"{RED}{streak_row['consecutive_losses']}{RESET}")
                cd_remaining = streak_row["cooldown_until"] - time.time()
                if cd_remaining > 0:
                    cells.append(f"{RED}{int(cd_remaining)}s{RESET}")
                else:
                    cells.append(f"{DIM}no{RESET}")
            else:
                cells.append(f"{DIM}0{RESET}")
                cells.append(f"{DIM}no{RESET}")

            table_rows.append(cells)

        print_table(headers, table_rows, indent=2)
        print()
    finally:
        conn.close()


def cmd_log(args):
    """Tail bot.log with colorized output."""
    log_path = args.log_file
    if not os.path.exists(log_path):
        print(f"{RED}Log file not found: {log_path}{RESET}")
        print(f"{DIM}Expected at: {os.path.abspath(log_path)}{RESET}")
        sys.exit(1)

    with open(log_path, "r") as f:
        lines = f.readlines()

    tail = lines[-args.lines:]

    if args.filter:
        pattern = args.filter.lower()
        tail = [l for l in tail if pattern in l.lower()]

    print(f"\n{BOLD}  Bot Log (last {args.lines} lines){RESET}")
    if args.filter:
        print(f"  {DIM}Filter: {args.filter}{RESET}")
    print(f"{DIM}{'─' * 72}{RESET}")

    for line in tail:
        line = line.rstrip()
        # Colorize by log level
        if "ERROR" in line or "CRITICAL" in line:
            print(f"  {RED}{line}{RESET}")
        elif "WARNING" in line:
            print(f"  {YELLOW}{line}{RESET}")
        elif "BUY" in line or "SNIPE" in line or "COPY:" in line or "GRADUATION:" in line:
            print(f"  {GREEN}{line}{RESET}")
        elif "SELL" in line or "exit" in line.lower():
            print(f"  {MAGENTA}{line}{RESET}")
        elif "STATUS" in line or "═" in line:
            print(f"  {CYAN}{line}{RESET}")
        else:
            print(f"  {line}")
    print()


# ── Main ──────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Mako query CLI — inspect bot state",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="bot_state.db", help="Path to bot database")

    sub = parser.add_subparsers(dest="command")

    def add_db(p):
        p.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    # positions
    p_pos = sub.add_parser("positions", help="Detailed open positions")
    add_db(p_pos)

    # trades
    p_trades = sub.add_parser("trades", help="Trade history")
    p_trades.add_argument("--limit", type=int, default=20, help="Number of trades to show")
    p_trades.add_argument("--strategy", type=str, default="", help="Filter by strategy")
    add_db(p_trades)

    # pnl
    p_pnl = sub.add_parser("pnl", help="P&L breakdown by strategy")
    p_pnl.add_argument("--hours", type=int, default=24, help="Lookback hours")
    add_db(p_pnl)

    # wallets
    p_wallets = sub.add_parser("wallets", help="Wallet scores")
    p_wallets.add_argument("--min-score", type=float, default=0, help="Minimum score filter")
    add_db(p_wallets)

    # strategies
    p_strat = sub.add_parser("strategies", help="Per-strategy 1h/6h/24h performance")
    add_db(p_strat)

    # log
    p_log = sub.add_parser("log", help="Tail bot.log with color")
    p_log.add_argument("--lines", type=int, default=50, help="Number of lines to show")
    p_log.add_argument("--filter", type=str, default="", help="Filter string (case-insensitive)")
    p_log.add_argument("--log-file", type=str, default="bot.log", help="Path to log file")
    add_db(p_log)

    args = parser.parse_args()

    commands = {
        None: cmd_summary,
        "positions": cmd_positions,
        "trades": cmd_trades,
        "pnl": cmd_pnl,
        "wallets": cmd_wallets,
        "strategies": cmd_strategies,
        "log": cmd_log,
    }

    fn = commands.get(args.command, cmd_summary)
    fn(args)


if __name__ == "__main__":
    main()
