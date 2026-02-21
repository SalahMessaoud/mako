"""Smart wallet discovery — auto-discover profitable wallets for copy-trading."""

import logging
import time
from db import Database

log = logging.getLogger(__name__)


class WalletDiscovery:
    """
    Automatically discovers profitable wallets by observing trade events,
    tracking candidates, and promoting/demoting based on performance.

    - manual_wallets: from config, never removed
    - auto_wallets: discovered wallets, can be promoted/demoted
    - candidates: observed wallets being tracked for potential promotion
    """

    MAX_CANDIDATES = 5000

    def __init__(self, db: Database, manual_wallets: list[str],
                 max_auto: int = 10, min_score: float = 50.0,
                 min_buys: int = 2, min_sells: int = 1,
                 min_ratio: float = 1.15):
        self.db = db
        self.manual_wallets: set[str] = set(manual_wallets)
        self.max_auto = max_auto
        self.min_score = min_score
        self.min_buys = min_buys
        self.min_sells = min_sells
        self.min_ratio = min_ratio

        # Load persisted auto wallets from DB
        saved = db.get_state("auto_wallets", [])
        self.auto_wallets: set[str] = set(saved) if saved else set()

        # Candidate tracking: wallet -> {buys, sells, sol_bought, sol_sold, first_seen}
        self._candidates: dict[str, dict] = {}

        if self.auto_wallets:
            log.info(f"  Wallet discovery: loaded {len(self.auto_wallets)} auto wallets")

    @property
    def all_watched(self) -> list[str]:
        """All wallets to watch (manual + auto)."""
        return list(self.manual_wallets | self.auto_wallets)

    def observe_trade(self, event: dict):
        """
        Called on all token trade events to track candidate wallets.
        Observes buy/sell patterns to identify potentially profitable wallets.
        """
        trader = event.get("traderPublicKey", "")
        if not trader:
            return

        # Don't track wallets we already watch
        if trader in self.manual_wallets or trader in self.auto_wallets:
            return

        tx_type = event.get("txType", "")
        sol_amount = float(event.get("solAmount", 0) or 0)

        if sol_amount < 0.01:
            return

        if trader not in self._candidates:
            # Cap candidates to prevent unbounded growth
            if len(self._candidates) >= self.MAX_CANDIDATES:
                self._evict_oldest_candidates()
            self._candidates[trader] = {
                "buys": 0, "sells": 0,
                "sol_bought": 0.0, "sol_sold": 0.0,
                "first_seen": time.time(),
            }

        c = self._candidates[trader]
        if tx_type == "buy":
            c["buys"] += 1
            c["sol_bought"] += sol_amount
        elif tx_type == "sell":
            c["sells"] += 1
            c["sol_sold"] += sol_amount

    def check_promotions(self) -> list[str]:
        """
        Check for wallets to promote or demote. Called every ~5 minutes.

        Promotion criteria:
        - DB wallet score >= min_score, win rate >= 55%, 5+ trades
        - OR candidate tracking shows sells > 1.3x buys

        Demotion: auto wallets with score < 30 and 10+ trades.

        Returns list of newly promoted wallet addresses.
        """
        newly_promoted = []

        # 1. Check DB wallet scores for promotion
        all_scores = self.db.get_all_wallet_scores()
        for ws in all_scores:
            wallet = ws["wallet"]
            if wallet in self.manual_wallets or wallet in self.auto_wallets:
                continue
            if len(self.auto_wallets) >= self.max_auto:
                break

            score = ws.get("score", 0)
            total = ws.get("total_trades", 0)
            wins = ws.get("winning_trades", 0)
            win_rate = (wins / total * 100) if total > 0 else 0

            # Anti-wash-trade filters
            tokens = ws.get("tokens_traded", "[]")
            if isinstance(tokens, str):
                import json
                try:
                    tokens = json.loads(tokens)
                except Exception:
                    tokens = []
            unique_tokens = len(tokens) if isinstance(tokens, list) else 0

            # Reject: 100% win rate with 5+ trades (real traders lose sometimes)
            if total >= 5 and win_rate > 95:
                continue
            # Reject: trades only 1-2 tokens (wash trading the same token)
            if total >= 5 and unique_tokens < 3:
                continue
            # Reject: suspiciously round PnL per trade (pipeline score inflation)
            pnl = ws.get("total_pnl_sol", 0)
            avg_pnl = pnl / total if total > 0 else 0
            if total >= 5 and abs(avg_pnl - 0.2) < 0.02:
                continue

            if score >= self.min_score and win_rate >= 55 and total >= 5:
                self.auto_wallets.add(wallet)
                newly_promoted.append(wallet)
                log.info(f"  WALLET PROMOTED: {wallet[:12]}... "
                         f"(score={score:.0f}, WR={win_rate:.0f}%, trades={total}, "
                         f"tokens={unique_tokens})")

        # 2. Check candidate tracker for profitable wallets
        for wallet, c in list(self._candidates.items()):
            if wallet in self.manual_wallets or wallet in self.auto_wallets:
                continue
            if len(self.auto_wallets) >= self.max_auto:
                break

            # Need enough data
            if c["buys"] < self.min_buys or c["sells"] < self.min_sells:
                continue

            # Check if selling significantly more than buying
            if c["sol_bought"] > 0 and c["sol_sold"] / c["sol_bought"] >= self.min_ratio:
                self.auto_wallets.add(wallet)
                newly_promoted.append(wallet)
                self._candidates.pop(wallet, None)
                ratio = c["sol_sold"] / c["sol_bought"]
                log.info(f"  WALLET PROMOTED (candidate): {wallet[:12]}... "
                         f"(ratio={ratio:.2f}x, buys={c['buys']}, sells={c['sells']})")

        # 3. Demote underperforming auto wallets
        for wallet in list(self.auto_wallets):
            ws = self.db.get_wallet_score(wallet)
            if ws and ws["total_trades"] >= 10 and ws["score"] < 30:
                self.auto_wallets.discard(wallet)
                log.info(f"  WALLET DEMOTED: {wallet[:12]}... "
                         f"(score={ws['score']:.0f}, trades={ws['total_trades']})")

        # Persist auto wallets
        self.db.set_state("auto_wallets", list(self.auto_wallets))

        return newly_promoted

    def score_pipeline_outcome(self, creator: str, mint: str, passed: bool,
                               confidence: float = 0.0):
        """Score a creator based on whether their token passed the pipeline.

        Pipeline passes are scored conservatively to avoid inflating
        creator scores — only real closed trades should drive promotion.
        """
        if not creator:
            return
        # Use tiny scores so pipeline outcomes inform but don't dominate
        pnl_equiv = 0.01 if passed else -0.005
        self.db.update_wallet_score(creator, pnl_equiv, 0, mint, passed)

    def get_stats(self) -> dict:
        """Get discovery statistics for reporting."""
        return {
            "manual_wallets": len(self.manual_wallets),
            "auto_wallets": len(self.auto_wallets),
            "candidates_tracked": len(self._candidates),
            "auto_wallet_list": [w[:12] + "..." for w in self.auto_wallets],
        }

    def _evict_oldest_candidates(self):
        """Remove oldest half of candidates when cap is reached."""
        sorted_by_time = sorted(
            self._candidates.items(),
            key=lambda x: x[1]["first_seen"],
        )
        to_remove = sorted_by_time[:len(sorted_by_time) // 2]
        for wallet, _ in to_remove:
            self._candidates.pop(wallet, None)
