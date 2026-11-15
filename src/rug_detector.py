"""Real-time rug detection — monitor held positions for creator sells, liquidity pulls, curve drops."""

import logging
import time
from dataclasses import dataclass, field
from onchain import OnChainAnalyzer

log = logging.getLogger(__name__)


@dataclass
class RugSignal:
    """Signal indicating potential rug activity."""
    mint: str
    symbol: str
    reason: str
    severity: str  # "critical" or "warning"
    sell_pct: int = 100  # how much to sell (100 = full exit)


@dataclass
class _MonitoredToken:
    """Internal tracking state for a monitored position."""
    mint: str
    creator: str
    baseline_curve_sol: float
    creator_sell_count: int = 0
    creator_total_sold_sol: float = 0.0
    last_check_time: float = 0.0
    graduated: bool = False  # Token graduated from pump.fun — curve drain is expected
    curve_pct: float = 0.0  # Last known bonding curve %


class RugDetector:
    """
    Monitors held positions for rug indicators:

    Event-driven (called on every token trade):
    - Creator sells all tokens -> critical
    - Creator large sell (>5 SOL) -> critical
    - Creator sells 3+ times -> critical
    - Whale dump (>10 SOL single sell) -> warning (log only)

    Polling (called every ~15s per token):
    - Bonding curve SOL drops >40% from baseline -> critical
    - Bonding curve SOL drops >20% -> warning
    - Top holder concentration >60% -> warning
    """

    def __init__(self, onchain: OnChainAnalyzer, config=None):
        self.onchain = onchain
        self._monitored: dict[str, _MonitoredToken] = {}  # mint -> tracking

        # Config overrides
        self._creator_sell_exit = True
        self._curve_drop_pct = 40.0
        if config:
            self._creator_sell_exit = getattr(config, 'rug_creator_sell_exit', True)
            self._curve_drop_pct = getattr(config, 'rug_curve_drop_pct', 40.0)

    def start_monitoring(self, mint: str, creator: str, curve_sol: float,
                         graduated: bool = False, curve_pct: float = 0.0):
        """Start monitoring a position for rug indicators."""
        self._monitored[mint] = _MonitoredToken(
            mint=mint,
            creator=creator,
            # 0 = baseline not known yet (the read failed). It is filled in by
            # the first successful poll instead of using a fake floor that
            # would silently disable curve-drop detection.
            baseline_curve_sol=max(curve_sol, 0.0),
            graduated=graduated,
            curve_pct=curve_pct,
        )
        log.debug(f"  Rug monitor started: {mint[:16]} (creator={creator[:8]}, "
                  f"curve={curve_sol:.2f} SOL, graduated={graduated})")

    def update_curve_pct(self, mint: str, curve_pct: float):
        """Update the bonding curve % for a monitored token."""
        if mint in self._monitored:
            self._monitored[mint].curve_pct = curve_pct

    def stop_monitoring(self, mint: str):
        """Stop monitoring a closed position."""
        removed = self._monitored.pop(mint, None)
        if removed:
            log.debug(f"  Rug monitor stopped: {mint[:16]}")

    def evaluate_trade_event(self, event: dict, positions: dict) -> "RugSignal | None":
        """
        Evaluate a token trade event for rug indicators.
        Called from _on_token_trade handler for every trade on monitored tokens.

        Returns RugSignal on critical detection, None otherwise.
        """
        mint = event.get("mint", "")
        if mint not in self._monitored:
            return None

        mon = self._monitored[mint]
        trader = event.get("traderPublicKey", "")
        tx_type = event.get("txType", "")
        sol_amount = float(event.get("solAmount", 0) or 0)
        new_balance = float(event.get("newTokenBalance", 0) or 0)

        pos = positions.get(mint)
        symbol = pos.symbol if pos else mint[:8]

        if tx_type != "sell":
            return None

        # ── Creator sell detection ────────────────────────────
        if trader == mon.creator and self._creator_sell_exit:
            mon.creator_sell_count += 1
            mon.creator_total_sold_sol += sol_amount

            # Creator sold ALL tokens
            if new_balance <= 0:
                reason = f"Creator sold ALL tokens ({sol_amount:.2f} SOL)"
                log.warning(f"  RUG DETECTED: {symbol} — {reason}")
                return RugSignal(
                    mint=mint, symbol=symbol, reason=reason,
                    severity="critical", sell_pct=100,
                )

            # Creator large sell — slightly higher per-event threshold near
            # graduation (>90% curve) where profit-taking is more common.
            # Also enforce a cumulative cap to catch gradual dumping.
            near_graduation = mon.curve_pct >= 90
            per_event_threshold = 7.0 if near_graduation else 5.0
            cumulative_cap = 12.0 if near_graduation else 8.0

            if sol_amount >= per_event_threshold:
                reason = (f"Creator large sell: {sol_amount:.2f} SOL "
                          f"(total: {mon.creator_total_sold_sol:.2f} SOL"
                          f", threshold: {per_event_threshold:.0f})")
                log.warning(f"  RUG DETECTED: {symbol} — {reason}")
                return RugSignal(
                    mint=mint, symbol=symbol, reason=reason,
                    severity="critical", sell_pct=100,
                )

            # Cumulative creator sells exceed cap (catches gradual dumping)
            if mon.creator_total_sold_sol >= cumulative_cap:
                reason = (f"Creator cumulative sells: {mon.creator_total_sold_sol:.2f} SOL "
                          f"(cap: {cumulative_cap:.0f} SOL in {mon.creator_sell_count} sells)")
                log.warning(f"  RUG DETECTED: {symbol} — {reason}")
                return RugSignal(
                    mint=mint, symbol=symbol, reason=reason,
                    severity="critical", sell_pct=100,
                )

            # Creator selling repeatedly
            if mon.creator_sell_count >= 3:
                reason = (f"Creator sold {mon.creator_sell_count} times "
                          f"(total: {mon.creator_total_sold_sol:.2f} SOL)")
                log.warning(f"  RUG DETECTED: {symbol} — {reason}")
                return RugSignal(
                    mint=mint, symbol=symbol, reason=reason,
                    severity="critical", sell_pct=100,
                )

        # ── Whale dump detection ──────────────────────────────
        if sol_amount >= 10.0 and trader != mon.creator:
            log.info(f"  RUG WARNING: {symbol} — whale dump {sol_amount:.2f} SOL "
                     f"by {trader[:8]}...")
            # Warning only, no sell signal
            return None

        return None

    async def check_onchain(self, mint: str, positions: dict) -> "RugSignal | None":
        """
        Polling-based rug detection via on-chain data.
        Called periodically (every ~15s per token) from poll loop.

        Returns RugSignal on critical detection, None otherwise.
        """
        if mint not in self._monitored:
            return None

        mon = self._monitored[mint]
        now = time.time()

        # Rate limit: check each token at most every 15 seconds
        if now - mon.last_check_time < 15:
            return None
        mon.last_check_time = now

        pos = positions.get(mint)
        symbol = pos.symbol if pos else mint[:8]

        try:
            result = await self.onchain.analyze(mint)

            # ── Bonding curve SOL drop ────────────────────────
            # Skip for graduated tokens — curve draining to 0 is expected
            # after migration to Raydium
            if (result.is_pump_token and mon.baseline_curve_sol <= 0
                    and not mon.graduated and result.bonding_curve_sol > 0):
                mon.baseline_curve_sol = result.bonding_curve_sol
                log.debug(f"  Rug monitor baseline set late: {symbol} "
                          f"{mon.baseline_curve_sol:.2f} SOL")
            elif result.is_pump_token and mon.baseline_curve_sol > 0 and not mon.graduated:
                current_sol = result.bonding_curve_sol
                drop_pct = ((mon.baseline_curve_sol - current_sol)
                            / mon.baseline_curve_sol * 100)

                if drop_pct >= self._curve_drop_pct:
                    # Before flagging, check if this is just graduation
                    try:
                        graduated = await self.onchain.is_graduated(mint)
                        if graduated is None:
                            raise RuntimeError("graduation status unknown")
                        if graduated:
                            mon.graduated = True
                            log.info(f"  {symbol} graduated — curve drain is normal, skipping rug flag")
                        else:
                            reason = (f"Bonding curve dropped {drop_pct:.0f}% "
                                      f"({mon.baseline_curve_sol:.1f} -> {current_sol:.1f} SOL)")
                            log.warning(f"  RUG DETECTED: {symbol} — {reason}")
                            return RugSignal(
                                mint=mint, symbol=symbol, reason=reason,
                                severity="critical", sell_pct=100,
                            )
                    except Exception:
                        # Can't verify graduation — flag the rug to be safe
                        reason = (f"Bonding curve dropped {drop_pct:.0f}% "
                                  f"({mon.baseline_curve_sol:.1f} -> {current_sol:.1f} SOL)")
                        log.warning(f"  RUG DETECTED: {symbol} — {reason}")
                        return RugSignal(
                            mint=mint, symbol=symbol, reason=reason,
                            severity="critical", sell_pct=100,
                        )
                elif drop_pct >= 20:
                    log.info(f"  RUG WARNING: {symbol} — curve dropped {drop_pct:.0f}% "
                             f"({current_sol:.1f} SOL)")

            # ── Top holder concentration ──────────────────────
            if result.top_holder_pct > 60:
                log.info(f"  RUG WARNING: {symbol} — top holder {result.top_holder_pct:.0f}%")

        except Exception as e:
            log.debug(f"  Rug on-chain check error for {mint[:8]}: {e}")

        return None

    @property
    def monitored_count(self) -> int:
        return len(self._monitored)
