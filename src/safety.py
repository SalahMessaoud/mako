"""Token safety analysis — DexScreener data + on-chain verification."""

import logging
from dataclasses import dataclass
from market_data import TokenData
from onchain import OnChainAnalyzer, OnChainResult
from config import Config

log = logging.getLogger(__name__)


@dataclass
class SafetyResult:
    passed: bool
    score: int
    warnings: list[str]
    details: dict
    onchain: OnChainResult = None
    # True when on-chain data could not be read. The token is not known to be
    # bad — callers should skip the trade but must NOT blacklist it.
    unverified: bool = False

    def __str__(self):
        s = "PASS" if self.passed else "FAIL"
        w = ", ".join(self.warnings[:3]) if self.warnings else "clean"
        return f"[{s}] Safety {self.score}/100 — {w}"


class SafetyChecker:
    """
    Multi-layer safety analysis.
    Layer 1 (DexScreener): Liquidity, age, buy/sell ratio, volume, mcap
    Layer 2 (On-chain):    Mint authority, freeze authority, holder concentration
    """

    def __init__(self, config: Config, onchain: OnChainAnalyzer = None):
        self.cfg = config
        self.onchain = onchain

    async def check(self, token: TokenData, is_snipe: bool = False,
                    creator: str = "") -> SafetyResult:
        warnings = []
        scores = {}

        # Layer 1: DexScreener
        scores["liq"] = self._score_liquidity(token)
        if scores["liq"] < 40:
            warnings.append(f"Low liq: ${token.liquidity_usd:,.0f}")

        scores["age"] = self._score_age(token, is_snipe)
        if scores["age"] < 30 and not is_snipe:
            warnings.append(f"Too new: {token.age_seconds:.0f}s")

        scores["honeypot"] = self._score_honeypot(token)
        if scores["honeypot"] < 25:
            warnings.append("Possible honeypot")

        scores["volume"] = self._score_volume(token)
        if scores["volume"] < 25:
            warnings.append("Suspicious volume")

        scores["mcap_liq"] = self._score_mcap_ratio(token)
        if scores["mcap_liq"] < 25:
            warnings.append("Bad mcap/liq ratio")

        # Layer 2: On-chain
        onchain_result = None
        onchain_failed = False
        if self.onchain:
            try:
                onchain_result = await self.onchain.analyze(token.mint, creator)
                scores["onchain"] = onchain_result.score
                warnings.extend(onchain_result.warnings)
                if not onchain_result.freeze_authority_revoked:
                    scores["onchain"] = max(scores["onchain"] - 20, 0)
                if onchain_result.top_holder_pct > self.cfg.max_top_holder_pct:
                    scores["onchain"] = max(scores["onchain"] - 15, 0)
            except Exception as e:
                log.debug(f"On-chain check failed: {e}")
                scores["onchain"] = 50
                onchain_result = None
                onchain_failed = True

        # Weighted score
        if "onchain" in scores:
            w = {"liq": 20, "age": 8, "honeypot": 22, "volume": 10,
                 "mcap_liq": 10, "onchain": 30}
        else:
            w = {"liq": 30, "age": 10, "honeypot": 30, "volume": 15, "mcap_liq": 15}

        total_w = sum(w.values())
        final = sum(scores.get(k, 50) * w.get(k, 0) for k in w) // total_w

        # Pass criteria
        if is_snipe:
            passed = final >= 25 and scores["honeypot"] >= 15
            if onchain_result:
                if not onchain_result.freeze_authority_revoked:
                    passed = False
                if onchain_result.top_holder_pct > self.cfg.max_top_holder_pct:
                    passed = False
        else:
            passed = final >= 40 and scores["liq"] >= 30 and scores["honeypot"] >= 20
            if onchain_result and not onchain_result.freeze_authority_revoked:
                passed = False

        # Fail closed: if the on-chain layer could not be read, the freeze
        # authority and holder checks above ran on default values and prove
        # nothing. Skip the trade without condemning the token.
        unverified = False
        if self.onchain and getattr(self.cfg, "safety_fail_closed", True):
            if onchain_failed or (onchain_result and not onchain_result.verified):
                unverified = True
                passed = False
                warnings.insert(0, "On-chain data unavailable")
        elif onchain_result and not onchain_result.mint_read_ok:
            # Fail-open mode: an unreadable mint must not look like an active
            # freeze authority.
            unverified = True

        return SafetyResult(passed=passed, score=final, warnings=warnings,
                            details=scores, onchain=onchain_result,
                            unverified=unverified)

    def _score_liquidity(self, t): return 100 if t.liquidity_usd >= 100_000 else 85 if t.liquidity_usd >= 50_000 else 65 if t.liquidity_usd >= 20_000 else 50 if t.liquidity_usd >= 10_000 else 30 if t.liquidity_usd >= 5_000 else 10
    def _score_age(self, t, snipe):
        if snipe: return 60 if t.age_seconds >= 3 else 30
        return 100 if t.age_seconds >= 86400 else 80 if t.age_seconds >= 3600 else 60 if t.age_seconds >= 600 else 40 if t.age_seconds >= 60 else 15
    def _score_honeypot(self, t):
        total = t.buys_5m + t.sells_5m
        if total < 3: return 40
        r = t.sells_5m / total
        return 100 if r >= 0.3 else 75 if r >= 0.2 else 50 if r >= 0.1 else 25 if r >= 0.03 else 5
    def _score_volume(self, t):
        if t.liquidity_usd < 1: return 0
        r = t.volume_24h / t.liquidity_usd
        return 100 if 0.5 <= r <= 15 else 65 if 0.1 <= r <= 30 else 15 if r > 50 else 40
    def _score_mcap_ratio(self, t):
        if t.liquidity_usd < 1 or t.market_cap < 1: return 30
        r = t.market_cap / t.liquidity_usd
        return 100 if r < 30 else 70 if r < 80 else 40 if r < 200 else 10
