"""Trading strategies — sniper, copy trading (with sell mirroring), graduation.

Momentum lives in bot.py (the on-chain discovery pipeline)."""

import time
import logging
from dataclasses import dataclass
from market_data import MarketData, TokenData
from db import Database
from onchain import OnChainAnalyzer

log = logging.getLogger(__name__)


@dataclass
class Signal:
    mint: str
    symbol: str
    strategy: str
    confidence: float
    reason: str
    suggested_sol: float = 0
    token_data: TokenData = None
    raw_event: dict = None
    creator: str = ""


@dataclass
class SellSignal:
    """Signal to exit a position (from copy-sell or other sources)."""
    mint: str
    symbol: str
    reason: str
    sell_pct: int = 100
    source_wallet: str = ""


class SniperStrategy:
    """Snipe new tokens on Pump.fun within seconds of creation."""

    def __init__(self, min_sol_liq: float = 3.0, buy_delay: float = 2.0,
                 db: Database = None):
        self.min_sol_liq = min_sol_liq
        self.buy_delay = buy_delay
        self.sniped: set[str] = set()
        self.blacklist_creators: set[str] = set()
        self.db = db

    def evaluate_new_token(self, event: dict) -> Signal | None:
        mint = event.get("mint", "")
        if not mint or mint in self.sniped:
            return None

        # Mark as sniped immediately to prevent duplicate evaluation
        self.sniped.add(mint)

        # Check blacklist in DB
        if self.db and self.db.is_blacklisted(mint):
            return None

        creator = event.get("traderPublicKey", "")
        if creator in self.blacklist_creators:
            return None

        symbol = event.get("symbol", "???")
        name = event.get("name", "")
        vsol = float(event.get("vSolInBondingCurve", 0) or 0)
        initial_buy = float(event.get("initialBuy", 0) or 0)
        mcap_sol = float(event.get("marketCapSol", 0) or 0)

        # Convert lamports to SOL for dev buy comparison
        initial_buy_sol = initial_buy / 1e9

        # Require meaningful dev buy — tiny/no buys are rug deployers
        if initial_buy_sol < 0.5:
            return None

        score = 0.0
        reasons = []

        if initial_buy_sol >= 2.0:
            score += 0.3
            reasons.append(f"Big dev buy: {initial_buy_sol:.2f} SOL")
        elif initial_buy_sol >= 0.5:
            score += 0.2
            reasons.append(f"Dev buy: {initial_buy_sol:.2f} SOL")

        if name and len(name) > 2:
            score += 0.1
            reasons.append(f"Named: {name}")

        uri = event.get("uri", "")
        if uri and "ipfs" in uri:
            score += 0.1
            reasons.append("Has metadata")

        if 5 < mcap_sol < 100:
            score += 0.2
            reasons.append(f"MCap: {mcap_sol:.1f} SOL")
        elif mcap_sol > 0:
            score += 0.05

        if vsol >= self.min_sol_liq:
            score += 0.15
            reasons.append(f"Liq: {vsol:.1f} SOL")
        else:
            return None

        confidence = min(score, 1.0)
        if confidence >= 0.45:
            # Cap sniped set to prevent unbounded growth
            if len(self.sniped) > 10_000:
                to_keep = set(list(self.sniped)[-5000:])
                self.sniped = to_keep
                self.sniped.add(mint)
            return Signal(
                mint=mint, symbol=symbol, strategy="sniper",
                confidence=confidence,
                reason="NEW TOKEN | " + " | ".join(reasons),
                raw_event=event, creator=creator,
            )
        return None

    def blacklist_creator(self, creator: str):
        """Called when a sniper position rugs — prevents sniping future tokens from this creator."""
        if creator:
            self.blacklist_creators.add(creator)


