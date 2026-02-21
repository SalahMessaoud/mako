"""CLI entry point for Mako — Solana meme coin trading bot."""

import sys
import argparse
import logging
import logging.handlers
import time
from config import Config
from bot import MemeBot

BANNER = """
╔══════════════════════════════════════════════════════════╗
║  MAKO v3.0 — Solana Trading Engine                       ║
║  PumpPortal + On-chain Analysis + Crash Recovery         ║
╠══════════════════════════════════════════════════════════╣
║  Strategies: Sniper | Momentum | Copy Trade | Graduation ║
║  Safety: DexScreener + On-chain (mint/freeze/holders)    ║
║  Risk: Dynamic sizing + Loss cooldowns + Persistence     ║
╚══════════════════════════════════════════════════════════╝
"""

def main():
    parser = argparse.ArgumentParser(description="Mako — Solana Trading Engine")
    parser.add_argument("--paper", action="store_true", default=True, help="Paper trading (default)")
    parser.add_argument("--live", action="store_true", help="Live trading with real SOL")
    parser.add_argument("--strategy", type=str, help="Strategy: sniper/momentum/copy_trade/graduation/multi")
    parser.add_argument("--max-sol", type=float, help="Max SOL per trade")
    parser.add_argument("--interval", type=int, help="Poll interval in seconds")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--db", type=str, default="bot_state.db", help="Database path")
    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.handlers.RotatingFileHandler(
                "bot.log", mode="a",
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=3,
            ),
        ],
    )
    # Silence noisy HTTP request logging from httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    log = logging.getLogger(__name__)
    print(BANNER)

    # Config
    config = Config.from_env()

    if args.live:
        config.paper_trading = False
    if args.strategy:
        config.strategy = args.strategy
    if args.max_sol:
        config.max_buy_sol = args.max_sol
    if args.interval:
        config.poll_interval = args.interval

    # Set DB path
    import os
    os.environ["BOT_DB_PATH"] = args.db

    # Validate
    issues = config.validate()
    if issues:
        for issue in issues:
            log.error(f"  Config: {issue}")
        sys.exit(1)

    # Config summary
    mode = "PAPER" if config.paper_trading else "LIVE"
    log.info(f"  Mode: {mode}")
    log.info(f"  Strategy: {config.strategy}")
    log.info(f"  Max buy: {config.max_buy_sol} SOL")
    log.info(f"  Stop-loss: {config.stop_loss_pct}% | TP1: {config.take_profit_pct}% | TP2: {config.take_profit_2_pct}%")
    log.info(f"  On-chain safety: ENABLED")
    log.info(f"  Crash recovery: ENABLED (DB: {args.db})")
    log.info(f"  Loss cooldowns: ENABLED")
    log.info(f"  Dynamic sizing: ENABLED")

    if config.copy_wallets:
        log.info(f"  Copy wallets: {len(config.copy_wallets)}")
        log.info(f"  Copy-sell mirroring: ENABLED")

    # Safety delay for live mode
    if not config.paper_trading:
        log.warning("  LIVE TRADING — real SOL at risk!")
        log.warning("  Starting in 5 seconds... Ctrl+C to cancel")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            log.info("  Cancelled")
            sys.exit(0)

    # Run
    bot = MemeBot(config)
    bot.run()


if __name__ == "__main__":
    main()
