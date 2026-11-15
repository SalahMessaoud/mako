"""Main bot orchestrator — crash recovery, copy-sell, on-chain analysis, dynamic sizing."""

import json
import time
import asyncio
import logging
from dataclasses import dataclass
from config import Config
from db import Database
from wallet import Wallet, PaperWallet
from pumpportal import PumpPortal, PumpPortalStream
from jupiter import JupiterSwap
from rug_detector import RugDetector
from wallet_discovery import WalletDiscovery
from market_data import MarketData
from onchain import OnChainAnalyzer
from safety import SafetyChecker
from risk_manager import RiskManager
from strategies import (
    SniperStrategy,
    CopyTradeStrategy, GraduationStrategy, Signal, SellSignal,
)
from notifications import Notifier
from telegram_ui import TelegramUI
from txutil import TradeResult, parse_tx_outcome

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
# MINT CANDIDATE POOL — Stage 1 of 3-stage discovery
# ══════════════════════════════════════════════════════════

@dataclass
class MintCandidate:
    """Token creation event data for scoring."""
    mint: str
    time: float
    initial_buy_lamports: float
    market_cap_sol: float
    creator: str
    symbol: str
    v_sol: float


class MintPool:
    """Scored candidate pool from WebSocket new_token events.

    Replaces random sampling with scored ranking based on:
    - Age sweet spot (5-12 min = highest score)
    - Initial buy amount (proof of commitment, log-scaled)
    - Market cap at creation (higher = more initial interest)
    """

    def __init__(self, max_age_sec: int = 1200):
        self.candidates: dict[str, MintCandidate] = {}
        self.max_age = max_age_sec
        # Buyer uniqueness tracking: mint -> set of unique buyer wallets
        self._unique_buyers: dict[str, set[str]] = {}
        self._total_buys: dict[str, int] = {}  # mint -> total buy count
        self._total_buy_sol: dict[str, float] = {}  # mint -> total SOL bought
        self._total_trades: dict[str, int] = {}  # mint -> all trades (buys + sells)

    def add(self, event: dict):
        mint = event.get("mint", "")
        if not mint:
            return
        self.candidates[mint] = MintCandidate(
            mint=mint,
            time=time.time(),
            initial_buy_lamports=float(event.get("initialBuy", 0) or 0),
            market_cap_sol=float(event.get("marketCapSol", 0) or 0),
            creator=event.get("traderPublicKey", ""),
            symbol=event.get("symbol", "???"),
            v_sol=float(event.get("vSolInBondingCurve", 0) or 0),
        )
        if len(self.candidates) > 600:
            self._prune()

    def record_buyer(self, mint: str, buyer: str, sol_amount: float = 0):
        """Record a buy event for buyer uniqueness tracking."""
        if not mint or not buyer:
            return
        if mint not in self._unique_buyers:
            self._unique_buyers[mint] = set()
            self._total_buys[mint] = 0
            self._total_buy_sol[mint] = 0.0
        self._unique_buyers[mint].add(buyer)
        self._total_buys[mint] += 1
        self._total_buy_sol[mint] += sol_amount

    def record_trade(self, mint: str):
        """Record any trade event (buy or sell) for activity counting."""
        self._total_trades[mint] = self._total_trades.get(mint, 0) + 1

    def get_trade_count(self, mint: str) -> int:
        """Get total observed trades (buys + sells) from WebSocket events."""
        return self._total_trades.get(mint, 0)

    def get_buyer_uniqueness(self, mint: str) -> tuple[float, int, int, float]:
        """Get buyer uniqueness metrics for a token.

        Returns (uniqueness_ratio, unique_count, total_buys, avg_sol_per_buy).
        """
        unique = self._unique_buyers.get(mint, set())
        total = self._total_buys.get(mint, 0)
        total_sol = self._total_buy_sol.get(mint, 0.0)
        if total == 0:
            return 1.0, 0, 0, 0.0  # No data — assume unique (don't penalize)
        avg_sol = total_sol / total if total > 0 else 0.0
        return len(unique) / total, len(unique), total, avg_sol

    def _prune(self):
        cutoff = time.time() - self.max_age
        stale = [m for m, c in self.candidates.items() if c.time <= cutoff]
        for m in stale:
            self.candidates.pop(m, None)
            self._unique_buyers.pop(m, None)
            self._total_buys.pop(m, None)
            self._total_buy_sol.pop(m, None)
            self._total_trades.pop(m, None)

    def get_best_candidates(self, count: int = 30, min_age: int = 120,
                            prefer_young: bool = False) -> list[str]:
        """Score and rank candidates for an on-chain check.

        `prefer_young=True` is for the momentum pipeline, whose scorer
        rewards 2-6 minute tokens and penalises anything past 8: ranking by
        the 5-12 minute window would feed it tokens it then marks down.
        The graduation poll wants the older ones and keeps the default.
        """
        now = time.time()
        scored = []
        for m, c in self.candidates.items():
            age = now - c.time
            if age < min_age or age > self.max_age:
                continue

            score = 0.0

            if prefer_young:
                if age <= 360:
                    score += 3.0
                elif age <= 480:
                    score += 2.0
                elif age <= 600:
                    score += 1.0
                # Live activity seen on the WebSocket, so a token that
                # launched quietly but is moving now is not ranked out.
                trades = self._total_trades.get(m, 0)
                if trades >= 30:
                    score += 2.0
                elif trades >= 10:
                    score += 1.0
            # Age sweet spot: 5-12 min (most likely to have DexScreener data + still early)
            elif 300 <= age <= 720:
                score += 3.0
            elif 180 <= age < 300:
                score += 1.5
            elif 720 < age <= self.max_age:
                score += 1.0

            # Initial buy signal (log scale, diminishing returns)
            ib_sol = c.initial_buy_lamports / 1e9
            if ib_sol >= 2.0:
                score += 2.0
            elif ib_sol >= 0.5:
                score += 1.5
            elif ib_sol >= 0.1:
                score += 1.0
            elif ib_sol > 0.01:
                score += 0.3
            # Zero buy = no bonus

            # Market cap at creation
            if c.market_cap_sol >= 10:
                score += 1.5
            elif c.market_cap_sol >= 5:
                score += 1.0
            elif c.market_cap_sol >= 2:
                score += 0.5

            scored.append((m, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [m for m, _ in scored[:count]]

    @property
    def size(self) -> int:
        return len(self.candidates)


class MemeBot:
    """
    Mako — Fully automated Solana trading engine.

    v3 Upgrades over v2:
    - Crash recovery (SQLite persistence)
    - Transaction confirmation + retry
    - On-chain safety (mint authority, freeze, holders)
    - Copy-sell mirroring (exit when smart money exits)
    - Dynamic position sizing (confidence + performance)
    - Loss streak cooldowns
    - Wallet scoring for copy targets
    """

    # Timing knobs for on-chain verification (class attributes so tests can
    # shorten them).
    ZERO_RECHECK_DELAY = 2.0          # gap before re-reading a zero balance
    ZERO_CLOSE_MIN_AGE_SEC = 30       # never trust a zero read sooner after a buy
    RECONCILE_GRACE_SEC = 120         # how long a buy may stay tokenless
    PARTIAL_HOLD_SEC = 90             # blockhash lifetime: an unknown tx can land until then
    CURVE_OBS_MIN_SEC = 4             # earlier curve reading must be this old...
    CURVE_OBS_MAX_SEC = 90            # ...and no older than this
    SELL_BALANCE_POLLS = 3            # balance reads after an unconfirmed sell
    SELL_BACKOFF_SEC = 30             # pause between sell attempts after a failure
    SELL_BACKOFF_LONG_SEC = 300       # pause once STUCK_BACKOFF_AFTER attempts failed
    STUCK_BACKOFF_AFTER = 10
    BUY_BALANCE_POLLS_CONFIRMED = (6, 1.5)   # (tries, pause) after a confirmed buy
    BUY_BALANCE_POLLS_UNKNOWN = (6, 2.0)     # unknown outcome, signature known
    BUY_BALANCE_POLLS_NO_SIG = (30, 3.0)     # unknown outcome, no signature

    def __init__(self, config: Config, db_path: str | None = None):
        self.cfg = config
        self.running = False

        # Database (crash recovery)
        self.db = Database(db_path)

        # Paper and live must never share a database: a paper run would
        # "sell" recovered live positions on paper and delete their rows.
        mode = "paper" if config.paper_trading else "live"
        stored_mode = self.db.get_state("trading_mode")
        if stored_mode and stored_mode != mode and self.db.position_count() > 0:
            open_count = self.db.position_count()
            self.db.close()
            raise RuntimeError(
                f"database {self.db.path} holds {open_count} open "
                f"{stored_mode} position(s) but this run is {mode}. "
                f"Use a separate file with --db."
            )
        self.db.set_state("trading_mode", mode)

        # Core systems
        self.market = MarketData()
        self.onchain = OnChainAnalyzer(config.rpc_url, config.fallback_rpc_url)
        self.safety = SafetyChecker(config, onchain=self.onchain)
        self.risk = RiskManager(config, db=self.db)
        self.notify = Notifier(config.telegram_bot_token, config.telegram_chat_id)
        self.tg_ui = TelegramUI(self)
        self._notified_candidates: set[str] = set()

        # Wallet
        if config.paper_trading:
            saved_bal = self.db.get_state("paper_balance")
            self.wallet = PaperWallet(initial_sol=saved_bal or config.paper_balance_sol)
            # Store initial balance for ROI calculation (only set once)
            if not self.db.get_state("paper_initial_balance"):
                self.db.set_state("paper_initial_balance", self.wallet.sol_balance)
        else:
            self.wallet = Wallet(config.rpc_url, config.private_key,
                                 config.fallback_rpc_url)

        # PumpPortal trading client (Jito only in live mode)
        keypair = getattr(self.wallet, 'keypair', None)
        self.pump = PumpPortal(
            config.rpc_url, keypair, config.pumpportal_api_key,
            use_jito=(config.use_jito and not config.paper_trading),
            jito_tip_sol=config.jito_tip_sol,
            fallback_rpc_url=config.fallback_rpc_url,
        )

        # Jupiter swap client (for selling graduated tokens)
        self.jupiter = JupiterSwap(
            config.rpc_url, keypair,
            slippage_bps=config.jupiter_slippage_bps,
            jito_sender=(self.pump._send_jito_bundle
                         if self.pump.use_jito else None),
            fallback_rpc_url=config.fallback_rpc_url,
        )
        self._graduated_cache: dict[str, bool] = {}

        # Rug detection
        if config.rug_detection:
            self.rug_detector = RugDetector(self.onchain, config)
            log.info("  Rug detection enabled")

        # WebSocket stream
        # The keyed connection is opt-in: PumpPortal bills trade events to
        # the key's wallet, and that would be real SOL even in paper mode.
        self.stream = PumpPortalStream(
            api_key=config.pumpportal_api_key if config.pumpportal_trade_feed else "")

        # Strategies
        self.strategies_enabled = self._parse_strategies()

        if "sniper" in self.strategies_enabled:
            self.sniper = SniperStrategy(
                min_sol_liq=config.sniper_min_sol_liq,
                buy_delay=config.sniper_buy_delay, db=self.db,
            )
            self.stream.on_new_token(self._on_new_token)
            log.info("  Sniper strategy enabled")

        if "momentum" in self.strategies_enabled:
            log.info("  Momentum strategy enabled")

        self._pipeline_rotation = 0
        # mint -> (time, virtual SOL) at the previous pipeline evaluation
        self._curve_obs: dict[str, tuple[float, float]] = {}
        self._copy_event_count = 0  # Diagnostic counter
        self._wallet_seen_sigs: dict[str, set[str]] = {}  # wallet -> seen tx signatures
        self._wallet_poll_ok = 0
        self._wallet_poll_fail = 0
        self._wallet_poll_warned_at = 0.0
        self._wallet_poll_task = None

        if "copy_trade" in self.strategies_enabled and config.copy_wallets:
            self.copy_trade = CopyTradeStrategy(config.copy_wallets, db=self.db)
            self.stream.on_account_trade(self._on_account_trade)
            log.info(f"  Copy trade enabled — watching {len(config.copy_wallets)} wallets")
            for w in config.copy_wallets:
                log.info(f"    Wallet: {w[:16]}...")

        if "graduation" in self.strategies_enabled:
            self.graduation = GraduationStrategy(
                self.market, config.grad_min_curve_pct, config.grad_buy_at_pct,
                onchain=self.onchain, db=self.db,
            )
            self.stream.on_token_trade(self._on_token_trade)
            log.info("  Graduation strategy enabled")

        # 3-stage discovery pipeline: scored mint pool replaces random sampling
        self.mint_pool = MintPool(max_age_sec=1200)

        # Always register new_token handler for discovery + graduation
        if "graduation" in self.strategies_enabled or "momentum" in self.strategies_enabled:
            self.stream.on_new_token(self._on_new_token_discovery)
            log.info("  3-stage token discovery enabled (WebSocket → on-chain → DexScreener)")

        # Wallet discovery (decoupled from copy_trade — works with all strategies)
        if config.auto_discover_wallets:
            manual_wallets = config.copy_wallets if config.copy_wallets else []
            self.wallet_discovery = WalletDiscovery(
                self.db, manual_wallets,
                max_auto=config.max_auto_wallets,
                min_score=config.min_wallet_score_to_watch,
                min_buys=config.wallet_discovery_min_buys,
                min_sells=config.wallet_discovery_min_sells,
                min_ratio=config.wallet_discovery_min_ratio,
                min_tokens=config.wallet_discovery_min_tokens,
            )
            log.info(f"  Wallet discovery enabled (max {config.max_auto_wallets} auto wallets)")

        # WebSocket reconnect audit: verify positions after connection loss
        self.stream.on_reconnect(self._on_ws_reconnect)

        # Per-mint sell locks to prevent race conditions without blocking
        # concurrent exits of different positions
        self._mint_locks: dict[str, asyncio.Lock] = {}

        # Per-mint re-entry cooldown (mint -> expiry timestamp)
        self._mint_cooldowns: dict[str, float] = {}

        # Discovery subscriptions: temporarily subscribe to active pipeline
        # tokens so _on_token_trade → wallet_discovery.observe_trade() sees
        # broader market activity (mint -> expiry timestamp)
        self._discovery_subs: dict[str, float] = {}

        # Buyer tracking subscriptions: subscribe promising new tokens to trade
        # events early so Sybil detection has data by the time Stage 3 evaluates
        self._buyer_subs: dict[str, float] = {}  # mint -> expiry timestamp
        self._buyer_sub_max = 50  # max concurrent buyer tracking subs

        # No-re-entry set (populated when positions close; reloaded from the
        # trade log so a restart does not re-open tokens traded today)
        self._closed_mints: set[str] = self.db.get_recently_closed_mints(hours=24)

        # Sells that could not be completed: mint -> failed attempt count.
        # These positions are KEPT and retried — never written off.
        self._stuck_positions: dict[str, int] = {}
        self._stuck_alerted: set[str] = set()
        # mint -> (no automatic sell before this time, failed attempt was an emergency)
        self._sell_backoff: dict[str, tuple[float, bool]] = {}

        # Signatures of sell attempts whose outcome was not confirmed. If the
        # tokens later turn out to be gone, these tell us what was received.
        self._sell_sigs: dict[str, list[str]] = {}

        # Mints with a sell currently in flight (prevents stacking sells)
        self._selling: dict[str, int] = {}   # mint -> largest sell % in flight

        # Partial sells with an unknown outcome: no new partial until resolved
        self._partial_hold: dict[str, dict] = {}

        # Buys approved by the risk check but not yet opened: mint -> SOL.
        # Counted against the caps so concurrent buys cannot overshoot them.
        self._reserved: dict[str, float] = {}

        # Background trade tasks (kept referenced so they are not GC'd)
        self._bg_tasks: set[asyncio.Task] = set()

        # Crash recovery
        recovered = self.risk.recover_positions()
        if recovered:
            log.info(f"  RECOVERED {recovered} positions from last session")
            # Validate recovered positions against on-chain balances (live only)
            if not config.paper_trading:
                to_remove = []
                for mint, pos in list(self.risk.positions.items()):
                    balance = self.wallet.get_token_balance(mint)
                    if balance is None:
                        log.warning(f"  Recovered {pos.symbol} balance check failed (RPC error) — keeping")
                        continue
                    if balance <= 0:
                        # Confirm with a second read before dropping anything
                        time.sleep(1)
                        recheck = self.wallet.get_token_balance(mint)
                        if recheck is None or recheck > 0:
                            log.warning(f"  Recovered {pos.symbol} balance unclear — keeping")
                            continue
                        log.warning(f"  Recovered {pos.symbol} has 0 on-chain balance — removing")
                        to_remove.append(mint)
                    elif pos.tx_signature == "phantom":
                        log.warning(f"  Recovered phantom position {pos.symbol} — balance: {balance}")
                for mint in to_remove:
                    self.risk.positions.pop(mint, None)
                    self.db.delete_position(mint)
                if to_remove:
                    recovered -= len(to_remove)
                    log.info(f"  Removed {len(to_remove)} stale positions after validation")
            self.notify.send(f"Bot restarted — recovered {recovered} open positions")

    def _parse_strategies(self) -> set[str]:
        s = self.cfg.strategy.lower()
        if s == "multi":
            return {"sniper", "momentum", "copy_trade", "graduation"}
        return set(x.strip() for x in s.split(","))

    # ══════════════════════════════════════════════════════════
    # MAIN RUN LOOP
    # ══════════════════════════════════════════════════════════

    def run(self):
        self.running = True
        mode = "PAPER" if self.cfg.paper_trading else "LIVE"
        notify_tag = " | NOTIFY-ONLY (no trades)" if self.cfg.notify_only else ""
        log.info(f"  Bot starting | {mode} | Strategies: {self.strategies_enabled}{notify_tag}")

        sol_bal = self.wallet.get_sol_balance()
        log.info(f"  SOL: {sol_bal:.4f}")
        log.info(f"  Open positions: {len(self.risk.positions)}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._main_loop())
        except KeyboardInterrupt:
            log.info("\n  Stopped by user")
        except Exception as e:
            log.error(f"  FATAL: {e}", exc_info=True)
        finally:
            log.info("  Bot shutting down...")
            self.running = False
            try:
                loop.run_until_complete(self._shutdown())
            except Exception as e:
                log.error(f"  Shutdown error: {e}")
            loop.close()
            log.info("  Bot exited.")

    async def _main_loop(self):
        tasks = []
        if hasattr(self, 'wallet_discovery'):
            watch_wallets = self.wallet_discovery.all_watched
        elif "copy_trade" in self.strategies_enabled:
            watch_wallets = self.cfg.copy_wallets
        else:
            watch_wallets = None
        tasks.append(asyncio.create_task(
            self.stream.connect(watch_wallets=watch_wallets)
        ))
        # Positions recovered from a crash mid-buy have no tokens/price yet
        try:
            await self._reconcile_positions(force=True)
        except Exception as e:
            log.warning(f"  Startup reconcile error: {e}")
        self._log_fee_overhead()
        tasks.append(asyncio.create_task(self._poll_loop()))
        tasks.append(asyncio.create_task(self._fast_position_check()))
        log.info("  All async tasks launched")
        self._main_tasks = tasks
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if not self.running:
            return  # normal shutdown: the tasks were cancelled on purpose
        log.warning("  asyncio.gather returned — all tasks ended!")
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.error(f"  Task {i} crashed: {result}", exc_info=result)

    async def _poll_loop(self):
        tick = 0
        import datetime
        self._last_date = datetime.date.today()
        log.info("  Poll loop starting")
        while self.running:
            try:
                tick += 1
                if tick <= 3 or tick % 20 == 0:
                    log.info(f"  [tick {tick}] positions={len(self.risk.positions)} pool={self.mint_pool.size}")
                await self._check_positions()

                if "momentum" in self.strategies_enabled and tick % 2 == 0:
                    await self._run_momentum()

                # Graduation polling: check MintPool candidates for near-graduation
                if "graduation" in self.strategies_enabled:
                    if tick % 3 == 0:
                        await self._run_graduation_poll()
                    else:
                        # Fast-poll hot list (tokens at 80%+) every tick
                        await self._run_graduation_hot_poll()

                # Telegram command polling (every tick)
                await self._poll_telegram_commands()

                # Heartbeat: update bot_state so monitor knows we're alive
                if tick % 10 == 0:
                    self.db.set_state("heartbeat", int(time.time()))
                    if not self.cfg.paper_trading:
                        self.db.set_state("live_balance", await self._sol_balance())

                    # Clean expired discovery subscriptions
                    if self._discovery_subs:
                        now = time.time()
                        expired_subs = [m for m, exp in self._discovery_subs.items() if now > exp]
                        for mint in expired_subs:
                            if mint not in self.risk.positions:
                                await self.stream.unsubscribe_token(mint)
                            self._discovery_subs.pop(mint, None)

                    # Clean expired buyer tracking subscriptions
                    if self._buyer_subs:
                        now = time.time()
                        expired_buyer = [m for m, exp in self._buyer_subs.items() if now > exp]
                        for mint in expired_buyer:
                            if (mint not in self.risk.positions
                                    and mint not in self._discovery_subs):
                                await self.stream.unsubscribe_token(mint)
                            self._buyer_subs.pop(mint, None)

                if tick % 100 == 0:
                    await self._report_status()
                    # Clean expired mint cooldowns
                    now = time.time()
                    expired = [m for m, exp in self._mint_cooldowns.items() if exp <= now]
                    for m in expired:
                        del self._mint_cooldowns[m]
                    # Drop idle per-mint locks (never one that is held)
                    for m in [m for m, lk in self._mint_locks.items()
                              if not lk.locked() and m not in self.risk.positions
                              and m not in self._reserved and m not in self._selling]:
                        self._mint_locks.pop(m, None)

                # Day-boundary reset for daily PnL
                today = datetime.date.today()
                if today != self._last_date:
                    log.info(f"  New day detected — resetting daily PnL")
                    self.risk.reset_daily()
                    self._last_date = today

                # Retry stuck sells. A stuck position is NEVER written off:
                # the tokens are still in the wallet. The pause between
                # attempts (30s, then 5 min after 10 failures) is enforced in
                # _spawn_sell for every trigger, not just this loop.
                if tick % 10 == 0 and self._stuck_positions:
                    for stuck_mint in list(self._stuck_positions.keys()):
                        pos = self.risk.positions.get(stuck_mint)
                        if not pos:
                            self._clear_stuck(stuck_mint)
                            continue
                        retries = self._stuck_positions.get(stuck_mint, 0)
                        if retries >= self.STUCK_BACKOFF_AFTER:
                            if stuck_mint not in self._stuck_alerted:
                                self._stuck_alerted.add(stuck_mint)
                                log.error(f"  UNSOLD: {pos.symbol} — {retries} failed sells, "
                                          f"backing off to one retry every ~5 min")
                                self.notify.send(
                                    f"UNSOLD: {pos.symbol} ({stuck_mint[:16]}...) — sell failed "
                                    f"{retries} times. Still holding the tokens; retrying "
                                    f"every ~5 min. Manual action may be needed."
                                )
                        self._spawn_sell(stuck_mint, 100, "stuck_retry")

                # Rug detection polling (every ~10 ticks / ~30s)
                if tick % 10 == 0 and hasattr(self, 'rug_detector'):
                    for mint in list(self.risk.positions.keys()):
                        try:
                            rug_signal = await self.rug_detector.check_onchain(
                                mint, self.risk.positions,
                            )
                            if rug_signal and rug_signal.severity == "critical":
                                log.warning(f"  RUG EXIT (poll): {rug_signal.symbol} — {rug_signal.reason}")
                                self.notify.send(
                                    f"RUG DETECTED: {rug_signal.symbol} — {rug_signal.reason}"
                                )
                                # Blacklist creator to prevent sniping their future tokens
                                if hasattr(self, 'sniper') and rug_signal.mint in self.rug_detector._monitored:
                                    mon = self.rug_detector._monitored[rug_signal.mint]
                                    if mon.creator:
                                        self.sniper.blacklist_creator(mon.creator)
                                        self.db.blacklist_creator(mon.creator, f"rug: {rug_signal.reason[:80]}")
                                        log.info(f"  Creator {mon.creator[:8]}... blacklisted after rug")
                                self._spawn_sell(
                                    rug_signal.mint, rug_signal.sell_pct, rug_signal.reason,
                                )
                        except Exception as e:
                            log.debug(f"  Rug poll error for {mint[:8]}: {e}")

                # RPC wallet polling for copy trade (every ~30s)
                if tick % 10 == 0 and "copy_trade" in self.strategies_enabled:
                    self._start_wallet_poll()

                # Wallet discovery promotions (every ~5 min)
                if tick % 100 == 0 and hasattr(self, 'wallet_discovery'):
                    try:
                        promoted = self.wallet_discovery.check_promotions()
                        for wallet in promoted:
                            await self.stream.subscribe_account(wallet)
                            if hasattr(self, 'copy_trade'):
                                self.copy_trade.wallets.add(wallet)
                            log.info(f"  Auto-subscribed wallet: {wallet[:12]}...")
                    except Exception as e:
                        log.debug(f"  Wallet discovery promotion error: {e}")

                # Periodic DB cleanup (every ~5 min)
                if tick % 100 == 0:
                    self.db.cleanup_old_data(days=7)
                    # Clean up stale strategy tracking data
                    if hasattr(self, 'copy_trade'):
                        self.copy_trade.cleanup_stale_positions()

                # Save paper wallet balance periodically
                if self.cfg.paper_trading and tick % 10 == 0:
                    self.db.set_state("paper_balance", self.wallet.sol_balance)

            except Exception as e:
                log.error(f"Poll error: {e}", exc_info=True)

            await asyncio.sleep(self.cfg.poll_interval)

    # ══════════════════════════════════════════════════════════
    # FAST STOP-LOSS (500ms coroutine)
    # ══════════════════════════════════════════════════════════

    async def _fast_position_check(self):
        """High-frequency check for positions in the danger zone.

        Runs every 500ms but only evaluates positions where PnL is already
        past half the stop-loss threshold (e.g., -15% when stop is 30%).
        This catches fast meme coin dumps between the 3s poll cycles.
        """
        # Let the main loop start first
        await asyncio.sleep(5)
        log.info("  Fast stop-loss checker started (500ms interval)")

        while self.running:
            try:
                for mint, pos in list(self.risk.positions.items()):
                    if pos.entry_price <= 0:
                        continue

                    # Grace period: don't fast-exit early after buy
                    # EXCEPT for catastrophic dumps (>50% loss).
                    in_grace = pos.age_sec < self.cfg.fast_stop_grace_sec

                    # Strategy-specific stop loss
                    stop = (self.cfg.momentum_stop_loss_pct
                            if pos.strategy == "momentum"
                            else self.cfg.stop_loss_pct)

                    # No-stop window: meme coins swing 15-20% routinely
                    # early on. Only allow catastrophic override during this
                    # window to avoid killing correct signals early.
                    in_nostop_window = pos.age_sec < self.cfg.nostop_window_sec
                    if in_nostop_window:
                        stop = self.cfg.early_stop_pct(stop)

                    danger_threshold = -(stop / 2)

                    # Quick price from DexScreener cache first
                    price_usd = None
                    token = await self.market.get_token(mint)
                    if token:
                        price_usd = token.price_usd
                    else:
                        # On-chain fallback (has its own 5s cache)
                        price_usd = await self._get_onchain_price_usd(mint)

                    if not price_usd or price_usd <= 0:
                        continue

                    pnl_pct = ((price_usd - pos.entry_price) / pos.entry_price) * 100

                    # During grace period, only act on catastrophic dumps (>50%)
                    # Require 8s confirmation to avoid acting on a single bad price read
                    if in_grace:
                        if pnl_pct > -50:
                            pos.catastrophic_first_seen = 0.0
                            continue
                        now_cat = time.time()
                        if pos.catastrophic_first_seen == 0.0:
                            pos.catastrophic_first_seen = now_cat
                        held = now_cat - pos.catastrophic_first_seen
                        if held < 8:
                            continue
                        # Confirmed for 8s — exit now. Falling through to the
                        # normal stop-loss block would start a SECOND 8s timer.
                        log.warning(f"  FAST-EXIT (catastrophic): {pos.symbol} at "
                                    f"{pnl_pct:+.1f}% during grace (confirmed {held:.0f}s)")
                        self._spawn_sell(mint, 100, "fast_stop_loss_catastrophic")
                        continue

                    # Only act on positions in the danger zone
                    if pnl_pct > danger_threshold:
                        continue

                    # Curve-aware: don't fast-exit if curve is rising toward graduation
                    if (pos.curve_pct > 60 and pos.curve_rising
                            and not pos.graduated
                            and (time.time() - pos.curve_updated_at) < 30
                            and pnl_pct > -self.cfg.protected_stop_pct):
                        continue

                    # Post-graduation grace: don't fast-exit during Raydium migration
                    if pos.in_graduation_grace and pnl_pct > -self.cfg.protected_stop_pct:
                        continue

                    # Check if it's past the full stop-loss (with confirmation)
                    if pnl_pct <= -stop:
                        now = time.time()
                        if pos.stop_loss_first_seen == 0.0:
                            pos.stop_loss_first_seen = now
                        elapsed = now - pos.stop_loss_first_seen
                        # Confirmed: sustained below stop for 8s, or catastrophic dump
                        if elapsed >= 8 or pnl_pct <= -(stop * 3):
                            log.warning(f"  FAST-EXIT: {pos.symbol} at {pnl_pct:+.1f}% "
                                        f"(stop={-stop:.0f}%, confirmed {elapsed:.0f}s)")
                            self._spawn_sell(mint, 100, "fast_stop_loss")
                    elif pos.stop_loss_first_seen > 0:
                        # Price recovered — reset timer
                        pos.stop_loss_first_seen = 0.0

            except Exception as e:
                log.debug(f"  Fast check error: {e}")

            await asyncio.sleep(0.5)

    # ══════════════════════════════════════════════════════════
    # WEBSOCKET EVENT HANDLERS
    # ══════════════════════════════════════════════════════════

    async def _on_new_token_discovery(self, event: dict):
        """Collect new tokens for 3-stage discovery + graduation + wallet discovery."""
        mint = event.get("mint", "")
        if not mint:
            return

        # Stage 1: Add to scored mint pool (replaces random _recent_mints)
        self.mint_pool.add(event)

        # A token's creator must never be promoted as a wallet to copy.
        if hasattr(self, 'wallet_discovery'):
            self.wallet_discovery.note_creator(event.get("traderPublicKey", ""))

        # Subscribe promising tokens to trade events for buyer uniqueness tracking.
        # Only subscribe tokens with decent dev buy (>= 0.1 SOL) to limit WS load.
        ib_sol = float(event.get("initialBuy", 0) or 0) / 1e9
        if (ib_sol >= 0.1
                and mint not in self._buyer_subs
                and mint not in self._discovery_subs
                and len(self._buyer_subs) < self._buyer_sub_max):
            try:
                await self.stream.subscribe_token(mint)
                self._buyer_subs[mint] = time.time() + 300  # 5 min expiry
            except Exception:
                pass

        # Record the creator's initial buy in buyer tracking
        creator = event.get("traderPublicKey", "")
        if creator and ib_sol > 0:
            self.mint_pool.record_buyer(mint, creator, ib_sol)

        # Track for graduation (new tokens with high curve % — buys happen post-graduation)
        if "graduation" in self.strategies_enabled:
            await self.graduation.evaluate_trade(event)

    async def _on_new_token(self, event: dict):
        signal = self.sniper.evaluate_new_token(event)
        if not signal:
            return
        log.info(f"  SNIPE: {signal.symbol} ({signal.confidence:.0%}) — {signal.reason}")
        # Run off the WebSocket reader: a buy can take a long time to confirm
        # and must not stop rug / copy-sell events from being processed.
        self._spawn(self._execute_buy(
            signal, auto_sell_sec=self.cfg.sniper_auto_sell_sec,
            delay=self.cfg.sniper_buy_delay,
        ))

    async def _on_account_trade(self, event: dict):
        """Handle smart money trade — buy copying + sell mirroring."""
        if "copy_trade" not in self.strategies_enabled:
            return

        # Diagnostic: log ALL events from watched wallets
        trader = event.get("traderPublicKey", "")
        if hasattr(self, 'copy_trade') and trader in self.copy_trade.wallets:
            self._copy_event_count += 1
            tx_type_dbg = event.get("txType", "?")
            mint_dbg = event.get("mint", "?")[:8]
            sol_dbg = float(event.get("solAmount", 0) or 0)
            log.info(f"  COPY-EVENT #{self._copy_event_count}: wallet={trader[:8]}... "
                     f"{tx_type_dbg} {mint_dbg}... for {sol_dbg:.3f} SOL")
            # Log full raw event for first 10 events to diagnose field structure
            if self._copy_event_count <= 10:
                log.info(f"  COPY-RAW: {json.dumps({k: str(v)[:50] for k, v in event.items()})}")

        tx_type = event.get("txType", "")

        # ── Copy SELLS (exit when smart money exits) ──────────
        if tx_type == "sell":
            held = set(self.risk.positions.keys())
            sell_signal = self.copy_trade.evaluate_sell(event, held)
            if sell_signal:
                log.info(f"  COPY-SELL: {sell_signal.symbol} — {sell_signal.reason}")
                self._spawn_sell(
                    sell_signal.mint, sell_signal.sell_pct,
                    sell_signal.reason,
                )
            return

        # ── Copy BUYS ─────────────────────────────────────────
        signal = self.copy_trade.evaluate_trade(event)
        if not signal:
            return
        log.info(f"  COPY: {signal.symbol} ({signal.confidence:.0%}) — {signal.reason}")
        self._spawn(self._execute_buy(signal, delay=self.cfg.copy_delay_sec))

    async def _on_token_trade(self, event: dict):
        # Track all trades (buys + sells) for activity counting
        trade_mint = event.get("mint", "")
        trade_type = event.get("txType", "")
        trader_pk = event.get("traderPublicKey", "")
        if trade_mint:
            self.mint_pool.record_trade(trade_mint)
        # Buyer uniqueness tracking for Sybil detection
        if trade_type == "buy" and trade_mint and trader_pk:
            trade_sol = float(event.get("solAmount", 0) or 0)
            self.mint_pool.record_buyer(trade_mint, trader_pk, trade_sol)

        # Wallet discovery: observe all trade events for candidate tracking
        if hasattr(self, 'wallet_discovery'):
            try:
                self.wallet_discovery.observe_trade(event)
            except Exception as e:
                log.debug(f"  Wallet discovery observe error: {e}")

        # Rug detection: check trade events against monitored positions
        if hasattr(self, 'rug_detector'):
            try:
                rug_signal = self.rug_detector.evaluate_trade_event(
                    event, self.risk.positions,
                )
                if rug_signal and rug_signal.severity == "critical":
                    log.warning(f"  RUG EXIT: {rug_signal.symbol} — {rug_signal.reason}")
                    self.notify.send(
                        f"RUG DETECTED: {rug_signal.symbol} — {rug_signal.reason}"
                    )
                    # Blacklist creator to prevent sniping their future tokens
                    if hasattr(self, 'sniper') and rug_signal.mint in self.rug_detector._monitored:
                        mon = self.rug_detector._monitored[rug_signal.mint]
                        if mon.creator:
                            self.sniper.blacklist_creator(mon.creator)
                            self.db.blacklist_creator(mon.creator, f"rug: {rug_signal.reason[:80]}")
                            log.info(f"  Creator {mon.creator[:8]}... blacklisted after rug")
                    self._spawn_sell(
                        rug_signal.mint, rug_signal.sell_pct, rug_signal.reason,
                    )
                    return
            except Exception as e:
                log.debug(f"  Rug event check error: {e}")

        # Real-time price tracking from WebSocket trade events
        mint = event.get("mint", "")
        if mint and mint in self.risk.positions:
            try:
                sol_amt = float(event.get("solAmount", 0) or 0)
                token_amt = float(event.get("tokenAmount", 0) or 0)
                if sol_amt > 0 and token_amt > 0:
                    implied_price_sol = sol_amt / token_amt
                    sol_price = await self.market.get_sol_price()
                    if sol_price > 0:
                        implied_price_usd = implied_price_sol * sol_price
                        action, sell_pct = self.risk.check_exits(
                            mint, implied_price_usd, noisy=True)
                        if action != "none" and sell_pct > 0:
                            # WS prices are noisy (single-trade implied) —
                            # block stop-losses unless explicitly enabled
                            if "stop" not in action or self.cfg.ws_stop_loss_enabled:
                                log.warning(f"  WS-EXIT: {self.risk.positions[mint].symbol} — {action}")
                                self._spawn_sell(mint, sell_pct, f"ws_{action}")
                            return
            except Exception as e:
                log.debug(f"  WS price check error: {e}")

        # Graduation strategy: track tokens approaching graduation
        # (actual buys happen post-graduation via _run_post_graduation_buy)
        if "graduation" in self.strategies_enabled:
            await self.graduation.evaluate_trade(event)

    # ══════════════════════════════════════════════════════════
    # MOMENTUM STRATEGY (POLLING)
    # ══════════════════════════════════════════════════════════

    async def _run_momentum(self):
        """3-stage discovery pipeline:
        Stage 1: MintPool scores candidates from WebSocket (free, in-memory)
        Stage 2: On-chain activity check via RPC (individual calls, limited to 10)
        Stage 3: On-chain bonding curve momentum via getMultipleAccounts (1 RPC call)
        Fallback: DexScreener boosted tokens (only if pipeline yields nothing)
        """
        signals = []

        # ── Stage 1: Get top candidates from scored MintPool ─────
        top_candidates = self.mint_pool.get_best_candidates(
            count=20, min_age=90, prefer_young=True)

        if top_candidates:
            # ── Stage 2: On-chain activity filter (individual RPC calls) ───
            # Ten checks per cycle: the six best-ranked, plus four that
            # rotate through the rest so lower-ranked tokens still get looked at.
            head, tail = top_candidates[:6], top_candidates[6:]
            rotating = []
            if tail:
                start = self._pipeline_rotation % len(tail)
                rotating = (tail[start:] + tail[:start])[:4]
                self._pipeline_rotation += 4
            active_mints = []
            for mint in head + rotating:
                try:
                    tx_count = await self.onchain.get_recent_tx_count(mint, seconds=300)
                    # Fallback to WebSocket trade count when RPC fails/returns 0
                    if tx_count == 0:
                        tx_count = self.mint_pool.get_trade_count(mint)
                    if tx_count >= 5:
                        active_mints.append((mint, tx_count))
                except Exception:
                    pass

            if active_mints:
                active_mints.sort(key=lambda x: x[1], reverse=True)
                verified = active_mints[:12]

                log.info(f"  Pipeline: {self.mint_pool.size} pool → "
                         f"{len(top_candidates)} scored → "
                         f"{len(active_mints)} active → evaluating {len(verified)}")

                # Subscribe active tokens for wallet discovery observation
                if hasattr(self, 'wallet_discovery'):
                    for mint, tx_count in active_mints:
                        if mint not in self._discovery_subs and \
                           mint not in self.stream._watched_tokens and \
                           len(self._discovery_subs) < 30:
                            await self.stream.subscribe_token(mint)
                            self._discovery_subs[mint] = time.time() + 300

                # ── Stage 3: Batched bonding curve momentum (1 HTTP request) ──
                pipeline_signals = await self._evaluate_onchain_momentum(verified)
                signals.extend(pipeline_signals)
            else:
                log.info(f"  Pipeline: {self.mint_pool.size} pool → "
                         f"{len(top_candidates)} scored → 0 active (all <5 txs)")

        # ── Boost fallback disabled (adversarial selection, ~30% win rate) ──
        if not signals:
            log.debug("  Pipeline empty — no boost fallback")

        for signal in signals:
            if signal.mint in self.risk.positions:
                continue
            log.info(f"  MOMENTUM: {signal.symbol} ({signal.confidence:.0%}) — {signal.reason}")
            self._spawn(self._execute_buy(signal))

    async def _evaluate_onchain_momentum(self, active_mints: list[tuple[str, int]]) -> list[Signal]:
        """Stage 3: Evaluate momentum using on-chain bonding curve data.

        Uses batched getMultipleAccounts (1 RPC call) to read all curves at once.
        Compares current state to creation-time data in MintPool.
        """
        # Filter candidates first (no RPC needed)
        candidates = []
        for mint, tx_count in active_mints:
            cand = self.mint_pool.candidates.get(mint)
            if not cand:
                continue
            if mint in self.risk.positions:
                continue
            if self.db and self.db.is_blacklisted(mint):
                continue
            # Skip tokens from blacklisted creators
            if cand.creator and self.db and self.db.is_creator_blacklisted(cand.creator):
                continue
            if cand.v_sol <= 0:
                continue
            candidates.append((mint, tx_count, cand))

        if not candidates:
            return []

        # Batch fetch all bonding curve states (1 RPC call)
        mint_list = [mint for mint, _, _ in candidates]
        states = await self.onchain.get_bonding_curve_states_batch(mint_list)

        signals = []
        evaluated = 0
        skipped_reasons = {"no_state": 0, "graduated": 0, "low_growth": 0, "low_score": 0}

        for mint, tx_count, cand in candidates:
            state = states.get(mint)
            if not state:
                skipped_reasons["no_state"] += 1
                continue
            if state.complete:
                skipped_reasons["graduated"] += 1
                continue

            evaluated += 1
            # Keep the stored reading until it is old enough to compare
            # against; replacing it every cycle would leave nothing to
            # compare with when cycles run faster than CURVE_OBS_MIN_SEC.
            prev_obs = self._curve_obs.get(mint)
            if prev_obs is None or time.time() - prev_obs[0] >= self.CURVE_OBS_MIN_SEC:
                self._curve_obs[mint] = (time.time(), state.virtual_sol)

            # ── Compute momentum signals ─────────────────────
            age_sec = time.time() - cand.time
            age_min = age_sec / 60

            # Curve growth: compare current virtual SOL to creation-time
            growth_pct = ((state.virtual_sol - cand.v_sol) / cand.v_sol) * 100

            # Hard minimum: skip tokens below minimum curve growth
            growth_rate = growth_pct / max(age_min, 0.5)
            if growth_pct < self.cfg.min_momentum_curve_growth:
                skipped_reasons["low_growth"] += 1
                if growth_pct >= 8:
                    log.info(f"  NEAR-MISS: {cand.symbol} | growth={growth_pct:.0f}% | "
                             f"rate={growth_rate:.0f}%/m | SOL={state.real_sol:.1f} | "
                             f"txs={tx_count} | curve={state.pct:.0f}%")
                continue

            # Hard minimum: require sustained growth rate (filters whale-only pumps)
            if growth_rate < self.cfg.min_momentum_growth_rate:
                skipped_reasons["low_growth"] += 1
                log.info(f"  FILTERED-RATE: {cand.symbol} | rate={growth_rate:.1f}%/m "
                         f"(need {self.cfg.min_momentum_growth_rate}) | "
                         f"growth={growth_pct:.0f}% | age={age_min:.1f}m")
                continue

            # Growth×age ceiling: dynamic cap prevents buying peaked tokens
            # A 5-min token at 100% is fine (20%/m), but 10-min at 200%+ is peaked
            max_growth_for_age = age_min * self.cfg.peaked_age_multiplier
            if growth_pct > max_growth_for_age and age_min > 3:
                skipped_reasons["peaked"] = skipped_reasons.get("peaked", 0) + 1
                log.info(f"  AGE-CAP: {cand.symbol} | growth={growth_pct:.0f}% > "
                         f"cap={max_growth_for_age:.0f}% (age={age_min:.1f}m × {self.cfg.peaked_age_multiplier:.0f}) | "
                         f"SOL={state.real_sol:.1f}")
                continue

            # Hard minimum: skip tokens below minimum SOL in curve
            if state.real_sol < self.cfg.min_momentum_sol_in_curve:
                skipped_reasons["low_growth"] += 1
                log.info(f"  FILTERED-SOL: {cand.symbol} | SOL={state.real_sol:.1f} (need {self.cfg.min_momentum_sol_in_curve}) | "
                         f"growth={growth_pct:.0f}% | txs={tx_count}")
                continue

            # Hard minimum: skip tokens below minimum tx count
            if tx_count < self.cfg.min_momentum_txns:
                skipped_reasons["low_growth"] += 1
                log.info(f"  FILTERED-TXN: {cand.symbol} | txs={tx_count} (need {self.cfg.min_momentum_txns}) | "
                         f"SOL={state.real_sol:.1f} | growth={growth_pct:.0f}%")
                continue

            # ── Score the token (rate-based, age-aware) ────────
            score = 0.0
            reasons = []

            # ── Age: strongly favor younger tokens ──
            # Younger = closer to start of wave 1, less likely peaked
            if 2 <= age_min <= 4:
                score += 0.20
                reasons.append(f"Age: {age_min:.0f}m (early)")
            elif 4 < age_min <= 6:
                score += 0.15
                reasons.append(f"Age: {age_min:.0f}m (prime)")
            elif 6 < age_min <= 8:
                score += 0.05
                reasons.append(f"Age: {age_min:.0f}m")
            elif 8 < age_min <= 10:
                score -= 0.05
                reasons.append(f"Age: {age_min:.0f}m (late)")
            else:
                score -= 0.15
                reasons.append(f"Age: {age_min:.0f}m (stale)")

            # ── Growth RATE (primary signal) ──
            if growth_rate >= 15:
                score += 0.30
                reasons.append(f"Rate: {growth_rate:.0f}%/m")
            elif growth_rate >= 8:
                score += 0.25
                reasons.append(f"Rate: {growth_rate:.0f}%/m")
            elif growth_rate >= 4:
                score += 0.20
                reasons.append(f"Rate: {growth_rate:.0f}%/m")
            elif growth_rate >= 2:
                score += 0.10
                reasons.append(f"Rate: {growth_rate:.0f}%/m")

            # ── Absolute growth (penalize peaked tokens) ──
            if growth_pct >= self.cfg.peaked_growth_cap_pct:
                skipped_reasons["peaked"] = skipped_reasons.get("peaked", 0) + 1
                log.info(f"  PEAKED: {cand.symbol} | growth={growth_pct:.0f}% >= "
                         f"{self.cfg.peaked_growth_cap_pct:.0f}% | "
                         f"SOL={state.real_sol:.1f} | curve={state.pct:.0f}%")
                continue
            elif growth_pct >= 100:
                # Between 2x and the hard cap: allowed, but penalised.
                # growth_pct is VIRTUAL-SOL growth and price scales with its
                # square, so +100% here is already ~4x in price.
                score -= 0.05
                reasons.append(f"Extended +{growth_pct:.0f}%")
            elif 60 <= growth_pct < 100:
                score += 0.05
                reasons.append(f"Curve +{growth_pct:.0f}%")
            elif 30 <= growth_pct < 60:
                score += 0.15
                reasons.append(f"Curve +{growth_pct:.0f}%")
            else:
                score += 0.05
                reasons.append(f"Curve +{growth_pct:.1f}%")

            # ── Transaction density (rate per minute) ──
            # tx_count covers the last 5 minutes at most, so that is the
            # longest span it can be averaged over.
            tx_rate = tx_count / min(max(age_min, 1), 5)
            if tx_rate >= 8:
                score += 0.20
                reasons.append(f"TxRate: {tx_rate:.1f}/m")
            elif tx_rate >= 4:
                score += 0.15
                reasons.append(f"TxRate: {tx_rate:.1f}/m")
            elif tx_rate >= 2:
                score += 0.10
                reasons.append(f"TxRate: {tx_rate:.1f}/m")
            else:
                score += 0.05

            # ── Buyer uniqueness (Sybil detection) ──
            uniq_ratio, uniq_count, total_buys, avg_sol = self.mint_pool.get_buyer_uniqueness(mint)
            if total_buys >= 10:  # Need meaningful sample size
                # Micro-buy detection: avg < 0.01 SOL = likely wash trading
                if avg_sol < 0.01 and total_buys >= 15:
                    skipped_reasons["sybil"] = skipped_reasons.get("sybil", 0) + 1
                    log.info(f"  SYBIL-MICRO: {cand.symbol} | avg={avg_sol:.4f} SOL/buy "
                             f"({total_buys} buys) — likely volume inflation")
                    continue
                elif uniq_ratio < 0.30:
                    # Hard block: same wallets buying repeatedly
                    skipped_reasons["sybil"] = skipped_reasons.get("sybil", 0) + 1
                    log.info(f"  SYBIL: {cand.symbol} | unique={uniq_count}/{total_buys} "
                             f"({uniq_ratio:.0%}) — likely wash trading")
                    continue
                elif uniq_ratio >= 0.80:
                    score += 0.15
                    reasons.append(f"Uniq: {uniq_ratio:.0%} ({uniq_count}/{total_buys})")
                elif uniq_ratio >= 0.60:
                    score += 0.10
                    reasons.append(f"Uniq: {uniq_ratio:.0%}")
                elif uniq_ratio >= 0.40:
                    score += 0.0  # neutral
                else:
                    score -= 0.10
                    reasons.append(f"LowUniq: {uniq_ratio:.0%}")

            # ── SOL commitment (lower thresholds) ──
            if state.real_sol >= 20:
                score += 0.20
                reasons.append(f"SOL: {state.real_sol:.1f}")
            elif state.real_sol >= 10:
                score += 0.15
                reasons.append(f"SOL: {state.real_sol:.1f}")
            elif state.real_sol >= 5:
                score += 0.10
                reasons.append(f"SOL: {state.real_sol:.1f}")
            elif state.real_sol >= 2:
                score += 0.05
                reasons.append(f"SOL: {state.real_sol:.1f}")

            # ── High curve penalty ──
            if state.pct >= 80:
                score -= 0.10
                reasons.append(f"Curve: {state.pct:.0f}%")

            confidence = min(score, 1.0)

            if confidence >= self.cfg.min_momentum_confidence:
                # Sustained growth: the curve must be higher than it was at
                # an earlier pipeline cycle. A token seen for the first time
                # waits one cycle; "did not fall in 2 seconds" proves nothing.
                obs_age = (time.time() - prev_obs[0]) if prev_obs else None
                if obs_age is None or obs_age < self.CURVE_OBS_MIN_SEC \
                        or obs_age > self.CURVE_OBS_MAX_SEC:
                    log.info(f"  2PT-WAIT: {cand.symbol} — no earlier reading to "
                             f"compare with yet")
                    continue
                if state.virtual_sol <= prev_obs[1]:
                    log.info(f"  2PT-FAIL: {cand.symbol} — no growth in {obs_age:.0f}s "
                             f"({prev_obs[1]:.2f}→{state.virtual_sol:.2f} SOL)")
                    continue
                # Then a short recheck so we do not buy into a drop.
                first_sol = state.virtual_sol
                await asyncio.sleep(2)
                state2 = await self.onchain.get_bonding_curve_state(mint)
                if not state2 or state2.complete:
                    log.info(f"  2PT-FAIL: {cand.symbol} — no state or graduated on recheck")
                    continue
                delta_sol = state2.virtual_sol - first_sol
                if delta_sol < 0:
                    log.info(f"  2PT-FAIL: {cand.symbol} — curve declining "
                             f"({first_sol:.2f}→{state2.virtual_sol:.2f} SOL)")
                    continue
                # Use the fresher state for price estimation
                state = state2

                # Estimate price for paper trading
                if state.virtual_tokens > 0:
                    price_sol = state.virtual_sol / (state.virtual_tokens / 1e6)
                    mcap_sol = price_sol * 1e9  # 1B token supply
                else:
                    mcap_sol = state.real_sol * 3

                signals.append(Signal(
                    mint=mint,
                    symbol=cand.symbol,
                    strategy="momentum",
                    confidence=confidence,
                    reason="PIPELINE | " + " | ".join(reasons),
                    raw_event={"marketCapSol": mcap_sol, "mint": mint},
                    creator=cand.creator,
                ))
                if mint not in self._notified_candidates:
                    self._notified_candidates.add(mint)
                    self.notify.candidate_alert(
                        symbol=cand.symbol, mint=mint,
                        confidence=confidence, sol_in_curve=state.real_sol,
                        txns=tx_count, curve_growth=growth_pct,
                        curve_pct=state.pct,
                    )
            else:
                skipped_reasons["low_score"] += 1

        # Forget readings for tokens that have left the pool
        if len(self._curve_obs) > 500:
            self._curve_obs = {m: o for m, o in self._curve_obs.items()
                               if m in self.mint_pool.candidates}

        if evaluated > 0 and signals:
            log.info(f"  Pipeline Stage 3: {len(signals)}/{evaluated} tokens have momentum")
        elif evaluated > 0:
            # Show rejection summary at INFO level for diagnostics
            parts = [f"{k}={v}" for k, v in skipped_reasons.items() if v > 0]
            if parts:
                log.info(f"  Pipeline Stage 3: 0/{evaluated} passed ({', '.join(parts)})")

        # Sort by confidence, take top 3
        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals[:3]

    # ══════════════════════════════════════════════════════════
    # RPC WALLET POLLING (catches Raydium/Jupiter trades)
    # ══════════════════════════════════════════════════════════

    WALLET_POLL_MAX_WALLETS = 8       # wallets polled over RPC per cycle
    WALLET_POLL_SIG_LIMIT = 15        # recent signatures fetched per wallet
    WALLET_POLL_MAX_NEW = 8           # new transactions parsed per wallet per cycle
    # Swaps into these are not token buys worth copying
    COPY_IGNORE_MINTS = frozenset({
        "So11111111111111111111111111111111111111112",    # wrapped SOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    })

    def _wallets_to_poll(self) -> list[str]:
        """Manual wallets first, then auto-discovered ones, in a stable order."""
        if not hasattr(self, 'copy_trade'):
            return []
        watched = self.copy_trade.wallets
        manual = [w for w in self.cfg.copy_wallets if w in watched]
        auto = sorted(w for w in watched if w not in set(manual))
        return (manual + auto)[:self.WALLET_POLL_MAX_WALLETS]

    def _start_wallet_poll(self):
        """Run the wallet poll in the background: it makes many RPC calls
        and must not delay position checks in the main loop."""
        task = getattr(self, "_wallet_poll_task", None)
        if task and not task.done():
            return   # previous cycle still running
        self._wallet_poll_task = asyncio.create_task(self._poll_wallet_transactions())

    async def _poll_wallet_transactions(self):
        """Poll copy-trade wallets via RPC to catch non-pump.fun trades.

        PumpPortal WebSocket only reports bonding curve trades. This poller uses
        getSignaturesForAddress + getTransaction to detect trades on Raydium,
        Jupiter, and other DEXes that the WebSocket misses.
        """
        for wallet in self._wallets_to_poll():
            try:
                # Strict call: an RPC failure raises, so it cannot be
                # mistaken for "this wallet did nothing".
                result = await self.onchain._rpc_call_strict("getSignaturesForAddress", [
                    wallet, {"limit": self.WALLET_POLL_SIG_LIMIT, "commitment": "confirmed"}
                ])
                self._wallet_poll_ok += 1
                result = result or []

                if wallet not in self._wallet_seen_sigs:
                    # First poll — just record signatures, don't process
                    self._wallet_seen_sigs[wallet] = {
                        s.get("signature", "") for s in result if s.get("signature")
                    }
                    continue

                seen = self._wallet_seen_sigs[wallet]
                new_sigs = [
                    s.get("signature", "") for s in result
                    if s.get("signature") and s["signature"] not in seen
                    and not s.get("err")  # skip failed txs
                ]
                if len(new_sigs) >= self.WALLET_POLL_SIG_LIMIT:
                    log.info(f"  Wallet {wallet[:8]}... made more trades than one "
                             f"poll can see — some were missed")

                # Oldest first, so a buy is processed before its sell
                for sig in list(reversed(new_sigs))[:self.WALLET_POLL_MAX_NEW]:
                    seen.add(sig)
                    try:
                        tx_data = await self.onchain._rpc_call("getTransaction", [
                            sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                  "maxSupportedTransactionVersion": 0}
                        ])
                        if not tx_data:
                            continue
                        await self._parse_wallet_tx(wallet, tx_data, sig)
                    except Exception as e:
                        log.debug(f"  Wallet tx parse error: {e}")
                # Anything not processed this cycle is dropped, not replayed late
                seen.update(new_sigs)

                # Cap seen signatures to prevent unbounded growth
                if len(seen) > 200:
                    keep = {s.get("signature", "") for s in result}
                    self._wallet_seen_sigs[wallet] = keep

            except Exception as e:
                self._wallet_poll_fail += 1
                now = time.time()
                if now - self._wallet_poll_warned_at > 300:
                    self._wallet_poll_warned_at = now
                    log.warning(f"  Wallet poll failed for {wallet[:8]}...: "
                                f"{type(e).__name__}: {e} "
                                f"({self._wallet_poll_fail} failures so far)")

    async def _parse_wallet_tx(self, wallet: str, tx_data: dict, sig: str):
        """Parse a wallet transaction to detect token buys/sells.

        Looks at SOL balance changes and token balance changes to determine
        if the wallet bought or sold a token.
        """
        meta = tx_data.get("meta", {})
        if not meta:
            return

        # Get SOL balance change for the wallet
        tx_msg = tx_data.get("transaction", {}).get("message", {})
        account_keys = tx_msg.get("accountKeys", [])

        # Find wallet index in account keys
        wallet_idx = None
        for i, key in enumerate(account_keys):
            addr = key.get("pubkey", key) if isinstance(key, dict) else key
            if addr == wallet:
                wallet_idx = i
                break

        if wallet_idx is None:
            return

        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        if wallet_idx >= len(pre_balances) or wallet_idx >= len(post_balances):
            return

        sol_change = (post_balances[wallet_idx] - pre_balances[wallet_idx]) / 1e9

        # Get token balance changes
        pre_token = meta.get("preTokenBalances", [])
        post_token = meta.get("postTokenBalances", [])

        # Build maps of owner -> mint -> balance
        def token_map(balances):
            m = {}
            for b in balances:
                owner = b.get("owner", "")
                mint = b.get("mint", "")
                amount = float(b.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                if owner == wallet and mint:
                    m[mint] = amount
            return m

        pre_tokens = token_map(pre_token)
        post_tokens = token_map(post_token)

        # Find token changes
        all_mints = set(pre_tokens.keys()) | set(post_tokens.keys())
        for mint in all_mints:
            pre_amt = pre_tokens.get(mint, 0)
            post_amt = post_tokens.get(mint, 0)
            token_change = post_amt - pre_amt

            if abs(token_change) < 0.001:
                continue
            if mint in self.COPY_IGNORE_MINTS:
                continue   # a swap into SOL or a stablecoin, not a token buy

            if token_change > 0 and sol_change < -0.01:
                # Bought tokens (SOL decreased, tokens increased)
                sol_spent = abs(sol_change)
                log.info(f"  RPC-COPY: {wallet[:8]}... BOUGHT {mint[:8]}... "
                         f"for {sol_spent:.3f} SOL ({post_amt:.0f} tokens) [tx: {sig[:12]}]")
                synthetic_event = {
                    "traderPublicKey": wallet,
                    "txType": "buy",
                    "mint": mint,
                    "solAmount": sol_spent,
                    "newTokenBalance": post_amt,
                    "marketCapSol": 0,  # unknown from tx
                    "symbol": mint[:8],
                }
                await self._on_account_trade(synthetic_event)

            elif token_change < 0 and sol_change > 0.01:
                # Sold tokens (SOL increased, tokens decreased)
                sol_received = sol_change
                log.info(f"  RPC-COPY: {wallet[:8]}... SOLD {mint[:8]}... "
                         f"for {sol_received:.3f} SOL ({post_amt:.0f} remaining) [tx: {sig[:12]}]")
                synthetic_event = {
                    "traderPublicKey": wallet,
                    "txType": "sell",
                    "mint": mint,
                    "solAmount": sol_received,
                    "newTokenBalance": post_amt,
                    "symbol": mint[:8],
                }
                await self._on_account_trade(synthetic_event)

    # ══════════════════════════════════════════════════════════
    # GRADUATION POLLING (fills the gap between 0% and 96%)
    # ══════════════════════════════════════════════════════════

    async def _run_graduation_poll(self):
        """Poll MintPool candidates for near-graduation tokens.

        Tracks tokens approaching graduation so the post-graduation
        buyer can catch them once they migrate to PumpSwap.
        """
        if not hasattr(self, 'graduation'):
            return

        # Get older candidates (2-20 min) — these have had time to accumulate
        candidates = self.mint_pool.get_best_candidates(count=30, min_age=120)
        if not candidates:
            return

        checked = 0
        near_grad = 0
        for mint in candidates[:25]:
            if mint in self.risk.positions or mint in self.graduation.bought:
                continue
            if self.db and self.db.is_blacklisted(mint):
                continue

            try:
                curve_pct = await self.onchain.get_bonding_curve_pct(mint)
                if curve_pct <= 0:
                    continue
                checked += 1
                self.graduation.note_curve(mint, curve_pct)

                if curve_pct >= self.graduation.min_curve:
                    near_grad += 1
                    # Feed into graduation strategy for tracking
                    cand = self.mint_pool.candidates.get(mint)
                    event = {
                        "mint": mint,
                        "symbol": cand.symbol if cand else mint[:8],
                        "traderPublicKey": cand.creator if cand else "",
                        "marketCapSol": cand.market_cap_sol if cand else 0,
                        "vSolInBondingCurve": 0,
                    }
                    await self.graduation.evaluate_trade(event)
            except Exception as e:
                log.debug(f"  Grad poll error for {mint[:8]}: {e}")

        if checked > 0:
            log.info(f"  Grad poll: checked {checked} candidates, "
                     f"{near_grad} near graduation (>={self.graduation.min_curve}%)")

    async def _run_graduation_hot_poll(self):
        """Fast-poll tokens at 80%+ — detect graduation events for post-grad buy."""
        if not hasattr(self, 'graduation'):
            return

        hot_mints = self.graduation.get_hot_list()
        if not hot_mints:
            return

        for mint in hot_mints[:10]:
            if mint in self.risk.positions or mint in self.graduation.bought:
                continue
            try:
                curve_pct = await self.onchain.get_bonding_curve_pct(mint)
                if curve_pct <= 0:
                    self.graduation.watching.pop(mint, None)
                    self.graduation._watching_times.pop(mint, None)
                    continue

                # Update the watching dict
                self.graduation.note_curve(mint, curve_pct)
                self.graduation.watching[mint] = curve_pct
                self.graduation._watching_times[mint] = time.time()

                # Detect graduation (100% curve = bonding curve complete).
                # Only for tokens we saw below 100% ourselves.
                if (curve_pct >= 100 and mint not in self.graduation._graduated_at
                        and mint in self.graduation._seen_below):
                    cand = self.mint_pool.candidates.get(mint)
                    symbol = cand.symbol if cand else mint[:8]
                    self.notify.graduation_alert(symbol, mint, curve_pct)
                    event = {
                        "mint": mint,
                        "symbol": symbol,
                        "traderPublicKey": cand.creator if cand else "",
                        "marketCapSol": cand.market_cap_sol if cand else 0,
                        "vSolInBondingCurve": 0,
                    }
                    # This will register the graduation in the strategy
                    await self.graduation.evaluate_trade(event)
                    log.info(f"  GRAD-HOT: {symbol} graduated at {curve_pct:.1f}% — "
                             f"queued for post-graduation buy")
            except Exception as e:
                log.debug(f"  Grad hot poll error for {mint[:8]}: {e}")

        # Check for post-graduation buys ready to execute
        await self._run_post_graduation_buy()

    async def _run_post_graduation_buy(self):
        """Execute buys for tokens that have graduated and pool has established."""
        if not hasattr(self, 'graduation'):
            return

        ready = self.graduation.get_post_graduation_candidates(
            min_wait=60, max_wait=300,
        )
        if not ready:
            return

        for event_data in ready:
            mint = event_data["mint"]
            symbol = event_data.get("symbol", mint[:8])
            creator = event_data.get("traderPublicKey", "")

            if mint in self.risk.positions or mint in self._closed_mints:
                self.graduation.mark_bought(mint)
                continue
            if self.db and self.db.is_blacklisted(mint):
                self.graduation.mark_bought(mint)
                continue

            # Verify the token has DEX liquidity before buying
            try:
                token_data = await self.market.get_token(mint)
                if not token_data:
                    log.info(f"  POST-GRAD: {symbol} — no DEX data yet, waiting...")
                    continue
                if token_data.liquidity_usd < 5000:
                    log.info(f"  POST-GRAD: {symbol} — low liquidity ${token_data.liquidity_usd:,.0f}, waiting...")
                    continue

                # A fresh graduate has a pool that is minutes old. An old pool
                # means this token graduated long ago and was mis-detected.
                pool_age_min = (token_data.age_minutes
                                if token_data.pair_created_at > 0 else None)
                if pool_age_min is None or pool_age_min > self.cfg.grad_max_pool_age_min:
                    age_txt = "unknown" if pool_age_min is None else f"{pool_age_min:.0f}m"
                    log.info(f"  POST-GRAD: {symbol} — pool age {age_txt} "
                             f"(max {self.cfg.grad_max_pool_age_min:.0f}m), not a fresh graduate")
                    self.graduation.mark_bought(mint)
                    continue

                # A 5-minute change of 0 with no trades is missing data, not
                # a calm market. Wait for real trading before judging entry.
                txns_5m = token_data.buys_5m + token_data.sells_5m
                if txns_5m < self.cfg.grad_min_txns_5m:
                    log.info(f"  POST-GRAD: {symbol} — only {txns_5m} trades in 5m, waiting...")
                    continue

                # Prefer post-dump entries: tokens with negative/low 5m%
                # have already shed FOMO buyers and found a floor.
                # Skip extreme FOMO tops and deep dumps.
                change_5m = token_data.price_change_5m
                if change_5m > 150:
                    log.info(f"  POST-GRAD: {symbol} — FOMO top ({change_5m:+.0f}% 5m), skipping")
                    self.graduation.mark_bought(mint)
                    continue
                if change_5m < -30:
                    log.info(f"  POST-GRAD: {symbol} — too deep ({change_5m:+.0f}% 5m), skipping")
                    self.graduation.mark_bought(mint)
                    continue

                # Confidence/sizing gradient: post-dump floor gets largest
                # position, chasing gets smallest. These buckets are a
                # heuristic; there is no recorded trade data behind them.
                if -15 <= change_5m <= -5:
                    conf = 0.85  # sweet spot: post-dump floor
                elif change_5m < -15:
                    conf = 0.65  # deeper dip, less data, cautious
                elif change_5m <= 10:
                    conf = 0.75  # calm entry
                elif change_5m <= 50:
                    conf = 0.55  # chasing, minimal edge
                else:
                    conf = 0.50  # high FOMO, smallest size

                log.info(f"  POST-GRAD BUY: {symbol} | liq=${token_data.liquidity_usd:,.0f} | "
                         f"5m={change_5m:+.1f}% | mcap=${token_data.market_cap:,.0f}")

                signal = Signal(
                    mint=mint, symbol=symbol, strategy="graduation",
                    confidence=conf,
                    reason=f"Post-graduation | Liq: ${token_data.liquidity_usd:,.0f} | "
                           f"5m: {change_5m:+.1f}%",
                    token_data=token_data, creator=creator,
                )
                self.graduation.mark_bought(mint)
                self._spawn(self._execute_buy(signal))

            except Exception as e:
                log.debug(f"  Post-grad buy error for {symbol}: {e}")

    # ══════════════════════════════════════════════════════════
    # POSITION MONITORING
    # ══════════════════════════════════════════════════════════

    async def _check_positions(self):
        await self._reconcile_positions()
        to_exit = []

        for mint, pos in list(self.risk.positions.items()):
            # Update curve % for held positions (non-graduated, not stale)
            if not pos.graduated and pos.age_sec > 5:
                await self._update_position_curve(mint, pos)

            token = await self.market.get_token(mint)

            if not token:
                price_usd = await self._get_onchain_price_usd(mint)
                if price_usd and price_usd > 0:
                    if pos.entry_price <= 0:
                        # Opened without a price: arm the stop-loss from here
                        self.risk.set_entry_price(mint, price_usd)
                    action, sell_pct = self.risk.check_exits(mint, price_usd)
                    if action != "none":
                        to_exit.append((mint, action, sell_pct))
                    continue

                # No price source at all — time-based exits only
                if pos.auto_sell_at > 0 and time.time() >= pos.auto_sell_at:
                    to_exit.append((mint, "auto_sell", 100))
                elif self.cfg.max_hold_minutes > 0 and pos.age_min >= self.cfg.max_hold_minutes:
                    to_exit.append((mint, "time_exit", 100))
                continue

            if pos.entry_price <= 0 and token.price_usd > 0:
                self.risk.set_entry_price(mint, token.price_usd, token.price_sol)
            action, sell_pct = self.risk.check_exits(mint, token.price_usd)
            if action != "none":
                to_exit.append((mint, action, sell_pct))

        # Sells run in the background so one slow exit cannot delay the
        # checks (and exits) of every other position.
        for mint, reason, sell_pct in to_exit:
            self._spawn_sell(mint, sell_pct, reason,
                             followup=(reason == "take_profit_graduated"))

    async def _update_position_curve(self, mint: str, pos):
        """Update bonding curve % for a held position (throttled to every ~10s)."""
        if time.time() - pos.curve_updated_at < 10:
            return  # Don't hammer RPC
        try:
            curve_pct = await self.onchain.get_bonding_curve_pct(mint)
            if curve_pct > 0:
                pos.update_curve(curve_pct)
                # Keep rug detector in sync with current curve %
                if hasattr(self, 'rug_detector'):
                    self.rug_detector.update_curve_pct(mint, curve_pct)
                # Detect graduation
                if curve_pct >= 100 and not pos.graduated:
                    self.risk.mark_graduated(mint, grace=True)
                    self._graduated_cache[mint] = True
                    log.info(f"  {pos.symbol} graduated — curve hit {curve_pct:.1f}% (5min grace active)")
        except Exception:
            pass

    async def _check_post_grad_liquidity(self, mint: str):
        """After graduated TP2 partial sell, check DEX liquidity.

        If liquidity is too thin (<$5K), sell the rest immediately
        to avoid a post-graduation dump (like BABYPUNCH -93%).
        """
        pos = self.risk.positions.get(mint)
        if not pos:
            return
        try:
            resp = await self.market.http.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    # No DEX pair found — sell immediately
                    log.warning(f"  POST-GRAD: {pos.symbol} no DEX pair — selling remainder")
                    await self._execute_sell(mint, 100, "post_grad_no_liquidity")
                    return
                # Find the pair with highest liquidity
                best_liq = 0
                for pair in pairs:
                    liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                    if liq > best_liq:
                        best_liq = liq
                if best_liq < 5000:
                    log.warning(f"  POST-GRAD: {pos.symbol} low liquidity (${best_liq:.0f}) — selling remainder")
                    await self._execute_sell(mint, 100, "post_grad_low_liquidity")
                else:
                    log.info(f"  POST-GRAD: {pos.symbol} holding remainder — "
                             f"liquidity ${best_liq:,.0f} (40% trailing stop)")
        except Exception as e:
            log.debug(f"  Post-grad liquidity check error: {e}")
            # On error, keep holding — conservative approach

    async def _get_onchain_price_usd(self, mint: str) -> float:
        """Get token price in USD from bonding curve state.

        Fallback for tokens DexScreener hasn't indexed yet.
        Uses cached bonding curve state (8s TTL) to avoid excessive RPC calls.
        """
        try:
            state = await self.onchain.get_bonding_curve_state(mint)
            if not state or state.virtual_tokens <= 0:
                return 0
            # price per whole token in SOL
            price_sol = state.virtual_sol / (state.virtual_tokens / 1e6)
            sol_price = await self.market.get_sol_price()
            if sol_price <= 0:
                return 0
            return price_sol * sol_price
        except Exception:
            return 0

    # ══════════════════════════════════════════════════════════
    # TRADE EXECUTION
    # ══════════════════════════════════════════════════════════

    # ── Non-blocking wallet access ────────────────────────────
    # The live wallet uses a synchronous RPC client. Calling it directly from
    # a coroutine freezes every other task (WebSocket, stop-loss checks) for
    # the duration of the HTTP request, so it runs in a worker thread.

    async def _sol_balance(self) -> float:
        if self.cfg.paper_trading:
            return self.wallet.get_sol_balance()
        return await asyncio.to_thread(self.wallet.get_sol_balance)

    async def _token_balance(self, mint: str) -> float | None:
        """Token balance, or None when it could not be read (NOT zero)."""
        if self.cfg.paper_trading:
            return self.wallet.get_token_balance(mint)
        return await asyncio.to_thread(self.wallet.get_token_balance, mint)

    async def _token_amount(self, mint: str) -> tuple[float, int, int] | None:
        """(ui_amount, raw_amount, decimals), or None when unreadable."""
        if self.cfg.paper_trading:
            return self.wallet.get_token_amount(mint)
        return await asyncio.to_thread(self.wallet.get_token_amount, mint)

    async def _zero_balance_confirmed(self, mint: str) -> bool:
        """Second, delayed read. True only if an RPC again reports 0 tokens."""
        await asyncio.sleep(self.ZERO_RECHECK_DELAY)
        bal = await self._token_balance(mint)
        return bal is not None and bal <= 0

    # ── Background trade tasks ────────────────────────────────

    def _spawn(self, coro) -> asyncio.Task:
        """Run a trade off the caller's loop so slow confirmations cannot
        stall the WebSocket reader or the position monitor."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_task_done)
        return task

    def _bg_task_done(self, task: asyncio.Task):
        self._bg_tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error(f"  Background trade task failed: {task.exception()}",
                      exc_info=task.exception())

    def _spawn_sell(self, mint: str, sell_pct: int, reason: str,
                    followup: bool = False):
        """Start a sell in the background unless an equal-or-larger sell of
        the same mint is already in flight. A full exit is still allowed to
        queue behind a partial one, so emergency exits are never dropped."""
        if self._selling.get(mint, 0) >= sell_pct:
            return
        # After a failed sell, every automatic trigger (stop-loss, fast check,
        # WebSocket, stuck retry) is held back for a while. Without this an
        # unsellable token is retried continuously, and each failed attempt
        # can still cost a priority fee. An emergency exit may go through
        # once if the attempt that failed was not itself an emergency.
        until, was_emergency = self._sell_backoff.get(mint, (0.0, False))
        if time.time() < until and (was_emergency or not self._is_emergency(reason)):
            return
        self._selling[mint] = sell_pct
        self._spawn(self._sell_task(mint, sell_pct, reason, followup))

    @staticmethod
    def _is_emergency(reason: str) -> bool:
        return ("stop" in reason or "rug" in reason.lower()
                or "fast" in reason or "ws_" in reason)

    async def _sell_task(self, mint: str, sell_pct: int, reason: str,
                         followup: bool):
        try:
            await self._execute_sell(mint, sell_pct, reason)
            # Post-graduation liquidity check: after TP2 graduated partial,
            # verify DEX liquidity supports holding the remainder
            if followup and mint in self.risk.positions:
                await self._check_post_grad_liquidity(mint)
        finally:
            if self._selling.get(mint) == sell_pct:
                self._selling.pop(mint, None)

    # ── Buy ───────────────────────────────────────────────────

    async def _execute_buy(self, signal: Signal, auto_sell_sec: int = 0,
                           delay: float = 0):
        if delay and delay > 0:
            await asyncio.sleep(delay)
        # Notify-only mode: log the signal and notify via Telegram, but don't trade
        if self.cfg.notify_only:
            log.info(f"  [NOTIFY-ONLY] Would buy {signal.symbol} ({signal.confidence:.0%}) — {signal.reason}")
            self.notify.send(
                f"📋 <b>SIGNAL</b> (no trade)\n"
                f"${signal.symbol} | {signal.strategy} | {signal.confidence:.0%}\n"
                f"{signal.reason}\n"
                f'🔗 <a href="https://pump.fun/{signal.mint}">pump.fun</a>'
            )
            return
        lock = self._get_mint_lock(signal.mint)
        if lock.locked():
            # A trade for this mint is already in flight. Drop the signal
            # rather than queue it: a queued buy would fire late, on stale
            # information, the moment the first attempt failed.
            log.debug(f"  Buy signal dropped: {signal.symbol} already has a trade in flight")
            return
        async with lock:
            await self._execute_buy_inner(signal, auto_sell_sec)

    async def _execute_buy_inner(self, signal: Signal, auto_sell_sec: int = 0):
        mint = signal.mint
        if mint in self.risk.positions or mint in self._reserved:
            return

        # Hard no-re-entry: never buy a token we already traded
        if mint in self._closed_mints:
            log.debug(f"  No re-entry: {signal.symbol} already traded")
            return

        # Per-mint re-entry cooldown
        cooldown_expiry = self._mint_cooldowns.get(mint, 0)
        if cooldown_expiry > time.time():
            remaining = (cooldown_expiry - time.time()) / 60
            log.info(f"  Cooldown: {signal.symbol} blocked for {remaining:.0f}m more")
            return

        sol_bal = await self._sol_balance()
        if mint in self.risk.positions or mint in self._reserved:
            return

        # Sizing + risk check + reservation happen with no await in between,
        # so two concurrent buys cannot both pass the caps.
        reserved_sol = sum(self._reserved.values())
        reserved_count = len(self._reserved)
        size = self.risk.calc_size(sol_bal, signal.confidence, signal.strategy,
                                   wallet=signal.creator,
                                   reserved_sol=reserved_sol)
        if size < 0.005:
            return

        # Risk check (includes cooldown)
        can, reason = self.risk.can_open(size, signal.strategy,
                                         reserved_sol=reserved_sol,
                                         reserved_count=reserved_count)
        if not can:
            # Exempt high-confidence signals from cooldown
            if ("cooldown" in reason.lower()
                    and signal.confidence >= self.cfg.cooldown_exempt_confidence):
                log.info(f"  Cooldown bypassed: {signal.symbol} confidence "
                         f"{signal.confidence:.0%} >= {self.cfg.cooldown_exempt_confidence:.0%}")
            else:
                log.info(f"  Risk block: {reason}")
                return

        self._reserved[mint] = size
        try:
            await self._execute_buy_checked(signal, size, auto_sell_sec)
        finally:
            self._reserved.pop(mint, None)

    async def _safety_gate(self, signal: Signal) -> bool:
        """Run safety checks. True = OK to buy.

        Data that could not be read is treated as UNKNOWN: with
        SAFETY_FAIL_CLOSED (default) the buy is skipped, but the token is not
        blacklisted — an RPC hiccup says nothing about the token.
        """
        mint = signal.mint
        fail_closed = self.cfg.safety_fail_closed
        is_snipe = signal.strategy == "sniper"

        if signal.token_data:
            try:
                safety = await self.safety.check(
                    signal.token_data, is_snipe=is_snipe,
                    creator=signal.creator,
                )
            except Exception as e:
                log.warning(f"  Safety check exception — skipping trade: {e}")
                return False
            if not safety.passed:
                if safety.unverified:
                    log.info(f"  Safety SKIP: on-chain data unavailable — "
                             f"{signal.symbol} (not blacklisted)")
                    return False
                log.warning(f"  Safety FAIL: {safety.warnings[:3]}")
                self.db.blacklist_token(mint, "; ".join(safety.warnings[:3]))
                return False
            return True

        # No token_data — run on-chain safety (freeze auth + holders + creator)
        try:
            oc = await self.onchain.analyze(mint, signal.creator)
        except Exception as e:
            if is_snipe or fail_closed:
                log.warning(f"  Safety SKIP: on-chain check failed — {signal.symbol}: {e}")
                return False
            log.debug(f"  On-chain safety check error: {e}")
            return True

        if not oc.mint_read_ok:
            if is_snipe or fail_closed:
                log.info(f"  Safety SKIP: mint account unreadable — "
                         f"{signal.symbol} (not blacklisted)")
                return False
        elif not oc.freeze_authority_revoked:
            log.warning(f"  Safety FAIL: freeze authority active — {signal.symbol}")
            self.db.blacklist_token(mint, "freeze authority active")
            return False

        if not oc.holders_read_ok:
            if fail_closed:
                log.info(f"  Safety SKIP: holder data unavailable — "
                         f"{signal.symbol} (not blacklisted)")
                return False
        else:
            # Young pump.fun tokens have concentrated holders — use relaxed
            # thresholds: 70% for sniper, 50% for pipeline, 30% for mature tokens
            max_holder = (self.cfg.sniper_max_top_holder_pct if is_snipe
                          else self.cfg.pipeline_max_top_holder_pct)
            if oc.top_holder_pct > max_holder:
                log.warning(f"  Safety FAIL: top holder {oc.top_holder_pct:.0f}% — {signal.symbol}")
                self.db.blacklist_token(mint, f"top holder {oc.top_holder_pct:.0f}%")
                return False
            # Minimum holder count
            if 0 < oc.holder_count < self.cfg.min_holder_count:
                log.warning(f"  Safety FAIL: only {oc.holder_count} holders — {signal.symbol}")
                return False

        if signal.creator:
            if not oc.creator_read_ok:
                if fail_closed:
                    log.info(f"  Safety SKIP: creator history unavailable — "
                             f"{signal.symbol} (not blacklisted)")
                    return False
            else:
                # Hard block serial deployers
                if oc.creator_token_count >= self.cfg.max_creator_tx_count:
                    log.warning(f"  Safety FAIL: creator {signal.creator[:8]} has "
                                f"{oc.creator_token_count}+ txs — {signal.symbol}")
                    self.db.blacklist_creator(signal.creator,
                                              f"serial deployer ({oc.creator_token_count} txs)")
                    return False
                # Soft log for moderately active creators
                if oc.creator_token_count >= 50:
                    log.info(f"  Creator active ({oc.creator_token_count} recent txs) — {signal.symbol}")
        return True

    def _paper_costs(self, trade_sol: float, jupiter: bool,
                     emergency: bool = False) -> tuple[float, float]:
        """(fixed SOL, percentage) a live trade of this kind would cost.

        The fixed part mirrors what the live path sends: the priority fee
        (after the size cap) and the Jito tip on pump.fun trades, a flat
        assumed network fee on Jupiter trades. The percentage (PAPER_FEE_PCT)
        stands for the pump.fun and PumpPortal fees and is an assumption.
        """
        if jupiter:
            fixed = self.cfg.paper_jupiter_fee_sol
        else:
            fixed = self.cfg.priority_fee_for(trade_sol, emergency=emergency)
        if self.cfg.use_jito and not emergency:
            fixed += self.cfg.jito_tip_sol
        return fixed, self.cfg.paper_fee_pct

    def _log_fee_overhead(self):
        """Say up front how much of each position the fixed fees eat."""
        for name, cap in (("momentum", self.cfg.momentum_max_buy_sol),
                          ("copy_trade", self.cfg.copy_max_buy_sol),
                          ("sniper", self.cfg.max_buy_sol)):
            if name not in self.strategies_enabled or cap <= 0:
                continue
            tip = self.cfg.jito_tip_sol if self.cfg.use_jito else 0.0
            round_trip = 2 * (self.cfg.priority_fee_for(cap) + tip)
            pct = round_trip / cap * 100
            line = (f"  Fixed fees for {name}: ~{round_trip:.4f} SOL per round trip "
                    f"= {pct:.0f}% of a {cap:g} SOL position")
            if pct >= 15:
                log.warning(line + " — the trade must gain that much to break even")
            else:
                log.info(line)

    async def _execute_buy_checked(self, signal: Signal, size: float,
                                   auto_sell_sec: int = 0):
        mint = signal.mint

        # Safety check (DexScreener + on-chain)
        if not await self._safety_gate(signal):
            return

        # Pre-entry creator balance check — large SOL balance = dump risk
        if signal.creator and signal.strategy == "momentum":
            try:
                creator_bal = await self.onchain.get_account_sol_balance(signal.creator)
                if creator_bal is not None and creator_bal > 50:
                    log.warning(f"  Safety FAIL: creator {signal.creator[:12]}... "
                                f"holds {creator_bal:.1f} SOL — dump risk — {signal.symbol}")
                    return
            except Exception:
                pass  # Non-blocking — proceed if check fails

        # Execute
        if self.cfg.paper_trading:
            token_data = signal.token_data or await self.market.get_token(mint)
            if token_data:
                price_usd = token_data.price_usd
                price_sol = token_data.price_sol
            else:
                mcap_sol = float(
                    signal.raw_event.get("marketCapSol", 30)
                    if signal.raw_event else 30
                )
                # pump.fun standard supply is 1 billion tokens
                supply = 1e9
                price_sol = mcap_sol / supply
                sol_price = await self.market.get_sol_price()
                price_usd = price_sol * sol_price

            # Model the costs a live buy would pay; the outlay stays `size`.
            via_jupiter = signal.strategy == "graduation" and self.cfg.use_jupiter
            fixed, fee_pct = self._paper_costs(size, via_jupiter)
            net = (max(size - fixed, 0) * (1 - fee_pct / 100)
                   * (1 - self.cfg.paper_slippage_pct / 100))
            tokens = (net / price_sol) if price_sol > 0 else 0
            if tokens <= 0:
                return
            log.info(f"  Paper costs: {size - net:.4f} of {size:.4f} SOL "
                     f"({fixed:.4f} fixed + {fee_pct:g}% fees + "
                     f"{self.cfg.paper_slippage_pct:g}% slippage)")

            ok = self.wallet.buy(mint, signal.symbol, size, tokens, price_usd)
            if ok:
                is_grad = signal.strategy == "graduation"
                self.risk.open(
                    mint, signal.symbol, price_usd, price_sol,
                    tokens, size, signal.strategy, auto_sell_sec,
                    creator=signal.creator, graduated=is_grad,
                )
                if is_grad:
                    self._graduated_cache[mint] = True
                self.notify.trade_alert("buy", signal.symbol, size,
                                         reason=signal.reason)
        else:
            await self._live_buy(signal, size, auto_sell_sec)

        # Start rug monitoring if position was opened
        if mint in self.risk.positions:
            await self._on_position_opened(mint, signal.creator)

    async def _live_buy(self, signal: Signal, size: float, auto_sell_sec: int):
        """Send a live buy and record exactly what happened.

        Invariants:
        - a buy that landed ALWAYS becomes a tracked position;
        - a buy is never sent twice (see PumpPortal.buy / JupiterSwap.buy_token);
        - a position is never opened for a buy known to have failed.
        """
        mint = signal.mint
        # Post-graduation buys go through Jupiter (token is on PumpSwap/Raydium)
        via_jupiter = signal.strategy == "graduation" and self.cfg.use_jupiter
        log.info(f"  Buying {signal.symbol} for {size:.4f} SOL...")
        bal_before = await self._token_balance(mint)

        if via_jupiter:
            result = await self.jupiter.buy_token(
                mint, size,
                slippage_bps=self.cfg.jupiter_slippage_bps,
                use_jito=self.pump.use_jito,
            )
        else:
            slippage = (self.cfg.momentum_slippage_pct
                        if signal.strategy == "momentum"
                        else self.cfg.slippage_pct)
            buy_fee = self.cfg.priority_fee_for(size)
            if buy_fee < self.cfg.priority_fee_sol:
                log.info(f"  Priority fee capped at {buy_fee:.5f} SOL "
                         f"({self.cfg.max_fee_pct_of_trade:g}% of the trade; "
                         f"configured {self.cfg.priority_fee_sol:g})")
            result = await self.pump.buy(
                mint, size,
                slippage=slippage,
                priority_fee=buy_fee,
            )

        if result.failed:
            # A definite failure should leave no tokens. One cheap look guards
            # against a misreported failure leaving tokens untracked.
            bal = await self._token_balance(mint)
            if bal is None or bal <= (bal_before or 0):
                log.error(f"  Buy FAILED for {signal.symbol}")
                return
            log.warning(f"  Buy reported failed but {bal} tokens of "
                        f"{signal.symbol} arrived — recording it")
            result = TradeResult("unknown", result.sig, result.via_lightning,
                                 result.sigs)

        # Persist immediately so a crash from here on cannot orphan tokens.
        self.db.save_position(
            mint=mint, symbol=signal.symbol, entry_price=0,
            entry_sol_price=0, tokens=0, sol_invested=size,
            entry_time=time.time(), strategy=signal.strategy,
            tx_signature=result.sig,
            extra={"creator": signal.creator, "pending": True,
                   "graduated": via_jupiter},
        )

        fill = await self._resolve_buy(signal, size, result, bal_before)
        if fill is None:
            self.db.delete_position(mint)
            return

        tokens, sol_spent, price_usd, price_sol, unverified = fill
        self.risk.open(
            mint, signal.symbol, price_usd, price_sol,
            tokens, sol_spent, signal.strategy, auto_sell_sec,
            tx_signature=result.sig or "phantom", creator=signal.creator,
            graduated=via_jupiter, buy_unverified=unverified,
        )
        if via_jupiter:
            self._graduated_cache[mint] = True
        self.notify.trade_alert("buy", signal.symbol, sol_spent,
                                 reason=signal.reason, tx=result.sig)

    async def _resolve_buy(self, signal: Signal, estimated_sol: float,
                           result: TradeResult, bal_before: float | None):
        """Work out what a buy actually did.

        Returns (tokens, sol_spent, price_usd, price_sol, unverified), or None
        when the buy did not happen. The transaction itself is the primary
        source; the wallet balance is the fallback.
        """
        mint = signal.mint
        owner = self.wallet.pubkey
        tokens = 0.0
        sol_spent = estimated_sol
        sol_for_tokens = estimated_sol
        landed = result.confirmed

        if result.sig:
            tx = await self.pump.get_tx_result(
                result.sig, retries=4 if result.confirmed else 2)
            out = parse_tx_outcome(tx, owner, mint)
            if out is not None:
                if out.err is not None:
                    log.warning(f"  Buy tx failed on-chain for {signal.symbol}: {out.err}")
                    return None
                if not out.owner_found:
                    if result.via_lightning:
                        self._lightning_wallet_mismatch(signal.symbol, result.sig)
                        return None
                else:
                    landed = True
                    if out.sol_delta < 0:
                        sol_spent = -out.sol_delta
                        # Subtract known fees so entry price reflects only the
                        # SOL that bought tokens, not tx overhead.
                        fee_deduction = out.fee + self.cfg.jito_tip_sol
                        sol_for_tokens = sol_spent - fee_deduction
                        if sol_for_tokens <= 0:
                            sol_for_tokens = sol_spent
                        log.info(f"  Entry: {sol_spent:.6f} total, "
                                 f"{fee_deduction:.6f} fees, "
                                 f"{sol_for_tokens:.6f} for tokens")
                    if out.token_delta > 0:
                        tokens = out.token_post

        last_bal = None
        if tokens <= 0:
            # No usable tx data — watch the wallet. With no signature at all
            # (Lightning timed out) the trade may still land any time until
            # its blockhash expires, so that case waits the full window.
            if landed:
                tries, pause = self.BUY_BALANCE_POLLS_CONFIRMED
            elif result.sig:
                tries, pause = self.BUY_BALANCE_POLLS_UNKNOWN
            else:
                tries, pause = self.BUY_BALANCE_POLLS_NO_SIG
            for i in range(tries):
                last_bal = await self._token_balance(mint)
                if last_bal is not None and last_bal > (bal_before or 0):
                    tokens = last_bal
                    landed = True
                    break
                if i < tries - 1:
                    await asyncio.sleep(pause)

        unverified = False
        if tokens <= 0:
            if landed:
                # Confirmed, but the balance is not visible yet. Track it
                # anyway; reconciliation fills the token count in.
                log.warning(f"  {signal.symbol}: buy confirmed but token balance "
                            f"not readable yet — tracking, will reconcile")
            elif last_bal is None:
                # Outcome unknown AND the wallet is unreadable. We may hold
                # tokens: track it as unverified rather than risk an orphan.
                unverified = True
                log.error(f"  {signal.symbol}: buy outcome UNKNOWN and balance "
                          f"unreadable — tracking as unverified")
                self.notify.send(
                    f"WARNING: buy of {signal.symbol} ({mint[:16]}...) has an "
                    f"unknown outcome (RPC unreachable). Tracking it as "
                    f"unverified until the wallet can be read."
                )
            else:
                log.warning(f"  {signal.symbol}: buy outcome unknown and no "
                            f"tokens arrived — not opening a position")
                self.notify.send(
                    f"Buy of {signal.symbol} was not confirmed and no tokens "
                    f"arrived — no position opened."
                )
                return None

        price_usd = 0.0
        price_sol = 0.0
        if tokens > 0 and sol_for_tokens > 0:
            price_sol = sol_for_tokens / tokens
            sol_price = await self.market.get_sol_price()
            price_usd = price_sol * sol_price if sol_price > 0 else 0.0

        if price_usd <= 0:
            # Bonding-curve price: available immediately, unlike DexScreener
            price_usd = await self._get_onchain_price_usd(mint) or 0.0
            sol_price = await self.market.get_sol_price()
            price_sol = price_usd / sol_price if price_usd > 0 and sol_price > 0 else 0.0

        if price_usd <= 0:
            token_data = await self.market.get_token(mint)
            price_usd = token_data.price_usd if token_data else 0.0
            price_sol = token_data.price_sol if token_data else 0.0

        if price_usd <= 0:
            log.warning(f"  {signal.symbol}: no entry price yet — it will be set "
                        f"from the first price read (stop-loss arms then)")

        return tokens, sol_spent, price_usd, price_sol, unverified

    def _lightning_wallet_mismatch(self, symbol: str, sig: str):
        """A Lightning trade was not paid by the bot's wallet: the API key
        belongs to a different wallet, so the bot cannot see or manage what
        Lightning buys. Stop using Lightning and tell the operator."""
        log.error(f"  LIGHTNING WALLET MISMATCH on {symbol}: tx {sig} was not "
                  f"signed by {self.wallet.pubkey} — disabling Lightning")
        self.pump.api_key = ""
        self.notify.send(
            f"CRITICAL: Lightning trade for {symbol} was NOT made from the bot "
            f"wallet ({self.wallet.pubkey[:8]}...). PUMPPORTAL_API_KEY belongs "
            f"to a different wallet. Lightning is disabled for this session. "
            f"That trade sits in the Lightning wallet and is NOT managed. "
            f"TX: {sig}"
        )

    # ── Position reconciliation ───────────────────────────────

    async def _reconcile_positions(self, force: bool = False):
        """Fill in token counts for positions opened without them (unverified
        buys, crash-recovered pending rows), and drop buys that never landed."""
        if self.cfg.paper_trading:
            return
        for mint, pos in list(self.risk.positions.items()):
            if pos.tokens > 0 and not pos.buy_unverified:
                continue
            now = time.time()
            if not force and now - pos.last_balance_check < 10:
                continue
            pos.last_balance_check = now
            bal = await self._token_balance(mint)
            if bal is None:
                continue
            if bal > 0:
                pos.tokens = bal
                pos.buy_unverified = False
                self.risk.persist(pos)
                log.info(f"  Reconciled {pos.symbol}: {bal:.0f} tokens on-chain")
            elif (pos.age_sec > self.RECONCILE_GRACE_SEC
                    and mint not in self._selling
                    and mint in self.risk.positions
                    and await self._zero_balance_confirmed(mint)):
                await self._close_no_tokens(mint, pos, "reconcile")

    async def _sol_from_sell_sigs(self, mint: str) -> float:
        """SOL received from earlier sell attempts whose outcome we never
        confirmed, read from the transactions themselves."""
        total = 0.0
        for sig in self._sell_sigs.get(mint, [])[-10:]:
            try:
                tx = await self.pump.get_tx_result(sig)
                out = parse_tx_outcome(tx, self.wallet.pubkey, mint)
                if out and out.err is None and out.owner_found and out.sol_delta > 0:
                    total += out.sol_delta
            except Exception as e:
                log.debug(f"  Sell sig lookup error: {e}")
        return total

    async def _close_no_tokens(self, mint: str, pos, reason: str):
        """Close a position whose tokens are confirmed gone (two RPC reads).

        - Unverified buy with no sell on record: it never landed. Drop it
          without booking a loss on SOL that was never spent.
        - Otherwise the tokens were sold: credit whatever earlier unconfirmed
          sell transactions actually paid out.
        """
        if mint not in self.risk.positions:
            return
        recovered_sol = await self._sol_from_sell_sigs(mint)
        self._sell_sigs.pop(mint, None)
        self._partial_hold.pop(mint, None)
        self._clear_stuck(mint)

        if pos.buy_unverified and recovered_sol <= 0:
            self.risk.discard(mint, f"{reason}: buy never landed")
            self.notify.send(f"{pos.symbol}: buy never landed — position dropped, no loss booked.")
            await self._on_position_closed(mint)
            return

        log.warning(f"  {pos.symbol}: no tokens on-chain — closing "
                    f"(recovered {recovered_sol:.4f} SOL from earlier sells)")
        pnl = self.risk.close(mint, recovered_sol, f"{reason}_no_tokens")
        # Only feed the loss into cooldown/blacklist logic when we know what
        # was received; an unexplained zero balance is not evidence of a rug.
        await self._on_position_closed(
            mint, pnl=pnl if recovered_sol > 0 else None,
            strategy=pos.strategy, creator=pos.creator)
        self.notify.trade_alert("sell", pos.symbol, recovered_sol, pnl=pnl,
                                 reason=f"{reason} (tokens already gone)",
                                 daily_pnl=self.risk.daily_pnl)

    async def _on_ws_reconnect(self):
        """Audit all held positions after WebSocket reconnect.

        During disconnection we missed trade events, so:
        1. Live mode: verify on-chain token balances (positions may have been
           sold externally or tokens transferred).
        2. Re-subscribe to token trades for rug detection.
        3. Force a position check for exit triggers.
        """
        positions = list(self.risk.positions.items())
        if not positions:
            log.info("  WS reconnect audit: no positions to check")
            return

        log.info(f"  WS reconnect audit: checking {len(positions)} positions...")

        if not self.cfg.paper_trading:
            for mint, pos in positions:
                try:
                    balance = await self._token_balance(mint)
                    if balance is None:
                        log.warning(f"  WS audit: {pos.symbol} balance check failed (RPC error) — skipping")
                        continue
                    if balance <= 0:
                        if (pos.age_sec > self.RECONCILE_GRACE_SEC
                                and mint not in self._selling
                                and await self._zero_balance_confirmed(mint)):
                            log.warning(f"  WS audit: {pos.symbol} has 0 balance — closing")
                            await self._close_no_tokens(mint, pos, "ws_audit")
                    elif abs(balance - pos.tokens) / max(pos.tokens, 1) > 0.5:
                        log.warning(f"  WS audit: {pos.symbol} balance mismatch "
                                    f"(expected {pos.tokens:.0f}, got {balance:.0f}) — updating")
                        pos.tokens = balance
                        pos.buy_unverified = False
                        self.risk.persist(pos)
                except Exception as e:
                    log.debug(f"  WS audit balance check error for {mint[:8]}: {e}")

        # Re-subscribe tokens for rug detection
        if hasattr(self, 'rug_detector'):
            for mint in list(self.risk.positions.keys()):
                try:
                    await self.stream.subscribe_token(mint)
                except Exception:
                    pass

        # Force a position check cycle
        try:
            await self._check_positions()
        except Exception as e:
            log.debug(f"  WS audit position check error: {e}")

        log.info(f"  WS reconnect audit complete")

    async def _on_position_opened(self, mint: str, creator: str = ""):
        """Hook called after a position is successfully opened."""
        # For copy trades `creator` is the wallet we copied. Its sells are
        # handled by copy-sell mirroring; treating it as the token's creator
        # would turn them into "creator sold" rug exits and blacklist it.
        pos = self.risk.positions.get(mint)
        if pos and pos.strategy == "copy_trade":
            creator = ""
        if hasattr(self, 'rug_detector') and self.cfg.rug_detection:
            curve_sol = 0
            curve_pct = 0.0
            graduated = False
            try:
                # Check graduation first — graduated tokens have 0 curve SOL
                # which is normal (funds migrated to Raydium)
                graduated = await self._is_graduated(mint,
                    self.risk.positions.get(mint)) if mint in self.risk.positions else False
                if not graduated:
                    result = await self.onchain.analyze(mint)
                    curve_sol = result.bonding_curve_sol
                    curve_pct = await self.onchain.get_bonding_curve_pct(mint)
            except Exception:
                pass
            self.rug_detector.start_monitoring(mint, creator, curve_sol,
                                                graduated=graduated,
                                                curve_pct=curve_pct)
            await self.stream.subscribe_token(mint)

    async def _on_position_closed(self, mint: str, pnl: float = None,
                                   strategy: str = "", creator: str = ""):
        """Hook called after a position is fully closed."""
        # No-re-entry — never buy this token again
        self._closed_mints.add(mint)

        # Record cooldown for losing trades to prevent re-entry
        if pnl is not None and pnl < 0 and self.cfg.mint_cooldown_minutes > 0:
            self._mint_cooldowns[mint] = time.time() + self.cfg.mint_cooldown_minutes * 60
            log.info(f"  Cooldown: {mint[:12]}... blocked for {self.cfg.mint_cooldown_minutes}m")

        # For copy trades `creator` holds the wallet we copied, not the
        # token's creator: score that wallet, and never blacklist it as a
        # token creator.
        is_copy = strategy == "copy_trade"

        # Auto-blacklist creator on loss
        if pnl is not None and pnl < 0 and not is_copy:
            if creator and hasattr(self, 'sniper'):
                self.sniper.blacklist_creator(creator)
            # Persist blacklist for significant losses (likely rugs)
            if creator and pnl < -0.005:
                self.db.blacklist_creator(creator, f"loss {pnl:.4f} SOL ({strategy})")
                log.info(f"  Creator {creator[:8]}... blacklisted after {strategy} loss")

        # Wallet scores rank COPY wallets (copy confidence, sizing, promotion
        # and demotion). Token creators are deliberately kept out of them.
        if pnl is not None and creator and is_copy:
            try:
                self.db.update_wallet_score(creator, pnl, 0, mint, pnl >= 0)
            except Exception:
                pass

        if hasattr(self, 'rug_detector'):
            self.rug_detector.stop_monitoring(mint)
            await self.stream.unsubscribe_token(mint)
        self._graduated_cache.pop(mint, None)

    def _get_mint_lock(self, mint: str) -> asyncio.Lock:
        if mint not in self._mint_locks:
            self._mint_locks[mint] = asyncio.Lock()
        return self._mint_locks[mint]

    # ── Sell ──────────────────────────────────────────────────

    async def _execute_sell(self, mint: str, sell_pct: int, reason: str):
        # The lock is deliberately NOT removed here: popping it while another
        # caller waits would hand a third caller a fresh lock and let two
        # sells of the same mint run at once. Idle locks are pruned in the
        # poll loop instead.
        lock = self._get_mint_lock(mint)
        async with lock:
            await self._execute_sell_inner(mint, sell_pct, reason)

    async def _execute_sell_inner(self, mint: str, sell_pct: int, reason: str):
        pos = self.risk.positions.get(mint)
        if not pos:
            return

        is_partial = sell_pct < 100
        log.info(f"  Selling {sell_pct}% of {pos.symbol} ({reason})")

        if self.cfg.paper_trading:
            token_data = await self.market.get_token(mint)
            if token_data:
                sell_tokens = pos.tokens * (sell_pct / 100)
                sol_received_raw = sell_tokens * token_data.price_sol
                slippage = self.cfg.paper_slippage_pct
                if "stop_loss" in reason or "rug" in reason.lower():
                    slippage *= 2  # emergency exits get worse execution
                emergency = self._is_emergency(reason)
                via_jupiter = pos.graduated and self.cfg.use_jupiter
                fixed, fee_pct = self._paper_costs(
                    pos.sol_invested * sell_pct / 100, via_jupiter, emergency)
                sol_received = max(
                    sol_received_raw * (1 - slippage / 100) * (1 - fee_pct / 100)
                    - fixed, 0.0)
                log.info(f"  Paper costs: {slippage:.1f}% slippage + {fee_pct:g}% fees + "
                         f"{fixed:.4f} SOL fixed "
                         f"({sol_received_raw:.4f} → {sol_received:.4f} SOL)")
                pnl = self.wallet.sell(mint, sell_tokens, sol_received,
                                        token_data.price_usd)
                if is_partial:
                    self.risk.partial_close(mint, sol_received=sol_received,
                                            reason=reason)
                    # Update RiskManager position to reflect partial sell
                    # (PaperWallet.sell already decremented its own pos.tokens)
                    remaining_tokens = pos.tokens * (1 - sell_pct / 100)
                    pos.tokens = remaining_tokens
                    pos.sol_invested *= (1 - sell_pct / 100)
                    self.db.update_position(
                        mint, tokens=pos.tokens,
                        sol_invested=pos.sol_invested,
                    )
                else:
                    pnl = self.risk.close(mint, sol_received, reason)
                    await self._on_position_closed(mint, pnl=pnl,
                                                   strategy=pos.strategy, creator=pos.creator)

                self.notify.trade_alert("sell", pos.symbol, sol_received,
                                         pnl=pnl, reason=reason,
                                         balance=self.wallet.sol_balance,
                                         daily_pnl=self.risk.daily_pnl)
            else:
                if not is_partial:
                    self.risk.close(mint, 0, reason)
                    await self._on_position_closed(mint, pnl=0,
                                                   strategy=pos.strategy, creator=pos.creator)
                self.notify.trade_alert("sell", pos.symbol, 0,
                                         pnl=0, reason=f"{reason} (no price data)",
                                         balance=self.wallet.sol_balance,
                                         daily_pnl=self.risk.daily_pnl)
        else:
            await self._live_sell(mint, pos, sell_pct, is_partial, reason)

    async def _get_sol_received(self, sol_before: float, pos,
                                retries: int = 3) -> float:
        """Fallback estimate of sell proceeds from the wallet's SOL change.

        Only used when the sell transaction itself cannot be read; it is
        inaccurate if another trade moves the wallet balance at the same time.
        """
        for attempt in range(retries):
            sol_after = await self._sol_balance()
            sol_received = max(sol_after - sol_before, 0)
            if sol_received > 0:
                return sol_received
            if attempt < retries - 1:
                wait = 2 * (attempt + 1)  # 2s, 4s
                log.info(f"  Balance unchanged after sell of {pos.symbol}, "
                         f"retrying in {wait}s ({attempt + 1}/{retries})")
                await asyncio.sleep(wait)
        return 0.0

    async def _live_sell(self, mint: str, pos, sell_pct: int,
                         is_partial: bool, reason: str):
        """Sell on-chain and act only on what verifiably happened.

        Invariant: a live position is closed ONLY when the sell transaction is
        confirmed or an RPC read shows the tokens left the wallet. A sell we
        cannot verify leaves the position open to be retried — never written
        off while the tokens may still be held.
        """
        # For emergency exits, use cached graduation flag (skip RPC call)
        # For normal exits, do the full check
        is_emergency = self._is_emergency(reason)

        # An earlier partial sell with an unknown outcome may still land.
        # Sending another partial now could sell twice as much as intended.
        if is_partial and not await self._partial_hold_cleared(mint, pos):
            return

        amount = await self._token_amount(mint)   # None = unreadable, not zero
        if amount is not None and amount[0] <= 0:
            if (pos.age_sec >= self.ZERO_CLOSE_MIN_AGE_SEC
                    and await self._zero_balance_confirmed(mint)):
                await self._close_no_tokens(mint, pos, reason)
                return
            # Too soon after the buy to trust a zero read (RPC lag)
            log.warning(f"  {pos.symbol}: balance reads 0 — not selling yet")
            if not is_partial:
                self._mark_stuck(mint, pos, emergency=is_emergency)
            return

        bal_before = amount[0] if amount else None
        sol_before = await self._sol_balance()
        graduated = pos.graduated if is_emergency else await self._is_graduated(mint, pos)

        sell_slippage = 40 if is_emergency else self.cfg.slippage_pct
        trade_sol = pos.sol_invested * sell_pct / 100
        sell_fee = self.cfg.priority_fee_for(trade_sol, emergency=is_emergency)
        uncapped = (min(self.cfg.priority_fee_sol * 2, 0.01) if is_emergency
                    else self.cfg.priority_fee_sol)
        if sell_fee < uncapped:
            log.info(f"  Priority fee capped at {sell_fee:.5f} SOL "
                     f"({self.cfg.max_fee_pct_of_trade:g}% of the trade; "
                     f"would be {uncapped:g})")
        resend = not is_partial   # re-sending a partial sell would oversell

        if graduated and self.cfg.use_jupiter:
            routes = ["jupiter", "pump", "pump_max"]
        else:
            routes = ["pump", "jupiter", "pump_max"]

        state = "not_sold"
        for route in routes:
            if route == "jupiter":
                if not self.cfg.use_jupiter:
                    continue
                if not (pos.graduated or await self._is_graduated(mint, pos)):
                    continue
                if amount is None:
                    continue   # cannot size a Jupiter sell without the balance
                # Raw amount and decimals come from the token account itself;
                # a separate decimals lookup that fails must never scale this.
                raw_amount = int(amount[1] * sell_pct / 100)
                if raw_amount <= 0:
                    continue
                log.info(f"  Selling {pos.symbol} via Jupiter (graduated)")
                result = await self.jupiter.sell_token(
                    mint, raw_amount,
                    slippage_bps=self.cfg.jupiter_slippage_bps,
                    use_jito=self.pump.use_jito,
                    resend_on_timeout=resend,
                )
            elif route == "pump":
                result = await self.pump.sell(
                    mint, sell_pct=sell_pct,
                    slippage=sell_slippage,
                    priority_fee=sell_fee,
                    skip_jito=is_emergency,
                    resend_on_timeout=resend,
                )
            else:
                # Last resort: max slippage, skip Jito, higher fee
                log.error(f"  Sell FAILED for {pos.symbol} — last-resort retry")
                retry_fee = min(self.cfg.priority_fee_for(trade_sol, emergency=True),
                                pos.sol_invested * 0.15)
                result = await self.pump.sell(
                    mint, sell_pct=sell_pct,
                    slippage=50,
                    priority_fee=retry_fee,
                    skip_jito=True,
                    resend_on_timeout=resend,
                )

            if result.sigs:
                self._sell_sigs.setdefault(mint, []).extend(result.sigs)

            state, sol_received, tokens_left, sig = await self._verify_sell(
                mint, pos, result, bal_before, sol_before, sell_pct, is_partial)

            if state == "sold":
                await self._finalize_sell(mint, pos, sell_pct, is_partial, reason,
                                          route, sol_received, tokens_left, sig)
                return
            if state == "unknown":
                break   # outcome unverifiable — trying another venue could double-sell
            log.warning(f"  Sell via {route} did not go through for {pos.symbol}")

        if state == "unknown" and is_partial:
            # Hold further partial sells until this one can no longer land.
            self._partial_hold[mint] = {
                "until": time.time() + self.PARTIAL_HOLD_SEC,
                "bal_before": bal_before, "sell_pct": sell_pct, "reason": reason,
            }
            log.warning(f"  {pos.symbol}: partial sell outcome unknown — "
                        f"holding further partial sells for {self.PARTIAL_HOLD_SEC}s")
            return
        self._mark_stuck(mint, pos, unknown=(state == "unknown"),
                         emergency=is_emergency)

    async def _verify_sell(self, mint: str, pos, result: TradeResult,
                           bal_before: float | None, sol_before: float,
                           sell_pct: int, is_partial: bool):
        """Decide whether a sell really happened.

        Returns (state, sol_received, tokens_left, signature) where state is
        "sold", "not_sold" or "unknown".
        """
        owner = self.wallet.pubkey

        if result.confirmed and result.sig:
            tx = await self.pump.get_tx_result(result.sig, retries=4)
            out = parse_tx_outcome(tx, owner, mint)
            if out is None:
                # Confirmed by signature status but the tx body is not
                # readable: fall back to wallet deltas.
                sol = await self._get_sol_received(sol_before, pos)
                bal = await self._token_balance(mint)
                self._forget_sell_sigs(mint, result.sigs)
                return "sold", sol, bal, result.sig
            if out.err is None and out.owner_found:
                self._forget_sell_sigs(mint, result.sigs)
                return "sold", max(out.sol_delta, 0.0), out.token_post, result.sig
            if out.err is None and result.via_lightning:
                self._lightning_wallet_mismatch(pos.symbol, result.sig)
                return "unknown", 0.0, None, result.sig
            # err is set: it did not execute — let the balance decide below

        # Not confirmed: the token balance is the judge.
        tries = 1 if result.failed else self.SELL_BALANCE_POLLS
        bal = None
        for i in range(tries):
            bal = await self._token_balance(mint)
            if bal is not None and bal_before is not None and bal_before > 0:
                sold_fraction = (bal_before - bal) / bal_before
                if sold_fraction >= (sell_pct / 100) * 0.5:
                    # It landed despite the status. Read proceeds from the
                    # sell transactions; wallet delta as a last resort.
                    sol = await self._sol_from_sell_sigs(mint)
                    if sol <= 0:
                        sol = await self._get_sol_received(sol_before, pos, retries=1)
                    self._sell_sigs.pop(mint, None)
                    log.info(f"  {pos.symbol}: sell landed (balance "
                             f"{bal_before:.0f} → {bal:.0f})")
                    return "sold", sol, bal, result.sig
            if i < tries - 1:
                await asyncio.sleep(2)

        if result.failed:
            return "not_sold", 0.0, bal, ""
        if bal is None or bal_before is None:
            return "unknown", 0.0, None, result.sig
        # Unknown status, balance unchanged so far. A full sell can safely be
        # retried (a duplicate just fails); a partial one cannot.
        return ("unknown" if is_partial else "not_sold"), 0.0, bal, result.sig

    def _forget_sell_sigs(self, mint: str, sigs: list[str]):
        """Drop signatures whose proceeds have been accounted for."""
        remaining = [s for s in self._sell_sigs.get(mint, []) if s not in sigs]
        if remaining:
            self._sell_sigs[mint] = remaining
        else:
            self._sell_sigs.pop(mint, None)

    async def _partial_hold_cleared(self, mint: str, pos) -> bool:
        """True when a new partial sell may be sent.

        While an earlier partial sell's outcome is unknown, wait until it can
        no longer land; then record it if the balance shows it did.
        """
        hold = self._partial_hold.get(mint)
        if not hold:
            return True
        if time.time() < hold["until"]:
            return False
        bal = await self._token_balance(mint)
        if bal is None:
            return False   # still cannot tell — keep holding
        self._partial_hold.pop(mint, None)
        before = hold.get("bal_before")
        if before and before > 0:
            sold_fraction = (before - bal) / before
            if sold_fraction >= (hold["sell_pct"] / 100) * 0.5:
                sol = await self._sol_from_sell_sigs(mint)
                self._sell_sigs.pop(mint, None)
                log.info(f"  {pos.symbol}: earlier partial sell did land — recording it")
                await self._finalize_sell(mint, pos, hold["sell_pct"], True,
                                          hold["reason"], "pump", sol, bal, "")
                return False
        return True

    async def _finalize_sell(self, mint: str, pos, sell_pct: int,
                             is_partial: bool, reason: str, route: str,
                             sol_received: float, tokens_left: float | None,
                             sig: str):
        """Book a sell that verifiably happened."""
        label = f"{reason} (jupiter)" if route == "jupiter" else reason
        sell_pnl = None
        if is_partial:
            self.risk.partial_close(mint, sol_received=sol_received, reason=label)
            # Keep the position in step with what is actually held, so the
            # portfolio cap and later sells work from real numbers.
            if tokens_left is not None and tokens_left >= 0:
                pos.tokens = tokens_left
            else:
                pos.tokens *= (1 - sell_pct / 100)
            pos.sol_invested *= (1 - sell_pct / 100)
            self.risk.persist(pos)
        else:
            # Credit any earlier sell of this position that landed unconfirmed
            sol_received += await self._sol_from_sell_sigs(mint)
            self._sell_sigs.pop(mint, None)
            self._partial_hold.pop(mint, None)
            self._clear_stuck(mint)
            sell_pnl = self.risk.close(mint, sol_received, reason, sig)
            await self._on_position_closed(mint, pnl=sell_pnl,
                                           strategy=pos.strategy, creator=pos.creator)

        self.notify.trade_alert("sell", pos.symbol, sol_received,
                                 pnl=sell_pnl, reason=label, tx=sig,
                                 balance=await self._sol_balance(),
                                 daily_pnl=self.risk.daily_pnl)

    def _mark_stuck(self, mint: str, pos, unknown: bool = False,
                    emergency: bool = False):
        """Record a sell that could not be completed. The position stays open,
        and further automatic sell attempts are held back (see _spawn_sell)."""
        count = self._stuck_positions.get(mint, 0) + 1
        self._stuck_positions[mint] = count
        delay = (self.SELL_BACKOFF_SEC if count < self.STUCK_BACKOFF_AFTER
                 else self.SELL_BACKOFF_LONG_SEC)
        self._sell_backoff[mint] = (time.time() + delay, emergency)
        why = "outcome unverifiable" if unknown else "sell failed"
        log.error(f"  SELL STUCK: {pos.symbol} ({why}) — still holding, will retry")
        if count == 1:
            self.notify.send(
                f"ALERT: Cannot sell {pos.symbol} ({mint[:16]}...) — {why}. "
                f"Still holding the tokens; will retry."
            )

    def _clear_stuck(self, mint: str):
        self._stuck_positions.pop(mint, None)
        self._stuck_alerted.discard(mint)
        self._sell_backoff.pop(mint, None)

    async def _is_graduated(self, mint: str, pos) -> bool:
        """Check if a token has graduated, with caching.

        An RPC failure is "unknown": it is not cached and never sets the
        graduated flag (which would suspend the stop-loss for 5 minutes).
        """
        if pos.graduated:
            return True
        if mint in self._graduated_cache:
            return self._graduated_cache[mint]
        try:
            graduated = await self.onchain.is_graduated(mint)
        except Exception as e:
            log.debug(f"  Graduation check error: {e}")
            return False
        if graduated is None:
            return False
        self._graduated_cache[mint] = graduated
        if graduated:
            # Grace (stop suppressed) only if we held it on the curve and so
            # are living through the migration. A token that was already
            # graduated when bought keeps its stop-loss.
            self.risk.mark_graduated(mint, grace=0 < pos.curve_pct < 100)
            log.info(f"  {pos.symbol} has graduated from pump.fun")
        return graduated

    # ══════════════════════════════════════════════════════════
    # TELEGRAM COMMANDS
    # ══════════════════════════════════════════════════════════

    async def _poll_telegram_commands(self):
        """Poll Telegram events and route to TelegramUI."""
        if not self.notify.enabled:
            return
        # Synchronous HTTP call — keep it off the event loop
        events = await asyncio.to_thread(self.notify.poll_events)
        for event in events:
            try:
                await self.tg_ui.handle_event(event)
            except Exception as e:
                log.warning(f"Telegram UI error: {e}")

    # ══════════════════════════════════════════════════════════
    # REPORTING
    # ══════════════════════════════════════════════════════════

    async def _report_status(self):
        status = self.risk.get_status()
        sol = await self._sol_balance()

        log.info(f"\n{'='*55}")
        log.info(f"  STATUS | SOL: {sol:.4f} | Positions: {status['positions']}")
        log.info(f"  Invested: {status['invested_sol']:.4f} SOL")
        log.info(f"  Daily PnL: {status['daily_pnl']:+.4f} SOL")
        log.info(f"  Trades: {status['daily_trades']} | Win rate: {status['win_rate']}")

        # Loss streaks
        for strat in self.strategies_enabled:
            streak = self.db.get_loss_streak(strat)
            on_cd, secs = self.db.is_on_cooldown(strat)
            if streak > 0 or on_cd:
                cd_str = f" (cooldown {secs:.0f}s)" if on_cd else ""
                log.info(f"    {strat}: {streak} consecutive losses{cd_str}")

        # Discovery pipeline stats
        log.info(f"  Mint pool: {self.mint_pool.size} tokens")

        # Copy trade stats
        if "copy_trade" in self.strategies_enabled:
            log.info(f"  Copy trade events received: {self._copy_event_count}")
            log.info(f"  Wallet RPC polls: {self._wallet_poll_ok} ok, "
                     f"{self._wallet_poll_fail} failed")
        feed = ("REFUSED by PumpPortal (needs PUMPPORTAL_TRADE_FEED=true and a funded API key)"
                if getattr(self.stream, "trade_feed_rejected", False)
                else f"{getattr(self.stream, 'trade_events', 0)} events")
        log.info(f"  Trade feed: {feed}")

        # Rug detection stats
        if hasattr(self, 'rug_detector'):
            log.info(f"  Rug monitoring: {self.rug_detector.monitored_count} tokens")

        # Wallet discovery stats
        if hasattr(self, 'wallet_discovery'):
            ds = self.wallet_discovery.get_stats()
            log.info(f"  Wallets: {ds['manual_wallets']} manual + "
                     f"{ds['auto_wallets']} auto | "
                     f"{ds['candidates_tracked']} candidates tracked")

        # Tx failure rate
        failures = self.pump.get_recent_failures(10)
        if failures > 0:
            log.warning(f"  TX failures (10min): {failures}")

        # Positions
        for mint, pos in list(self.risk.positions.items()):
            token = await self.market.get_token(mint)
            pnl = pos.pnl_pct(token.price_usd) if token else 0
            log.info(f"    {pos.symbol}: {pos.sol_invested:.4f} SOL | "
                     f"{pnl:+.1f}% | {pos.age_min:.0f}m | {pos.strategy}")

        log.info(f"{'='*55}")

        if self.cfg.paper_trading:
            summary = self.wallet.get_summary()
            balance = summary['sol_balance']
            invested = summary['total_value'] - balance
            initial = self.wallet.initial_sol
            overall_pnl = (balance + invested) - initial
            roi = (overall_pnl / initial * 100) if initial > 0 else 0
            log.info(f"  Paper: {balance:.4f} SOL | "
                     f"ROI: {roi:+.1f}%")
            status['balance'] = balance
            status['roi_pct'] = roi
        else:
            balance = sol
            invested = status.get('invested_sol', 0)
            initial = self.db.get_state("live_initial_balance")
            if not initial:
                initial = balance + invested
                self.db.set_state("live_initial_balance", initial)
            overall_pnl = (balance + invested) - initial
            roi = (overall_pnl / initial * 100) if initial > 0 else 0
            log.info(f"  Live: {balance:.4f} SOL | ROI: {roi:+.1f}%")
            status['balance'] = balance
            status['roi_pct'] = roi
            # Save to DB so monitor can read it
            self.db.set_state("live_balance", balance)

        self.notify.status_alert(status)

    async def _shutdown(self):
        log.info("  Shutting down...")
        # Stop the WebSocket reader and the loops first, so no new trade can
        # start while we are shutting down (and no task outlives the loop).
        main_tasks = [t for t in getattr(self, "_main_tasks", []) if not t.done()]
        poll_task = getattr(self, "_wallet_poll_task", None)
        if poll_task and not poll_task.done():
            main_tasks.append(poll_task)
        for t in main_tasks:
            t.cancel()
        if main_tasks:
            await asyncio.gather(*main_tasks, return_exceptions=True)
        # Let in-flight trades finish recording before closing the database
        pending = [t for t in self._bg_tasks if not t.done()]
        if pending:
            log.info(f"  Waiting for {len(pending)} in-flight trade task(s)...")
            _done, still = await asyncio.wait(pending, timeout=45)
            for t in still:
                t.cancel()
        await self._report_status()

        # Save state
        if self.cfg.paper_trading:
            self.db.set_state("paper_balance", self.wallet.sol_balance)

        # Wallet score summary
        scores = self.db.get_all_wallet_scores()
        if scores:
            log.info("  Wallet Scores:")
            for ws in scores[:10]:
                wr = (ws["winning_trades"]/ws["total_trades"]*100
                      ) if ws["total_trades"] > 0 else 0
                log.info(f"    {ws['wallet'][:12]}... | Score: {ws['score']:.0f} | "
                         f"WR: {wr:.0f}% | PnL: {ws['total_pnl_sol']:+.4f}")

        await self.market.close()
        await self.pump.close()
        await self.jupiter.close()
        await self.onchain.close()
        self.db.close()
        self.notify.close()
        log.info("  Bot stopped")