class CopyTradeStrategy:
    """
    Copy trades from smart money wallets.
    NEW: Sell mirroring — exit when smart money exits.
    NEW: Wallet scoring — weight by wallet performance.
    """

    def __init__(self, watch_wallets: list[str], db: Database = None):
        self.wallets = set(watch_wallets)
        self.copied: set[str] = set()
        self.db = db
        # Track which wallet bought which token (for sell mirroring)
        self.wallet_positions: dict[str, set[str]] = {}  # wallet -> {mints}
        self._wallet_position_times: dict[str, dict[str, float]] = {}  # wallet -> {mint -> time}
        # Suspended wallets (auto-suspended after consecutive losses)
        self._suspended_wallets: dict[str, float] = {}  # wallet -> resume_time

    def evaluate_trade(self, event: dict) -> Signal | None:
        """Evaluate a buy from a watched wallet."""
        trader = event.get("traderPublicKey", "")
        if trader not in self.wallets:
            return None

        tx_type = event.get("txType", "")
        if tx_type != "buy":
            return None

        # Check wallet suspension
        if trader in self._suspended_wallets:
            if time.time() < self._suspended_wallets[trader]:
                return None  # Still suspended
            else:
                del self._suspended_wallets[trader]  # Suspension expired

        mint = event.get("mint", "")
        key = f"{mint}:{trader}"
        if key in self.copied:
            return None

        sol_amount = float(event.get("solAmount", 0) or 0)
        symbol = event.get("symbol", mint[:8])
        mcap_sol = float(event.get("marketCapSol", 0) or 0)

        if sol_amount < 0.05:
            return None

        # Wallet scoring: adjust confidence by wallet performance
        base_confidence = 0.5
        if self.db:
            wallet_score = self.db.get_wallet_score(trader)
            if wallet_score:
                # Score 0-100, map to 0.3-0.9 confidence
                base_confidence = 0.3 + (wallet_score["score"] / 100) * 0.6
                if wallet_score["total_trades"] >= 10 and wallet_score["score"] < 30:
                    log.info(f"  Skipping low-score wallet {trader[:8]} "
                             f"(score: {wallet_score['score']:.0f})")
                    return None
                # Auto-suspend after 5 consecutive losses
                if wallet_score["streak"] <= -5:
                    suspend_min = 60  # 1 hour
                    self._suspended_wallets[trader] = time.time() + suspend_min * 60
                    log.warning(f"  Wallet {trader[:8]}... suspended for {suspend_min}m "
                                f"({wallet_score['streak']} consecutive losses)")
                    return None

        score = base_confidence
        reasons = [f"Smart money {trader[:8]}... bought"]

        if sol_amount >= 1.0:
            score += 0.2; reasons.append(f"Big buy: {sol_amount:.2f} SOL")
        elif sol_amount >= 0.2:
            score += 0.1; reasons.append(f"Buy: {sol_amount:.2f} SOL")

        if 0 < mcap_sol < 200:
            score += 0.1; reasons.append(f"Early MCap: {mcap_sol:.0f} SOL")

        confidence = min(score, 1.0)
        self.copied.add(key)

        # Track wallet->mint for sell mirroring
        if trader not in self.wallet_positions:
            self.wallet_positions[trader] = set()
            self._wallet_position_times[trader] = {}
        self.wallet_positions[trader].add(mint)
        self._wallet_position_times.setdefault(trader, {})[mint] = time.time()

        return Signal(mint=mint, symbol=symbol, strategy="copy_trade",
                      confidence=confidence, reason=" | ".join(reasons),
                      raw_event=event, creator=trader)

    def evaluate_sell(self, event: dict, held_mints: set[str]) -> SellSignal | None:
        """
        Check if smart money is selling a token we hold.
        Returns SellSignal if we should exit.
        """
        trader = event.get("traderPublicKey", "")
        if trader not in self.wallets:
            return None

        tx_type = event.get("txType", "")
        if tx_type != "sell":
            return None

        mint = event.get("mint", "")
        if mint not in held_mints:
            return None

        # Check if this wallet was the one we copied for this mint
        if trader in self.wallet_positions and mint in self.wallet_positions[trader]:
            sol_amount = float(event.get("solAmount", 0) or 0)
            new_balance = float(event.get("newTokenBalance", 0) or 0)

            # Determine sell percentage
            if new_balance <= 0:
                sell_pct = 100
                reason = f"Smart money {trader[:8]}... SOLD ALL"
                # Clean up tracking after full sell
                self.wallet_positions[trader].discard(mint)
                if trader in self._wallet_position_times:
                    self._wallet_position_times[trader].pop(mint, None)
            else:
                sell_pct = 50  # partial sell = we sell 50%
                reason = f"Smart money {trader[:8]}... partial sell ({sol_amount:.2f} SOL)"

            log.info(f"  COPY-SELL: {reason}")
            return SellSignal(
                mint=mint, symbol=event.get("symbol", mint[:8]),
                reason=reason, sell_pct=sell_pct, source_wallet=trader,
            )

        return None

    def cleanup_stale_positions(self):
        """Remove wallet_positions entries older than 1 hour and prune copied set."""
        cutoff = time.time() - 3600
        for trader in list(self._wallet_position_times.keys()):
            times = self._wallet_position_times[trader]
            stale = [m for m, t in times.items() if t < cutoff]
            for mint in stale:
                times.pop(mint, None)
                if trader in self.wallet_positions:
                    self.wallet_positions[trader].discard(mint)
            if not times:
                self._wallet_position_times.pop(trader, None)
            if trader in self.wallet_positions and not self.wallet_positions[trader]:
                self.wallet_positions.pop(trader, None)
        # Prune copied set to prevent unbounded growth
        if len(self.copied) > 5000:
            self.copied = set(list(self.copied)[-2500:])

    def update_wallet_score(self, wallet: str, mint: str, pnl_sol: float,
                            hold_sec: float, won: bool):
        """Update wallet performance tracking after a trade closes."""
        if self.db:
            self.db.update_wallet_score(wallet, pnl_sol, hold_sec, mint, won)


