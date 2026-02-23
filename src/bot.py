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
    SniperStrategy, MomentumStrategy,
    CopyTradeStrategy, GraduationStrategy, Signal, SellSignal,
)
from notifications import Notifier
from telegram_ui import TelegramUI

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

    def get_best_candidates(self, count: int = 30, min_age: int = 120) -> list[str]:
        """Score and rank candidates for on-chain activity check."""
        now = time.time()
        scored = []
        for m, c in self.candidates.items():
            age = now - c.time
            if age < min_age or age > self.max_age:
                continue

            score = 0.0

            # Age sweet spot: 5-12 min (most likely to have DexScreener data + still early)
            if 300 <= age <= 720:
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

    def __init__(self, config: Config):
        self.cfg = config
        self.running = False

        # Database (crash recovery)
        self.db = Database()

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
        )

        # Jupiter swap client (for selling graduated tokens)
        self.jupiter = JupiterSwap(
            config.rpc_url, keypair,
            slippage_bps=config.jupiter_slippage_bps,
            jito_sender=(self.pump._send_jito_bundle
                         if self.pump.use_jito else None),
        )
        self._graduated_cache: dict[str, bool] = {}

        # Rug detection
        if config.rug_detection:
            self.rug_detector = RugDetector(self.onchain, config)
            log.info("  Rug detection enabled")

        # WebSocket stream
        self.stream = PumpPortalStream()

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
            self.momentum = MomentumStrategy(self.market, db=self.db)
            log.info("  Momentum strategy enabled")

        self._copy_event_count = 0  # Diagnostic counter
        self._wallet_seen_sigs: dict[str, set[str]] = {}  # wallet -> seen tx signatures

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

        # Permanent no-re-entry set (populated when positions close)
        self._closed_mints: set[str] = set()

        # Crash recovery
        recovered = self.risk.recover_positions()
        if recovered:
            log.info(f"  RECOVERED {recovered} positions from last session")
            # Validate recovered positions against on-chain balances (live only)
            if not config.paper_trading:
                to_remove = []
                for mint, pos in list(self.risk.positions.items()):
                    balance = self.wallet.get_token_balance(mint)
                    if balance <= 0:
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
        tasks.append(asyncio.create_task(self._poll_loop()))
        tasks.append(asyncio.create_task(self._fast_position_check()))
        log.info("  All async tasks launched")
        results = await asyncio.gather(*tasks, return_exceptions=True)
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
                        self.db.set_state("live_balance", self.wallet.get_sol_balance())

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

                # Day-boundary reset for daily PnL
                today = datetime.date.today()
                if today != self._last_date:
                    log.info(f"  New day detected — resetting daily PnL")
                    self.risk.reset_daily()
                    self._last_date = today

                # Retry stuck sells (every ~30s) with escalation
                if tick % 10 == 0 and hasattr(self, '_stuck_positions') and self._stuck_positions:
                    for stuck_mint in list(self._stuck_positions.keys()):
                        if stuck_mint not in self.risk.positions:
                            self._stuck_positions.pop(stuck_mint, None)
                            continue
                        retries = self._stuck_positions.get(stuck_mint, 0)
                        if retries >= 10:
                            # Force-close: write off the position to free portfolio space
                            pos = self.risk.positions.get(stuck_mint)
                            log.error(f"  FORCE CLOSE: {pos.symbol} after {retries} failed sell retries")
                            self.risk.close(stuck_mint, 0, "force_close_stuck")
                            await self._on_position_closed(stuck_mint,
                                                           strategy=pos.strategy, creator=pos.creator)
                            self._stuck_positions.pop(stuck_mint, None)
                            self.notify.send(
                                f"FORCE CLOSED {pos.symbol} — sell failed {retries} times"
                            )
                        else:
                            log.info(f"  Retrying stuck sell ({retries}/10): {stuck_mint[:16]}...")
                            await self._execute_sell(stuck_mint, 100, "stuck_retry")
                            if stuck_mint not in self.risk.positions:
                                self._stuck_positions.pop(stuck_mint, None)

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
                                await self._execute_sell(
                                    rug_signal.mint, rug_signal.sell_pct, rug_signal.reason,
                                )
                        except Exception as e:
                            log.debug(f"  Rug poll error for {mint[:8]}: {e}")

                # RPC wallet polling for copy trade (every ~30s)
                if tick % 10 == 0 and "copy_trade" in self.strategies_enabled:
                    await self._poll_wallet_transactions()

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

                    # Grace period: don't fast-exit early after buy.
                    # Entry slippage + fees cause phantom losses that recover.
                    if pos.age_sec < self.cfg.fast_stop_grace_sec:
                        continue

                    # Strategy-specific stop loss
                    stop = (self.cfg.momentum_stop_loss_pct
                            if pos.strategy == "momentum"
                            else self.cfg.stop_loss_pct)

                    # No-stop window: meme coins swing 15-20% routinely
                    # early on. Only allow catastrophic override during this
                    # window to avoid killing correct signals early.
                    in_nostop_window = pos.age_sec < self.cfg.nostop_window_sec
                    if in_nostop_window:
                        stop = stop * self.cfg.nostop_multiplier

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

                    # Only act on positions in the danger zone
                    if pnl_pct > danger_threshold:
                        continue

                    # Curve-aware: don't fast-exit if curve is rising toward graduation
                    if (pos.curve_pct > 60 and pos.curve_rising
                            and not pos.graduated
                            and (time.time() - pos.curve_updated_at) < 30
                            and pnl_pct > -60):
                        continue

                    # Post-graduation grace: don't fast-exit during Raydium migration
                    if pos.in_graduation_grace and pnl_pct > -80:
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
                            await self._execute_sell(mint, 100, "fast_stop_loss")
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

        # Feed wallet discovery: observe token creators as potential candidates
        if hasattr(self, 'wallet_discovery'):
            creator = event.get("traderPublicKey", "")
            # Estimate SOL spent from vSolInBondingCurve (starts at ~30 virtual SOL)
            vsol = float(event.get("vSolInBondingCurve", 0) or 0)
            sol_spent = max(vsol - 30.0, 0)
            if creator and sol_spent > 0.001:
                discovery_event = {
                    "traderPublicKey": creator,
                    "txType": "buy",
                    "solAmount": sol_spent,
                    "mint": mint,
                }
                try:
                    self.wallet_discovery.observe_trade(discovery_event)
                except Exception:
                    pass

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
        if self.cfg.sniper_buy_delay > 0:
            await asyncio.sleep(self.cfg.sniper_buy_delay)
        await self._execute_buy(signal, auto_sell_sec=self.cfg.sniper_auto_sell_sec)

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
                await self._execute_sell(
                    sell_signal.mint, sell_signal.sell_pct,
                    sell_signal.reason,
                )
            return

        # ── Copy BUYS ─────────────────────────────────────────
        signal = self.copy_trade.evaluate_trade(event)
        if not signal:
            return
        log.info(f"  COPY: {signal.symbol} ({signal.confidence:.0%}) — {signal.reason}")
        if self.cfg.copy_delay_sec > 0:
            await asyncio.sleep(self.cfg.copy_delay_sec)
        await self._execute_buy(signal)

    async def _on_token_trade(self, event: dict):
        # Buyer uniqueness tracking for Sybil detection
        trade_mint = event.get("mint", "")
        trade_type = event.get("txType", "")
        trader_pk = event.get("traderPublicKey", "")
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
                    await self._execute_sell(
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
                        action, sell_pct = self.risk.check_exits(mint, implied_price_usd)
                        if action != "none" and sell_pct > 0:
                            # WS prices are noisy (single-trade implied) —
                            # block stop-losses unless explicitly enabled
                            if "stop" not in action or self.cfg.ws_stop_loss_enabled:
                                log.warning(f"  WS-EXIT: {self.risk.positions[mint].symbol} — {action}")
                                await self._execute_sell(mint, sell_pct, f"ws_{action}")
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
        top_candidates = self.mint_pool.get_best_candidates(count=20, min_age=90)

        if top_candidates:
            # ── Stage 2: On-chain activity filter (individual RPC calls) ───
            active_mints = []
            for mint in top_candidates[:10]:
                try:
                    tx_count = await self.onchain.get_recent_tx_count(mint, seconds=300)
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
            await self._execute_buy(signal)

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
            # A 5-min token at 60% is fine (12%/m), but 10-min at 120% is peaked
            max_growth_for_age = age_min * 12
            if growth_pct > max_growth_for_age and age_min > 3:
                skipped_reasons["peaked"] = skipped_reasons.get("peaked", 0) + 1
                log.info(f"  AGE-CAP: {cand.symbol} | growth={growth_pct:.0f}% > "
                         f"cap={max_growth_for_age:.0f}% (age={age_min:.1f}m × 12) | "
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
            if growth_pct >= 100:
                # Hard reject: already 2x'd, probability of catching another
                # 2x vs catching a dump is unfavorable
                skipped_reasons["peaked"] = skipped_reasons.get("peaked", 0) + 1
                log.info(f"  PEAKED: {cand.symbol} | growth={growth_pct:.0f}% | "
                         f"SOL={state.real_sol:.1f} | curve={state.pct:.0f}%")
                continue
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
            tx_rate = tx_count / max(age_min, 1)
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
                # Two-point curve measurement: confirm growth is sustained
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
                # Score creator positively for passing pipeline
                if hasattr(self, 'wallet_discovery'):
                    self.wallet_discovery.score_pipeline_outcome(
                        cand.creator, mint, True, confidence)
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
                # Score creator negatively for failing pipeline
                if hasattr(self, 'wallet_discovery'):
                    self.wallet_discovery.score_pipeline_outcome(
                        cand.creator, mint, False, confidence)

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

    async def _poll_wallet_transactions(self):
        """Poll manual copy-trade wallets via RPC to catch non-pump.fun trades.

        PumpPortal WebSocket only reports bonding curve trades. This poller uses
        getSignaturesForAddress + getTransaction to detect trades on Raydium,
        Jupiter, and other DEXes that the WebSocket misses.
        """
        if not hasattr(self, 'copy_trade'):
            return

        # Only poll manual wallets (3-5 wallets, not all candidates)
        wallets = list(self.copy_trade.wallets)[:5]

        for wallet in wallets:
            try:
                # Get recent transaction signatures
                result = await self.onchain._rpc_call("getSignaturesForAddress", [
                    wallet, {"limit": 5, "commitment": "confirmed"}
                ])
                if not result:
                    continue

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

                for sig in new_sigs[:3]:  # max 3 new txs per wallet per cycle
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

                # Cap seen signatures to prevent unbounded growth
                if len(seen) > 100:
                    self._wallet_seen_sigs[wallet] = set(list(seen)[-50:])

            except Exception as e:
                log.debug(f"  Wallet poll error for {wallet[:8]}: {e}")

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
                self.graduation.watching[mint] = curve_pct
                self.graduation._watching_times[mint] = time.time()

                # Detect graduation (100% curve = bonding curve complete)
                if curve_pct >= 100 and mint not in self.graduation._graduated_at:
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

                # Prefer post-dump entries: tokens with negative/low 5m%
                # have already shed FOMO buyers and found a floor.
                # Skip high positive 5m — likely buying the top.
                change_5m = token_data.price_change_5m
                if change_5m > 80:
                    log.info(f"  POST-GRAD: {symbol} — FOMO top ({change_5m:+.0f}% 5m), skipping")
                    self.graduation.mark_bought(mint)
                    continue
                if change_5m < -50:
                    log.info(f"  POST-GRAD: {symbol} — too deep ({change_5m:+.0f}% 5m), skipping")
                    self.graduation.mark_bought(mint)
                    continue

                # Confidence: boost for negative 5m (post-dump floor),
                # penalize for high positive (chasing)
                if change_5m <= -5:
                    conf = 0.80
                elif change_5m <= 10:
                    conf = 0.75
                else:
                    conf = 0.65

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
                await self._execute_buy(signal)

            except Exception as e:
                log.debug(f"  Post-grad buy error for {symbol}: {e}")

    # ══════════════════════════════════════════════════════════
    # POSITION MONITORING
    # ══════════════════════════════════════════════════════════

    async def _check_positions(self):
        to_exit = []

        for mint, pos in list(self.risk.positions.items()):
            # Update curve % for held positions (non-graduated, not stale)
            if not pos.graduated and pos.age_sec > 5:
                await self._update_position_curve(mint, pos)

            token = await self.market.get_token(mint)

            if not token:
                price_usd = await self._get_onchain_price_usd(mint)
                if price_usd and price_usd > 0:
                    action, sell_pct = self.risk.check_exits(mint, price_usd)
                    if action != "none":
                        to_exit.append((mint, action, sell_pct, None))
                    continue

                # No price source at all — time-based exits only
                if pos.auto_sell_at > 0 and time.time() >= pos.auto_sell_at:
                    to_exit.append((mint, "auto_sell", 100, None))
                elif self.cfg.max_hold_minutes > 0 and pos.age_min >= self.cfg.max_hold_minutes:
                    to_exit.append((mint, "time_exit", 100, None))
                continue

            action, sell_pct = self.risk.check_exits(mint, token.price_usd)
            if action != "none":
                to_exit.append((mint, action, sell_pct, token))

        for mint, reason, sell_pct, token in to_exit:
            await self._execute_sell(mint, sell_pct, reason)

            # Post-graduation liquidity check: after TP2 graduated partial,
            # verify DEX liquidity supports holding the remainder
            if reason == "take_profit_graduated" and mint in self.risk.positions:
                await self._check_post_grad_liquidity(mint)

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
                    pos.graduated = True
                    pos.graduated_at = time.time()
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
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
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

    async def _execute_buy(self, signal: Signal, auto_sell_sec: int = 0):
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
        async with lock:
            await self._execute_buy_inner(signal, auto_sell_sec)

    async def _execute_buy_inner(self, signal: Signal, auto_sell_sec: int = 0):
        mint = signal.mint
        if mint in self.risk.positions:
            return

        # Hard no-re-entry: never buy a token we already traded this session
        if mint in self._closed_mints:
            log.debug(f"  No re-entry: {signal.symbol} already traded")
            return

        # Per-mint re-entry cooldown
        cooldown_expiry = self._mint_cooldowns.get(mint, 0)
        if cooldown_expiry > time.time():
            remaining = (cooldown_expiry - time.time()) / 60
            log.info(f"  Cooldown: {signal.symbol} blocked for {remaining:.0f}m more")
            return

        # Dynamic position sizing (wallet score affects copy-trade sizing)
        sol_bal = self.wallet.get_sol_balance()
        size = self.risk.calc_size(sol_bal, signal.confidence, signal.strategy,
                                   wallet=signal.creator)
        if size < 0.005:
            return

        # Risk check (includes cooldown)
        can, reason = self.risk.can_open(size, signal.strategy)
        if not can:
            # Exempt high-confidence signals from cooldown
            if ("cooldown" in reason.lower()
                    and signal.confidence >= self.cfg.cooldown_exempt_confidence):
                log.info(f"  Cooldown bypassed: {signal.symbol} confidence "
                         f"{signal.confidence:.0%} >= {self.cfg.cooldown_exempt_confidence:.0%}")
            else:
                log.info(f"  Risk block: {reason}")
                return

        # Safety check (DexScreener + on-chain)
        if signal.token_data:
            try:
                safety = await self.safety.check(
                    signal.token_data,
                    is_snipe=(signal.strategy == "sniper"),
                    creator=signal.creator,
                )
                if not safety.passed:
                    log.warning(f"  Safety FAIL: {safety.warnings[:3]}")
                    self.db.blacklist_token(mint, "; ".join(safety.warnings[:3]))
                    return
            except Exception as e:
                log.warning(f"  Safety check exception — skipping trade: {e}")
                return
        else:
            # No token_data — run on-chain safety (freeze auth + holders + creator)
            try:
                oc = await self.onchain.analyze(mint, signal.creator)
                if not oc.freeze_authority_revoked:
                    log.warning(f"  Safety FAIL: freeze authority active — {signal.symbol}")
                    self.db.blacklist_token(mint, "freeze authority active")
                    return
                # Young pump.fun tokens have concentrated holders — use relaxed
                # thresholds: 70% for sniper, 50% for pipeline, 30% for mature tokens
                if signal.strategy == "sniper":
                    max_holder = self.cfg.sniper_max_top_holder_pct
                else:
                    max_holder = self.cfg.pipeline_max_top_holder_pct
                if oc.top_holder_pct > max_holder:
                    log.warning(f"  Safety FAIL: top holder {oc.top_holder_pct:.0f}% — {signal.symbol}")
                    self.db.blacklist_token(mint, f"top holder {oc.top_holder_pct:.0f}%")
                    return
                # Hard block serial deployers
                if signal.creator and oc.creator_token_count >= self.cfg.max_creator_tx_count:
                    log.warning(f"  Safety FAIL: creator {signal.creator[:8]} has "
                                f"{oc.creator_token_count}+ txs — {signal.symbol}")
                    self.db.blacklist_creator(signal.creator,
                                              f"serial deployer ({oc.creator_token_count} txs)")
                    return
                # Soft log for moderately active creators
                if signal.creator and oc.creator_token_count >= 50:
                    log.info(f"  Creator active ({oc.creator_token_count} recent txs) — {signal.symbol}")
                # Minimum holder count
                if oc.holder_count > 0 and oc.holder_count < self.cfg.min_holder_count:
                    log.warning(f"  Safety FAIL: only {oc.holder_count} holders — {signal.symbol}")
                    return
            except Exception as e:
                if signal.strategy == "sniper":
                    log.warning(f"  Safety FAIL: on-chain check failed for sniper — {signal.symbol}: {e}")
                    return  # HARD BLOCK — don't snipe if we can't verify
                log.debug(f"  On-chain safety check error: {e}")

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

            tokens = (size / price_sol) if price_sol > 0 else 0
            if tokens <= 0:
                return

            ok = self.wallet.buy(mint, signal.symbol, size, tokens, price_usd)
            if ok:
                self.risk.open(
                    mint, signal.symbol, price_usd, price_sol,
                    tokens, size, signal.strategy, auto_sell_sec,
                    creator=signal.creator,
                )
                # Mark post-graduation buys as graduated immediately
                if signal.strategy == "graduation" and mint in self.risk.positions:
                    self.risk.positions[mint].graduated = True
                    self.risk.positions[mint].graduated_at = time.time()
                    self._graduated_cache[mint] = True
                self.notify.trade_alert("buy", signal.symbol, size,
                                         reason=signal.reason)
        else:
            log.info(f"  Buying {signal.symbol} for {size:.4f} SOL...")

            # Post-graduation buys go through Jupiter (token is on PumpSwap/Raydium)
            if signal.strategy == "graduation" and self.cfg.use_jupiter:
                tx = await self.jupiter.buy_token(
                    mint, size,
                    slippage_bps=self.cfg.jupiter_slippage_bps,
                    use_jito=self.pump.use_jito,
                )
                if tx:
                    self.db.save_position(
                        mint=mint, symbol=signal.symbol, entry_price=0,
                        entry_sol_price=0, tokens=0, sol_invested=size,
                        entry_time=time.time(), strategy=signal.strategy,
                        tx_signature=tx,
                        extra={"creator": signal.creator, "pending": True,
                               "graduated": True},
                    )
                    await asyncio.sleep(2)
                    total_spent, price_usd, price_sol, tokens, _sol_tok = \
                        await self._resolve_buy_prices(mint, size, tx)
                    self.risk.open(
                        mint, signal.symbol, price_usd, price_sol,
                        tokens, total_spent, signal.strategy, auto_sell_sec,
                        tx_signature=tx, creator=signal.creator,
                    )
                    # Mark as graduated immediately (bought post-graduation)
                    if mint in self.risk.positions:
                        self.risk.positions[mint].graduated = True
                        self.risk.positions[mint].graduated_at = time.time()
                        self._graduated_cache[mint] = True
                    self.notify.trade_alert("buy", signal.symbol, total_spent,
                                             reason=signal.reason, tx=tx)
                else:
                    log.error(f"  Jupiter buy FAILED for {signal.symbol}")

                # Start rug monitoring if position was opened
                if mint in self.risk.positions:
                    await self._on_position_opened(mint, signal.creator)
                return

            slippage = (self.cfg.momentum_slippage_pct
                        if signal.strategy == "momentum"
                        else self.cfg.slippage_pct)
            tx = await self.pump.buy(
                mint, size,
                slippage=slippage,
                priority_fee=self.cfg.priority_fee_sol,
            )
            if tx:
                # Immediately record position to DB to survive crashes.
                # Uses estimated values; updated with actuals below.
                self.db.save_position(
                    mint=mint, symbol=signal.symbol, entry_price=0,
                    entry_sol_price=0, tokens=0, sol_invested=size,
                    entry_time=time.time(), strategy=signal.strategy,
                    tx_signature=tx,
                    extra={"creator": signal.creator, "pending": True},
                )
                await asyncio.sleep(2)
                total_spent, price_usd, price_sol, tokens, _sol_tok = \
                    await self._resolve_buy_prices(mint, size, tx)
                self.risk.open(
                    mint, signal.symbol, price_usd, price_sol,
                    tokens, total_spent, signal.strategy, auto_sell_sec,
                    tx_signature=tx, creator=signal.creator,
                )
                self.notify.trade_alert("buy", signal.symbol, total_spent,
                                         reason=signal.reason, tx=tx)
            else:
                log.error(f"  Buy FAILED for {signal.symbol} — retrying once...")
                tx = await self.pump.buy(
                    mint, size,
                    slippage=self.cfg.slippage_pct,
                    priority_fee=self.cfg.priority_fee_sol * 2,
                )
                if tx:
                    self.db.save_position(
                        mint=mint, symbol=signal.symbol, entry_price=0,
                        entry_sol_price=0, tokens=0, sol_invested=size,
                        entry_time=time.time(), strategy=signal.strategy,
                        tx_signature=tx,
                        extra={"creator": signal.creator, "pending": True},
                    )
                    await asyncio.sleep(2)
                    total_spent, price_usd, price_sol, tokens, _sol_tok = \
                        await self._resolve_buy_prices(mint, size, tx)
                    self.risk.open(
                        mint, signal.symbol, price_usd, price_sol,
                        tokens, total_spent, signal.strategy, auto_sell_sec,
                        tx_signature=tx, creator=signal.creator,
                    )
                    self.notify.trade_alert("buy", signal.symbol, total_spent,
                                             reason=signal.reason, tx=tx)
                    return

                # Check if we actually bought (tx confirmed but response lost)
                await asyncio.sleep(3)
                actual_tokens = self.wallet.get_token_balance(mint)
                if actual_tokens > 0 and mint not in self.risk.positions:
                    log.warning(f"  Surprise! We own {actual_tokens} tokens of "
                                f"{signal.symbol} — recording phantom buy")
                    token_data = await self.market.get_token(mint)
                    price_usd = token_data.price_usd if token_data else 0
                    price_sol = token_data.price_sol if token_data else 0
                    self.risk.open(
                        mint, signal.symbol, price_usd, price_sol,
                        actual_tokens, size, signal.strategy, auto_sell_sec,
                        tx_signature="phantom", creator=signal.creator,
                    )

        # Start rug monitoring if position was opened
        if mint in self.risk.positions:
            await self._on_position_opened(mint, signal.creator)

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
            to_remove = []
            for mint, pos in positions:
                try:
                    balance = self.wallet.get_token_balance(mint)
                    if balance <= 0:
                        log.warning(f"  WS audit: {pos.symbol} has 0 balance — removing")
                        to_remove.append(mint)
                    elif abs(balance - pos.tokens) / max(pos.tokens, 1) > 0.5:
                        log.warning(f"  WS audit: {pos.symbol} balance mismatch "
                                    f"(expected {pos.tokens:.0f}, got {balance:.0f}) — updating")
                        pos.tokens = balance
                        self.db.update_position(mint, tokens=balance)
                except Exception as e:
                    log.debug(f"  WS audit balance check error for {mint[:8]}: {e}")

            for mint in to_remove:
                self.risk.close(mint, 0, "ws_audit_zero_balance")
                await self._on_position_closed(mint)

        # Re-subscribe tokens for rug detection
        if hasattr(self, 'rug_detector'):
            for mint, pos in self.risk.positions.items():
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
        # Permanent no-re-entry — never buy this token again this session
        self._closed_mints.add(mint)

        # Record cooldown for losing trades to prevent re-entry
        if pnl is not None and pnl < 0 and self.cfg.mint_cooldown_minutes > 0:
            self._mint_cooldowns[mint] = time.time() + self.cfg.mint_cooldown_minutes * 60
            log.info(f"  Cooldown: {mint[:12]}... blocked for {self.cfg.mint_cooldown_minutes}m")

        # Auto-blacklist creator on loss
        if pnl is not None and pnl < 0:
            if creator and hasattr(self, 'sniper'):
                self.sniper.blacklist_creator(creator)
            # Persist blacklist for significant losses (likely rugs)
            if creator and pnl < -0.005:
                self.db.blacklist_creator(creator, f"loss {pnl:.4f} SOL ({strategy})")
                log.info(f"  Creator {creator[:8]}... blacklisted after {strategy} loss")

        # Score creator wallet for ALL strategies (feeds wallet discovery)
        if pnl is not None and creator:
            try:
                self.db.update_wallet_score(creator, pnl, 0, mint, pnl >= 0)
            except Exception:
                pass

        if hasattr(self, 'rug_detector'):
            self.rug_detector.stop_monitoring(mint)
            await self.stream.unsubscribe_token(mint)
        self._graduated_cache.pop(mint, None)

    async def _resolve_buy_prices(self, mint: str, estimated_sol: float,
                                   tx_sig: str = ""
                                   ) -> tuple[float, float, float, float, float]:
        """Resolve actual SOL spent, prices, and token balance after a buy tx.

        Returns (total_sol_spent, price_usd, price_sol, tokens, sol_for_tokens).
        total_sol_spent = full wallet impact (for sol_invested / PnL tracking).
        sol_for_tokens  = SOL that actually bought tokens (for entry price).
        """
        total_sol_spent = estimated_sol
        sol_for_tokens = estimated_sol
        price_usd = 0
        price_sol = 0
        tx_result = await self.pump.get_tx_result(tx_sig) if tx_sig else None
        if tx_result:
            try:
                meta = tx_result.get("meta", {})
                pre = meta.get("preBalances", [])
                post = meta.get("postBalances", [])
                if pre and post and len(pre) > 0 and len(post) > 0:
                    sol_diff = (pre[0] - post[0]) / 1e9
                    if sol_diff > 0:
                        total_sol_spent = sol_diff
                        # Subtract known fees so entry price reflects
                        # only the SOL that bought tokens, not tx overhead.
                        tx_fee = meta.get("fee", 0) / 1e9  # base + priority
                        fee_deduction = tx_fee + self.cfg.jito_tip_sol
                        sol_for_tokens = sol_diff - fee_deduction
                        if sol_for_tokens <= 0:
                            sol_for_tokens = sol_diff  # fallback
                        log.info(f"  Entry: {sol_diff:.6f} total, "
                                 f"{fee_deduction:.6f} fees, "
                                 f"{sol_for_tokens:.6f} for tokens")
            except Exception as e:
                log.debug(f"  Could not parse tx result: {e}")

        tokens = self.wallet.get_token_balance(mint)
        if tokens > 0 and sol_for_tokens > 0:
            price_sol = sol_for_tokens / tokens
            sol_price = await self.market.get_sol_price()
            price_usd = price_sol * sol_price if sol_price > 0 else 0

        if price_usd <= 0 or price_sol <= 0:
            token_data = await self.market.get_token(mint)
            price_usd = token_data.price_usd if token_data else 0
            price_sol = token_data.price_sol if token_data else 0

        return total_sol_spent, price_usd, price_sol, tokens, sol_for_tokens

    def _get_mint_lock(self, mint: str) -> asyncio.Lock:
        if mint not in self._mint_locks:
            self._mint_locks[mint] = asyncio.Lock()
        return self._mint_locks[mint]

    async def _execute_sell(self, mint: str, sell_pct: int, reason: str):
        lock = self._get_mint_lock(mint)
        async with lock:
            await self._execute_sell_inner(mint, sell_pct, reason)
        # Clean up lock if position is closed (sell_pct == 100)
        if sell_pct >= 100:
            self._mint_locks.pop(mint, None)

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
                sol_received = sol_received_raw * (1 - slippage / 100)
                log.info(f"  Paper slippage: {slippage:.1f}% applied "
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
            # For emergency exits, use cached graduation flag (skip RPC call)
            # For normal exits, do the full check
            is_emergency = ("stop" in reason or "rug" in reason.lower()
                            or "fast" in reason or "ws_" in reason)
            if is_emergency:
                graduated = pos.graduated  # use cached flag, no RPC
            else:
                graduated = await self._is_graduated(mint, pos)

            if graduated and self.cfg.use_jupiter:
                await self._sell_via_jupiter(mint, pos, sell_pct, is_partial, reason)
            else:
                await self._sell_via_pumpportal(mint, pos, sell_pct, is_partial, reason)

    async def _get_sol_received(self, sol_before: float, pos,
                                retries: int = 3) -> float:
        """Get SOL received after a sell tx, retrying if balance unchanged.

        RPC may not reflect the new balance immediately after tx confirmation.
        Retries with short delays to avoid recording 0 SOL on valid sells.
        """
        for attempt in range(retries):
            sol_after = self.wallet.get_sol_balance()
            sol_received = max(sol_after - sol_before, 0)
            if sol_received > 0:
                return sol_received
            if attempt < retries - 1:
                wait = 2 * (attempt + 1)  # 2s, 4s
                log.info(f"  Balance unchanged after sell of {pos.symbol}, "
                         f"retrying in {wait}s ({attempt + 1}/{retries})")
                await asyncio.sleep(wait)
        return 0.0

    async def _sell_via_jupiter(self, mint: str, pos, sell_pct: int,
                                is_partial: bool, reason: str):
        """Sell graduated tokens through Jupiter V6."""
        log.info(f"  Selling {pos.symbol} via Jupiter (graduated)")
        token_balance = self.wallet.get_token_balance(mint)
        if token_balance <= 0:
            log.warning(f"  No token balance for Jupiter sell: {pos.symbol}")
            # Fall back to PumpPortal
            await self._sell_via_pumpportal(mint, pos, sell_pct, is_partial, reason)
            return

        sell_amount = token_balance * (sell_pct / 100)
        decimals = await self.onchain.get_token_decimals(mint)
        raw_amount = int(sell_amount * (10 ** decimals))

        if raw_amount <= 0:
            await self._sell_via_pumpportal(mint, pos, sell_pct, is_partial, reason)
            return

        sol_before = self.wallet.get_sol_balance()
        tx = await self.jupiter.sell_token(
            mint, raw_amount,
            slippage_bps=self.cfg.jupiter_slippage_bps,
            use_jito=self.pump.use_jito,
        )
        if tx:
            sell_pnl = None
            sol_received = await self._get_sol_received(sol_before, pos)
            if is_partial:
                self.risk.partial_close(mint, sol_received=sol_received,
                                        reason=f"{reason} (jupiter)")
            else:
                sell_pnl = self.risk.close(mint, sol_received, reason, tx)
                await self._on_position_closed(mint, pnl=sell_pnl,
                                               strategy=pos.strategy, creator=pos.creator)
            self.notify.trade_alert("sell", pos.symbol, pos.sol_invested,
                                     pnl=sell_pnl, reason=f"{reason} (jupiter)",
                                     tx=tx, balance=self.wallet.sol_balance,
                                     daily_pnl=self.risk.daily_pnl)
        else:
            log.warning(f"  Jupiter sell failed for {pos.symbol}, falling back to PumpPortal")
            await self._sell_via_pumpportal(mint, pos, sell_pct, is_partial, reason)

    async def _sell_via_pumpportal(self, mint: str, pos, sell_pct: int,
                                   is_partial: bool, reason: str):
        """Sell tokens through PumpPortal (bonding curve or fallback)."""
        is_emergency = ("stop" in reason or "rug" in reason.lower()
                        or "fast" in reason or "ws_" in reason)
        sell_slippage = 40 if is_emergency else self.cfg.slippage_pct
        sell_fee = min(self.cfg.priority_fee_sol * 2, 0.01) if is_emergency else self.cfg.priority_fee_sol
        sol_before = self.wallet.get_sol_balance()
        tx = await self.pump.sell(
            mint, sell_pct=sell_pct,
            slippage=sell_slippage,
            priority_fee=sell_fee,
            skip_jito=is_emergency,
        )
        if tx:
            sell_pnl = None
            sol_received = await self._get_sol_received(sol_before, pos)
            if is_partial:
                self.risk.partial_close(mint, sol_received=sol_received,
                                        reason=reason)
            else:
                sell_pnl = self.risk.close(mint, sol_received, reason, tx)
                await self._on_position_closed(mint, pnl=sell_pnl,
                                               strategy=pos.strategy, creator=pos.creator)

            self.notify.trade_alert("sell", pos.symbol, pos.sol_invested,
                                     pnl=sell_pnl, reason=reason, tx=tx,
                                     balance=self.wallet.sol_balance,
                                     daily_pnl=self.risk.daily_pnl)
        else:
            log.error(f"  Sell FAILED for {pos.symbol} — CRITICAL!")
            # Check if token graduated during our hold — PumpPortal can't sell graduated tokens
            if self.cfg.use_jupiter and not pos.graduated:
                graduated = await self._is_graduated(mint, pos)
                if graduated:
                    log.info(f"  {pos.symbol} graduated — retrying via Jupiter")
                    await self._sell_via_jupiter(mint, pos, sell_pct, is_partial, reason)
                    return
            elif pos.graduated and self.cfg.use_jupiter:
                log.info(f"  {pos.symbol} already graduated — retrying via Jupiter")
                await self._sell_via_jupiter(mint, pos, sell_pct, is_partial, reason)
                return

            # Last-resort retry: max slippage, skip Jito, higher fee
            retry_fee = min(self.cfg.priority_fee_sol * 2,
                            pos.sol_invested * 0.15)
            sol_before2 = self.wallet.get_sol_balance()
            tx2 = await self.pump.sell(
                mint, sell_pct=sell_pct,
                slippage=50,
                priority_fee=retry_fee,
                skip_jito=True,
            )
            if tx2:
                sol_received2 = await self._get_sol_received(sol_before2, pos)
                sell_pnl2 = None
                if not is_partial:
                    sell_pnl2 = self.risk.close(mint, sol_received2, reason, tx2)
                    await self._on_position_closed(mint, pnl=sell_pnl2,
                                                   strategy=pos.strategy, creator=pos.creator)
                log.info(f"  Sell recovered with max slippage: {tx2}")
                self.notify.trade_alert("sell", pos.symbol, sol_received2,
                                         pnl=sell_pnl2, reason=f"{reason} (retry)",
                                         tx=tx2, balance=self.wallet.sol_balance,
                                         daily_pnl=self.risk.daily_pnl)
            else:
                log.error(f"  SELL STUCK: {pos.symbol} — will retry next cycle")
                if not hasattr(self, '_stuck_positions'):
                    self._stuck_positions = {}
                self._stuck_positions[mint] = self._stuck_positions.get(mint, 0) + 1
                self.notify.send(
                    f"ALERT: Cannot sell {pos.symbol} ({mint[:16]}...) — "
                    f"will retry next poll cycle"
                )

    async def _is_graduated(self, mint: str, pos) -> bool:
        """Check if a token has graduated, with caching."""
        if pos.graduated:
            return True
        if mint in self._graduated_cache:
            return self._graduated_cache[mint]
        try:
            graduated = await self.onchain.is_graduated(mint)
            self._graduated_cache[mint] = graduated
            if graduated:
                pos.graduated = True
                if not pos.graduated_at:
                    pos.graduated_at = time.time()
                log.info(f"  {pos.symbol} has graduated from pump.fun")
            return graduated
        except Exception as e:
            log.debug(f"  Graduation check error: {e}")
            return False

    # ══════════════════════════════════════════════════════════
    # TELEGRAM COMMANDS
    # ══════════════════════════════════════════════════════════

    async def _poll_telegram_commands(self):
        """Poll Telegram events and route to TelegramUI."""
        events = self.notify.poll_events()
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
        sol = self.wallet.get_sol_balance()

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
        for mint, pos in self.risk.positions.items():
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