class GraduationStrategy:
    """
    Post-graduation entry: monitor tokens approaching graduation, then buy
    AFTER they graduate and migrate to PumpSwap/Raydium via Jupiter.

    Flow:
    1. Track tokens at 75%+ curve (watching dict)
    2. Detect graduation (curve_pct >= 100 or bonding curve complete)
    3. Wait 60-120s for PumpSwap pool to establish
    4. Buy via Jupiter with verified price/volume
    """

    def __init__(self, market: MarketData, min_curve_pct: float = 85,
                 buy_at_pct: float = 95, onchain: OnChainAnalyzer = None,
                 db: Database = None):
        self.market = market
        self.min_curve = min_curve_pct
        self.buy_at = buy_at_pct
        self.onchain = onchain
        self.db = db
        self.watching: dict[str, float] = {}  # mint -> curve_pct
        self._watching_times: dict[str, float] = {}  # mint -> last_seen_time
        self.bought: set[str] = set()
        # Mints we have read on-chain while still below 100%
        self._seen_below: set[str] = set()
        # Post-graduation tracking: mint -> graduation detected time
        self._graduated_at: dict[str, float] = {}
        # Tokens ready for post-graduation buy (passed wait period)
        self._ready_to_buy: dict[str, dict] = {}  # mint -> event data

    # pump.fun bonding curve: ~30 virtual SOL at launch, graduates at ~85 real SOL
    VIRTUAL_SOL_AT_LAUNCH = 30.0
    GRADUATION_SOL = 85.0
    MAX_TRACKED = 2000

    def note_curve(self, mint: str, curve_pct: float):
        """Record an ON-CHAIN curve reading for a token.

        A graduation is only believed for a token we have ourselves seen
        still on the curve. Without this, any event for a token that
        graduated months ago reads as "just graduated".
        """
        if 0 < curve_pct < 100:
            if len(self._seen_below) >= self.MAX_TRACKED:
                self._seen_below.clear()
            self._seen_below.add(mint)

    def _estimate_pct(self, event: dict) -> float | None:
        """Rough curve % from an event's virtual SOL, or None if absent.
        Only ever used to SKIP an RPC call, never to declare anything."""
        vsol = float(event.get("vSolInBondingCurve", 0) or 0)
        if vsol <= 0:
            return None
        real = max(vsol - self.VIRTUAL_SOL_AT_LAUNCH, 0)
        return min(real / self.GRADUATION_SOL * 100, 100)

    async def evaluate_trade(self, event: dict) -> Signal | None:
        """Track tokens approaching graduation. Does NOT emit buy signals directly."""
        mint = event.get("mint", "")
        if not mint or mint in self.bought:
            return None
        if self.db and self.db.is_blacklisted(mint):
            return None
        if not self.onchain:
            return None

        # Most events are for tokens nowhere near graduation: don't spend an
        # RPC call on those.
        estimate = self._estimate_pct(event)
        if (estimate is not None and estimate < self.min_curve - 20
                and mint not in self.watching):
            return None

        try:
            curve_pct = await self.onchain.get_bonding_curve_pct(mint)
        except Exception:
            return None
        if curve_pct <= 0:
            return None   # unreadable: an estimate is not evidence

        self.note_curve(mint, curve_pct)
        if curve_pct < self.min_curve:
            return None

        self.watching[mint] = curve_pct
        self._watching_times[mint] = time.time()

        # Periodic cleanup of stale entries (not seen in 10 minutes)
        if len(self.watching) > 500:
            cutoff = time.time() - 600
            stale = [m for m, t in self._watching_times.items() if t < cutoff]
            for m in stale:
                self.watching.pop(m, None)
                self._watching_times.pop(m, None)
                self.bought.discard(m)
                self._seen_below.discard(m)

        # Detect graduation: the curve is complete AND we watched it get there
        if curve_pct >= 100 and mint not in self._graduated_at:
            if mint not in self._seen_below:
                # Already graduated before we ever saw it — not a fresh one.
                self.watching.pop(mint, None)
                self._watching_times.pop(mint, None)
                return None
            symbol = event.get("symbol", mint[:8])
            creator = event.get("traderPublicKey", "")
            self._graduated_at[mint] = time.time()
            self._ready_to_buy[mint] = {
                "mint": mint,
                "symbol": symbol,
                "traderPublicKey": creator,
                "marketCapSol": float(event.get("marketCapSol", 0) or 0),
            }
            log.info(f"  GRAD-DETECTED: {symbol} graduated — waiting for pool to establish")

        return None  # Never buy directly — post-graduation poll handles it

    def get_post_graduation_candidates(self, min_wait: float = 60,
                                        max_wait: float = 300) -> list[dict]:
        """Get tokens that graduated and are ready for post-graduation buy.

        Returns list of event dicts for tokens where:
        - Graduated at least min_wait seconds ago (pool established)
        - Not older than max_wait seconds (still fresh)
        - Not already bought
        """
        now = time.time()
        ready = []
        expired = []
        for mint, grad_time in self._graduated_at.items():
            if mint in self.bought:
                expired.append(mint)
                continue
            age = now - grad_time
            if age > max_wait:
                expired.append(mint)
                continue
            if age >= min_wait and mint in self._ready_to_buy:
                ready.append(self._ready_to_buy[mint])

        # Clean up expired
        for mint in expired:
            self._graduated_at.pop(mint, None)
            self._ready_to_buy.pop(mint, None)

        return ready

    def mark_bought(self, mint: str):
        """Mark a graduated token as bought."""
        self.bought.add(mint)
        self._graduated_at.pop(mint, None)
        self._ready_to_buy.pop(mint, None)

    def get_hot_list(self) -> list[str]:
        """Return mints at 80%+ curve — these need faster polling."""
        return [m for m, p in self.watching.items()
                if p >= 80 and m not in self.bought]

    def get_near_graduation(self) -> list[tuple[str, float]]:
        return sorted(
            [(m, p) for m, p in self.watching.items() if p >= self.min_curve],
            key=lambda x: x[1], reverse=True,
        )[:10]
