"""Tests for the strategy-review fixes: graduation detection, stops, the
momentum pipeline, wallet discovery / polling, fees and paper-mode costs.
Everything external is faked (see helpers.py)."""

import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

import helpers
from helpers import (MINT, FakeMarket, FakeNotify, FakeOnchain, FakeStream,
                     make_config, make_live_bot, make_tx, open_position)
from market_data import TokenData
from onchain import BondingCurveState, OnChainAnalyzer, RpcUnavailable
from strategies import GraduationStrategy, Signal
from txutil import TradeResult
from wallet_discovery import WalletDiscovery

W1 = "WalletManual1111111111111111111111111111111"
W2 = "WalletManual2222222222222222222222222222222"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def token(price_sol=1e-6, pool_age_min=3.0, change_5m=0.0, buys=10, sells=10,
          liquidity=50_000):
    created = int((time.time() - pool_age_min * 60) * 1000) if pool_age_min else 0
    return TokenData(
        mint=MINT, symbol="TKN", name="Token", price_usd=price_sol * 100,
        price_sol=price_sol, liquidity_usd=liquidity, volume_24h=100_000,
        volume_5m=1000, price_change_5m=change_5m, price_change_1h=0,
        price_change_24h=0, market_cap=100_000, fdv=100_000, pair_address="P",
        pair_created_at=created, buys_5m=buys, sells_5m=sells)


class Onchain(FakeOnchain):
    """FakeOnchain plus the readers the strategies use."""

    def __init__(self):
        super().__init__()
        self.pct = {}                 # mint -> curve %
        self.pct_calls = []
        self.states = {}              # mint -> BondingCurveState
        self.tx_calls = []
        self.tx_count = 0
        self.rpc = None               # callable(method, params) for the poller

    async def get_bonding_curve_pct(self, mint):
        self.pct_calls.append(mint)
        return self.pct.get(mint, 0.0)

    async def get_bonding_curve_states_batch(self, mints):
        return {m: self.states[m] for m in mints if m in self.states}

    async def get_bonding_curve_state(self, mint):
        return self.states.get(mint)

    async def get_recent_tx_count(self, mint, seconds=300):
        self.tx_calls.append(mint)
        return self.tx_count

    async def _rpc_call_strict(self, method, params):
        return self.rpc(method, params)

    async def _rpc_call(self, method, params):
        try:
            return self.rpc(method, params)
        except RpcUnavailable:
            return None


def make_bot(tmpdir, **cfg_over):
    """A paper-mode bot with faked chain, market, stream and notifications."""
    from bot import MemeBot
    bot = MemeBot(make_config(**cfg_over), db_path=os.path.join(tmpdir, "t.db"))
    bot.onchain = Onchain()
    bot.safety.onchain = bot.onchain
    if hasattr(bot, "graduation"):
        bot.graduation.onchain = bot.onchain
    bot.market = FakeMarket()
    bot.stream = FakeStream()
    bot.notify = FakeNotify()
    bot.notify.candidate_alert = lambda **k: None
    return bot


class Case(unittest.IsolatedAsyncioTestCase):
    cfg = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch("asyncio.sleep", helpers.fast_sleep)
        p.start()
        self.addCleanup(p.stop)
        self.bot = make_bot(self.tmp.name, **self.cfg)

    async def drain(self):
        while self.bot._bg_tasks:
            await asyncio.gather(*list(self.bot._bg_tasks), return_exceptions=True)


# ───────────────────────── GRADUATION ─────────────────────────

class GraduationDetection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.oc = Onchain()
        self.grad = GraduationStrategy(None, min_curve_pct=90, onchain=self.oc)

    async def test_token_that_was_already_graduated_is_not_a_fresh_graduation(self):
        # The PEPONK case: an event arrives for a token whose curve completed
        # months ago. It used to be queued for a post-graduation buy.
        self.oc.pct[MINT] = 100.0
        await self.grad.evaluate_trade({"mint": MINT, "symbol": "OLD"})
        self.assertEqual(self.grad._graduated_at, {})
        self.assertEqual(self.grad.get_post_graduation_candidates(min_wait=0), [])

    async def test_token_watched_on_the_curve_then_completing_is_detected(self):
        self.oc.pct[MINT] = 95.0
        await self.grad.evaluate_trade({"mint": MINT, "symbol": "NEW"})
        self.assertEqual(self.grad._graduated_at, {})
        self.oc.pct[MINT] = 100.0
        await self.grad.evaluate_trade({"mint": MINT, "symbol": "NEW"})
        self.assertIn(MINT, self.grad._graduated_at)

    async def test_unreadable_curve_never_falls_back_to_an_estimate(self):
        # Virtual SOL of 85 is only ~65% of the way; the old fallback read
        # it as 100% and declared a graduation.
        self.oc.pct[MINT] = 0.0
        await self.grad.evaluate_trade({"mint": MINT, "vSolInBondingCurve": 85})
        self.assertEqual(self.grad._graduated_at, {})
        self.assertNotIn(MINT, self.grad.watching)

    async def test_new_tokens_do_not_cost_an_rpc_call(self):
        await self.grad.evaluate_trade({"mint": MINT, "vSolInBondingCurve": 31})
        self.assertEqual(self.oc.pct_calls, [])
        await self.grad.evaluate_trade({"mint": MINT, "vSolInBondingCurve": 110})
        self.assertEqual(self.oc.pct_calls, [MINT])


class PostGraduationBuy(Case):
    cfg = dict(strategy="graduation", use_jupiter=True)

    def queue(self, age=100):
        g = self.bot.graduation
        g._graduated_at[MINT] = time.time() - age
        g._ready_to_buy[MINT] = {"mint": MINT, "symbol": "TKN",
                                 "traderPublicKey": "Creator1111"}

    async def test_months_old_pool_is_not_bought(self):
        self.queue()
        self.bot.market.token = token(pool_age_min=379_545)
        await self.bot._run_post_graduation_buy()
        await self.drain()
        self.assertEqual(self.bot.risk.positions, {})
        self.assertIn(MINT, self.bot.graduation.bought)     # not retried

    async def test_pool_of_unknown_age_is_not_bought(self):
        self.queue()
        self.bot.market.token = token(pool_age_min=None)
        await self.bot._run_post_graduation_buy()
        await self.drain()
        self.assertEqual(self.bot.risk.positions, {})

    async def test_no_trade_data_waits_instead_of_counting_as_a_calm_entry(self):
        self.queue()
        self.bot.market.token = token(buys=0, sells=1)
        await self.bot._run_post_graduation_buy()
        await self.drain()
        self.assertEqual(self.bot.risk.positions, {})
        self.assertNotIn(MINT, self.bot.graduation.bought)  # still a candidate

    async def test_fresh_graduate_is_bought_with_its_stop_loss_armed(self):
        self.queue()
        self.bot.market.token = token()
        await self.bot._run_post_graduation_buy()
        await self.drain()
        pos = self.bot.risk.positions[MINT]
        self.assertTrue(pos.graduated)
        # Bought after graduation: no migration dip to sit through.
        self.assertFalse(pos.in_graduation_grace)

    async def test_position_held_through_graduation_does_get_the_grace(self):
        pos = self.bot.risk.open(MINT, "TKN", 0.001, 1e-5, 1000, 0.03, "momentum")
        pos.update_curve(97.0)
        self.bot.risk.mark_graduated(MINT, grace=True)
        self.assertTrue(pos.in_graduation_grace)

    async def test_token_found_already_graduated_gets_no_grace(self):
        pos = self.bot.risk.open(MINT, "TKN", 0.001, 1e-5, 1000, 0.03, "copy_trade")
        self.bot.onchain.graduated = True
        self.assertTrue(await self.bot._is_graduated(MINT, pos))
        self.assertTrue(pos.graduated)
        self.assertFalse(pos.in_graduation_grace)


# ───────────────────────── STOPS ─────────────────────────

class EarlyStops(Case):
    cfg = dict(stop_loss_pct=20.0, momentum_stop_loss_pct=12.0)

    def position(self, strategy, age):
        pos = self.bot.risk.open(MINT, "TKN", 1.0, 0.01, 1000, 0.03, strategy)
        pos.entry_time = time.time() - age
        pos.stop_loss_first_seen = time.time() - 20     # dip already confirmed
        return pos

    def test_widened_early_stop_is_capped(self):
        cfg = self.bot.cfg
        self.assertEqual(cfg.early_stop_pct(20), 40)    # was 80
        self.assertEqual(cfg.early_stop_pct(12), 40)    # was 48
        self.assertEqual(cfg.early_stop_pct(5), 20)     # 4x, under the cap
        self.assertEqual(cfg.early_stop_pct(45), 45)    # never tighter than the stop

    def test_a_45_percent_loss_in_the_first_minute_is_stopped(self):
        self.position("graduation", age=60)
        self.assertEqual(self.bot.risk.check_exits(MINT, 0.55), ("stop_loss", 100))

    def test_a_30_percent_dip_in_the_first_minute_is_still_tolerated(self):
        self.position("graduation", age=60)
        self.assertEqual(self.bot.risk.check_exits(MINT, 0.70)[0], "none")

    def test_normal_stop_applies_after_the_window(self):
        self.position("graduation", age=600)
        self.assertEqual(self.bot.risk.check_exits(MINT, 0.78), ("stop_loss", 100))

    def test_curve_protection_gives_way_at_the_protected_stop(self):
        pos = self.position("momentum", age=600)
        pos.update_curve(70.0)
        pos.update_curve(72.0)                           # rising, fresh
        self.assertEqual(self.bot.risk.check_exits(MINT, 0.60)[0], "none")
        self.assertEqual(self.bot.risk.check_exits(MINT, 0.45), ("stop_loss", 100))


# ───────────────────────── MOMENTUM ─────────────────────────

def add_candidate(bot, mint, age, v_sol=30.0, creator="Creator1111"):
    bot.mint_pool.add({"mint": mint, "symbol": mint[:4], "initialBuy": 1e9,
                       "marketCapSol": 30, "vSolInBondingCurve": v_sol,
                       "traderPublicKey": creator})
    bot.mint_pool.candidates[mint].time = time.time() - age


class MomentumPipeline(Case):
    cfg = dict(strategy="momentum", min_momentum_confidence=0.1,
               min_momentum_sol_in_curve=1.0, min_momentum_txns=1,
               min_momentum_curve_growth=1.0, min_momentum_growth_rate=0.1,
               auto_discover_wallets=True)

    def test_momentum_ranking_prefers_the_ages_its_scorer_rewards(self):
        add_candidate(self.bot, "YOUNG", age=200)       # 3.3 min
        add_candidate(self.bot, "OLDER", age=600)       # 10 min
        pool = self.bot.mint_pool
        self.assertEqual(pool.get_best_candidates(prefer_young=True)[0], "YOUNG")
        self.assertEqual(pool.get_best_candidates()[0], "OLDER")   # graduation poll

    def test_live_activity_lifts_a_token_in_the_momentum_ranking(self):
        add_candidate(self.bot, "QUIET", age=200)
        add_candidate(self.bot, "BUSY", age=450)
        for _ in range(40):
            self.bot.mint_pool.record_trade("BUSY")
        self.assertEqual(
            self.bot.mint_pool.get_best_candidates(prefer_young=True)[0], "BUSY")

    async def test_activity_checks_rotate_beyond_the_top_ten(self):
        for i in range(20):
            add_candidate(self.bot, f"M{i:02d}", age=200 + i)
        for _ in range(3):
            await self.bot._run_momentum()
        calls = self.bot.onchain.tx_calls
        self.assertEqual(len(calls), 30)                 # still ten per cycle
        self.assertGreater(len(set(calls)), 15)          # was always the same ten

    def curve(self, v_sol):
        self.bot.onchain.states[MINT] = BondingCurveState(
            virtual_sol=v_sol, real_sol=v_sol - 30, virtual_tokens=7e14,
            real_tokens=5e14, pct=(v_sol - 30) / 85 * 100)

    async def test_signal_needs_growth_since_an_earlier_cycle(self):
        add_candidate(self.bot, MINT, age=200)
        self.curve(45.0)
        ev = self.bot._evaluate_onchain_momentum

        # First sighting: nothing to compare with, so no signal yet.
        self.assertEqual(await ev([(MINT, 60)]), [])

        # Flat since the earlier reading: not momentum.
        self.bot._curve_obs[MINT] = (time.time() - 10, 45.0)
        self.assertEqual(await ev([(MINT, 60)]), [])

        # Higher than the earlier reading: signal.
        self.bot._curve_obs[MINT] = (time.time() - 10, 43.0)
        signals = await ev([(MINT, 60)])
        self.assertEqual([s.mint for s in signals], [MINT])

    async def test_fast_cycles_still_reach_a_comparison(self):
        # Cycles closer together than CURVE_OBS_MIN_SEC must not keep
        # replacing the earlier reading, or the signal would wait forever.
        add_candidate(self.bot, MINT, age=200)
        ev = self.bot._evaluate_onchain_momentum
        self.curve(43.0)
        self.assertEqual(await ev([(MINT, 60)]), [])
        first = self.bot._curve_obs[MINT]
        self.curve(44.0)
        self.assertEqual(await ev([(MINT, 60)]), [])     # 0s later: too fresh
        self.assertEqual(self.bot._curve_obs[MINT], first)   # reading kept
        self.bot._curve_obs[MINT] = (first[0] - 5, first[1])  # 5s pass
        self.curve(45.0)
        self.assertEqual([s.mint for s in await ev([(MINT, 60)])], [MINT])

    async def test_stale_earlier_reading_is_not_used(self):
        add_candidate(self.bot, MINT, age=200)
        self.curve(45.0)
        self.bot._curve_obs[MINT] = (time.time() - 600, 40.0)
        self.assertEqual(await self.bot._evaluate_onchain_momentum([(MINT, 60)]), [])

    async def test_pipeline_outcome_does_not_score_the_creator_as_a_copy_wallet(self):
        add_candidate(self.bot, MINT, age=200)
        self.curve(45.0)
        self.bot._curve_obs[MINT] = (time.time() - 10, 43.0)
        self.assertTrue(await self.bot._evaluate_onchain_momentum([(MINT, 60)]))
        self.assertEqual(self.bot.db.get_all_wallet_scores(), [])


class TxCount(unittest.IsolatedAsyncioTestCase):
    async def test_count_is_not_capped_at_fifty(self):
        oc = OnChainAnalyzer("http://rpc.invalid")
        self.addAsyncCleanup(oc.close)
        asked = {}

        async def rpc(method, params):
            asked["limit"] = params[1]["limit"]
            now = time.time()
            recent = [{"blockTime": now - 10}] * 120
            old = [{"blockTime": now - 900}] * 30 + [{"blockTime": None}]
            return (recent + old)[:asked["limit"]]
        oc._rpc_call = rpc
        real_mint = "So11111111111111111111111111111111111111112"
        self.assertEqual(await oc.get_recent_tx_count(real_mint, seconds=300), 120)
        self.assertGreaterEqual(asked["limit"], 200)


# ───────────────────────── WALLETS ─────────────────────────

class FakeDb:
    def __init__(self):
        self.state = {}

    def get_state(self, k, default=None):
        return self.state.get(k, default)

    def set_state(self, k, v):
        self.state[k] = v

    def get_all_wallet_scores(self):
        return []

    def get_wallet_score(self, w):
        return None


def trade(wallet, mint, kind, sol):
    return {"traderPublicKey": wallet, "mint": mint, "txType": kind, "solAmount": sol}


class Discovery(unittest.TestCase):
    def setUp(self):
        self.wd = WalletDiscovery(FakeDb(), [W1], max_auto=5)

    def round_trips(self, wallet, n, buy=1.0, sell=1.5):
        for i in range(n):
            self.wd.observe_trade(trade(wallet, f"T{i}", "buy", buy))
            self.wd.observe_trade(trade(wallet, f"T{i}", "sell", sell))

    def test_two_buys_and_a_sell_on_one_token_is_not_enough(self):
        # The old bar: promoted after 2 buys + 1 sell at 1.15x.
        self.wd.observe_trade(trade("X", "T0", "buy", 1.0))
        self.wd.observe_trade(trade("X", "T0", "buy", 1.0))
        self.wd.observe_trade(trade("X", "T0", "sell", 3.0))
        self.assertEqual(self.wd.check_promotions(), [])

    def test_profitable_round_trips_on_three_tokens_promote(self):
        self.round_trips("X", 3)
        self.assertEqual(self.wd.check_promotions(), ["X"])

    def test_sells_of_tokens_bought_before_we_watched_do_not_count(self):
        for i in range(4):
            self.wd.observe_trade(trade("X", f"T{i}", "sell", 5.0))
            self.wd.observe_trade(trade("X", f"T{i}", "buy", 1.0))
        self.assertEqual(self.wd.check_promotions(), [])

    def test_one_lucky_token_among_losers_does_not_promote(self):
        self.round_trips("X", 1, buy=1.0, sell=10.0)
        for i in range(1, 4):
            self.wd.observe_trade(trade("X", f"T{i}", "buy", 1.0))
            self.wd.observe_trade(trade("X", f"T{i}", "sell", 0.5))
        self.assertEqual(self.wd.check_promotions(), [])

    def test_token_creator_is_never_promoted(self):
        self.wd.note_creator("DEV")
        self.round_trips("DEV", 5)
        self.assertEqual(self.wd.check_promotions(), [])
        # ...also when it was seen trading before it created a token
        self.round_trips("DEV2", 5)
        self.wd.note_creator("DEV2")
        self.assertEqual(self.wd.check_promotions(), [])


class CopyScoring(Case):
    cfg = dict(strategy="copy_trade", copy_wallets=[W1, W2])

    async def test_losing_copy_trade_scores_the_wallet_and_does_not_blacklist_it(self):
        await self.bot._on_position_closed(MINT, pnl=-0.02, strategy="copy_trade",
                                           creator=W1)
        self.assertFalse(self.bot.db.is_creator_blacklisted(W1))
        scores = self.bot.db.get_all_wallet_scores()
        self.assertEqual([s["wallet"] for s in scores], [W1])

    async def test_other_strategies_blacklist_a_losing_creator_but_do_not_score_it(self):
        await self.bot._on_position_closed(MINT, pnl=-0.02, strategy="momentum",
                                           creator="Creator1111")
        self.assertTrue(self.bot.db.is_creator_blacklisted("Creator1111"))
        self.assertEqual(self.bot.db.get_all_wallet_scores(), [])


class CopyRugMonitoring(Case):
    cfg = dict(strategy="copy_trade", copy_wallets=[W1, W2], rug_detection=True)

    async def test_copied_wallet_is_not_monitored_as_the_token_creator(self):
        self.bot.risk.open(MINT, "TKN", 0.001, 1e-5, 1000, 0.02, "copy_trade",
                           creator=W1)
        await self.bot._on_position_opened(MINT, W1)
        self.assertEqual(self.bot.rug_detector._monitored[MINT].creator, "")
        sell = {"mint": MINT, "traderPublicKey": W1, "txType": "sell",
                "solAmount": 9.0, "newTokenBalance": 0}
        self.assertIsNone(self.bot.rug_detector.evaluate_trade_event(
            sell, self.bot.risk.positions))

    async def test_real_creator_is_still_monitored_for_other_strategies(self):
        self.bot.risk.open(MINT, "TKN", 0.001, 1e-5, 1000, 0.03, "momentum",
                           creator="Creator1111")
        await self.bot._on_position_opened(MINT, "Creator1111")
        self.assertEqual(self.bot.rug_detector._monitored[MINT].creator, "Creator1111")


class WalletPoller(Case):
    cfg = dict(strategy="copy_trade", copy_wallets=[W1, W2])

    def test_manual_wallets_are_polled_before_auto_discovered_ones(self):
        for auto in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF", "GGGG"):
            self.bot.copy_trade.wallets.add(auto)
        order = self.bot._wallets_to_poll()
        self.assertEqual(order[:2], [W1, W2])
        self.assertEqual(len(order), self.bot.WALLET_POLL_MAX_WALLETS)

    async def test_rpc_failure_is_counted_and_logged_not_silent(self):
        def rpc(method, params):
            raise RpcUnavailable(method)
        self.bot.onchain.rpc = rpc
        import logging
        logging.disable(logging.NOTSET)                  # helpers silences logs
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs("bot", level="WARNING") as logs:
            await self.bot._poll_wallet_transactions()
        self.assertEqual(self.bot._wallet_poll_fail, 2)
        self.assertEqual(self.bot._wallet_poll_ok, 0)
        self.assertTrue(any("Wallet poll failed" in line for line in logs.output))

    async def test_new_transactions_are_processed_oldest_first(self):
        sigs = {W1: [], W2: []}

        def rpc(method, params):
            if method == "getSignaturesForAddress":
                return [{"signature": s} for s in sigs[params[0]]]
            return {"sig": params[0]}
        self.bot.onchain.rpc = rpc
        seen = []

        async def parse(wallet, tx, sig):
            seen.append(sig)
        self.bot._parse_wallet_tx = parse

        await self.bot._poll_wallet_transactions()       # first poll: baseline
        sigs[W1] = ["sell", "buy"]                        # RPC lists newest first
        await self.bot._poll_wallet_transactions()
        self.assertEqual(seen, ["buy", "sell"])
        self.assertEqual(self.bot._wallet_poll_ok, 4)

    async def test_swap_into_a_stablecoin_is_not_copied(self):
        self.bot._on_account_trade = mock.AsyncMock()
        await self.bot._parse_wallet_tx(
            W1, make_tx(owner=W1, mint=USDC, sol_delta=-1.0, token_post=100), "s1")
        self.bot._on_account_trade.assert_not_called()
        await self.bot._parse_wallet_tx(
            W1, make_tx(owner=W1, mint=MINT, sol_delta=-1.0, token_post=100), "s2")
        self.bot._on_account_trade.assert_called_once()
        self.assertEqual(self.bot._on_account_trade.call_args[0][0]["txType"], "buy")

    async def test_poll_runs_in_the_background_and_does_not_stack(self):
        gate = asyncio.Event()

        async def slow_poll():
            await gate.wait()
        self.bot._poll_wallet_transactions = slow_poll
        self.bot._start_wallet_poll()
        first = self.bot._wallet_poll_task
        self.bot._start_wallet_poll()                     # previous still running
        self.assertIs(self.bot._wallet_poll_task, first)
        gate.set()
        await first


# ───────────────────────── FEES ─────────────────────────

class Fees(unittest.IsolatedAsyncioTestCase):
    def test_priority_fee_is_capped_relative_to_the_trade(self):
        cfg = make_config(priority_fee_sol=0.005, max_fee_pct_of_trade=5.0)
        self.assertAlmostEqual(cfg.priority_fee_for(0.03), 0.0015)
        self.assertAlmostEqual(cfg.priority_fee_for(0.03, emergency=True), 0.003)
        self.assertAlmostEqual(cfg.priority_fee_for(1.0), 0.005)      # cap not reached
        self.assertAlmostEqual(cfg.priority_fee_for(1.0, emergency=True), 0.01)

    def test_cap_can_be_switched_off(self):
        cfg = make_config(priority_fee_sol=0.005, max_fee_pct_of_trade=0)
        self.assertAlmostEqual(cfg.priority_fee_for(0.03), 0.005)

    async def test_live_trades_send_the_capped_fee(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch("asyncio.sleep", helpers.fast_sleep)
        p.start()
        self.addCleanup(p.stop)
        bot = make_live_bot(tmp.name, priority_fee_sol=0.005,
                            max_fee_pct_of_trade=5.0)
        sig = Signal(mint=MINT, symbol="TKN", strategy="sniper", confidence=0.8,
                     reason="t", creator="Creator1111")
        await bot._live_buy(sig, 0.03, 0)
        self.assertAlmostEqual(bot.pump.buy_calls[0][2]["priority_fee"], 0.0015)

        open_position(bot, sol=0.03)
        bot.pump.sell_results = [TradeResult("failed")]
        await bot._execute_sell(MINT, 100, "take_profit_full")
        self.assertAlmostEqual(bot.pump.sell_calls[0][1]["priority_fee"], 0.0015)


class PaperCosts(Case):
    cfg = dict(strategy="sniper", priority_fee_sol=0.005, max_fee_pct_of_trade=0,
               use_jito=True, jito_tip_sol=0.001, paper_fee_pct=1.5,
               paper_slippage_pct=3.0, max_buy_sol=0.03, paper_balance_sol=2.0)

    async def test_round_trip_at_an_unchanged_price_loses_what_live_would_pay(self):
        bot = self.bot
        bot.market.token = token(price_sol=1e-6)
        sig = Signal(mint=MINT, symbol="TKN", strategy="sniper", confidence=1.0,
                     reason="t", creator="Creator1111", token_data=bot.market.token)
        await bot._execute_buy(sig)
        await self.drain()
        pos = bot.risk.positions[MINT]
        size = pos.sol_invested
        self.assertAlmostEqual(size, 0.03)
        # 0.006 fixed (priority fee + Jito tip), then 1.5% fees and 3% slippage
        expected_tokens = (size - 0.006) * 0.985 * 0.97 / 1e-6
        self.assertAlmostEqual(pos.tokens / expected_tokens, 1.0, places=6)

        await bot._execute_sell(MINT, 100, "take_profit_full")
        self.assertNotIn(MINT, bot.risk.positions)
        expected_back = expected_tokens * 1e-6 * 0.97 * 0.985 - 0.006
        self.assertAlmostEqual(bot.wallet.sol_balance, 2.0 - size + expected_back,
                               places=6)
        # The old model returned 97% of the stake here; fixed fees alone are 40%.
        self.assertLess(expected_back / size, 0.6)

    async def test_fee_cap_shows_up_in_paper_costs(self):
        self.bot.cfg.max_fee_pct_of_trade = 5.0
        fixed, pct = self.bot._paper_costs(0.03, jupiter=False)
        self.assertAlmostEqual(fixed, 0.0015 + 0.001)
        self.assertEqual(pct, 1.5)
        fixed, _ = self.bot._paper_costs(0.04, jupiter=True)
        self.assertAlmostEqual(fixed, self.bot.cfg.paper_jupiter_fee_sol + 0.001)


if __name__ == "__main__":
    unittest.main()


# ───────────────────────── TRADE FEED ─────────────────────────

class TradeFeed(unittest.IsolatedAsyncioTestCase):
    NOTICE = ("'subscribeTokenTrade' and 'subscribeAccountTrade' methods are only "
              "available when connecting with an API key funded with at least 0.02 SOL.")

    def test_stream_connects_with_the_api_key_when_there_is_one(self):
        from pumpportal import PumpPortalStream
        self.assertTrue(PumpPortalStream(api_key="SECRET")._url().endswith("?api-key=SECRET"))
        self.assertNotIn("api-key", PumpPortalStream()._url())

    def test_keyed_connection_is_opt_in_because_it_is_billed(self):
        from bot import MemeBot
        for flag, expect_key in ((False, False), (True, True)):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            bot = MemeBot(make_config(pumpportal_api_key="SECRET",
                                      pumpportal_trade_feed=flag),
                          db_path=os.path.join(tmp.name, "t.db"))
            self.assertEqual("api-key=SECRET" in bot.stream._url(), expect_key)

    async def test_refused_keyed_connection_falls_back_to_the_free_feed(self):
        from pumpportal import PumpPortalStream
        stream = PumpPortalStream(api_key="SECRET")
        urls = []

        class Stop(BaseException):
            pass

        def connect(url):
            urls.append(url)
            if len(urls) >= 4:
                raise Stop()
            raise OSError("HTTP 401")
        with mock.patch("pumpportal.websockets.connect", connect), \
                mock.patch("asyncio.sleep", helpers.fast_sleep):
            with self.assertRaises(Stop):
                await stream.connect()
        self.assertIn("api-key", urls[0])
        self.assertIn("api-key", urls[1])
        self.assertNotIn("api-key", urls[2])          # gave up on the key
        self.assertTrue(stream.trade_feed_rejected)

    async def test_refused_trade_subscriptions_are_reported_not_swallowed(self):
        import logging
        from pumpportal import PumpPortalStream
        stream = PumpPortalStream(api_key="SECRET")
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs("pumpportal", level="WARNING") as logs:
            await stream._dispatch({"message": self.NOTICE})
            await stream._dispatch({"message": self.NOTICE})      # warned once
        self.assertTrue(stream.trade_feed_rejected)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("SECRET", stream._redact("wss://x?api-key=SECRET"))

    async def test_ordinary_notice_and_trades_do_not_flag_a_refusal(self):
        from pumpportal import PumpPortalStream
        stream = PumpPortalStream()
        got = []
        stream.on_token_trade(got.append)
        await stream._dispatch({"message": "Successfully subscribed to token creation events."})
        await stream._dispatch({"traderPublicKey": "X", "txType": "buy", "mint": MINT})
        self.assertFalse(stream.trade_feed_rejected)
        self.assertEqual(stream.trade_events, 1)
        self.assertEqual(len(got), 1)


# ───────────────────────── PAPER SELL PRICING ─────────────────────────

class PaperSellPricing(Case):
    cfg = dict(strategy="momentum", priority_fee_sol=0.0, use_jito=False,
               paper_fee_pct=0.0, paper_slippage_pct=0.0, max_fee_pct_of_trade=0)

    def hold(self, tokens=1_000_000.0, sol=0.15, graduated=False):
        self.bot.wallet.buy(MINT, "TKN", sol, tokens, 0.00001)
        return self.bot.risk.open(MINT, "TKN", 0.00001, 1e-7, tokens, sol,
                                  "momentum", graduated=graduated)

    def curve(self, v_sol, real_sol, v_tokens=5e14, complete=False):
        self.bot.onchain.states[MINT] = BondingCurveState(
            virtual_sol=v_sol, real_sol=real_sol, virtual_tokens=v_tokens,
            real_tokens=3e14, complete=complete, pct=real_sol / 85 * 100)

    async def test_rug_exit_is_priced_from_the_drained_curve_not_a_stale_quote(self):
        # The GTN case: curve drained 34 -> 0.1 SOL, but market data still
        # showed the pre-rug price and the paper sell booked +43%.
        self.hold()
        self.bot.market.token = token(price_sol=2.3e-7)      # stale: worth 0.23 SOL
        self.curve(v_sol=30.1, real_sol=0.1)
        await self.bot._execute_sell(MINT, 100, "Bonding curve dropped 100%")
        self.assertNotIn(MINT, self.bot.risk.positions)
        got = self.bot.wallet.sol_balance - (2.0 - 0.15)
        self.assertLess(got, 0.07)                           # a loss, not +43%
        self.assertGreater(got, 0)

    async def test_curve_sale_cannot_return_more_sol_than_the_curve_holds(self):
        pos = self.hold(tokens=400_000_000.0)                # a huge position
        self.curve(v_sol=31.0, real_sol=1.0)
        sol, _, source = await self.bot._paper_sell_quote(MINT, pos, pos.tokens)
        self.assertEqual(source, "bonding curve")
        self.assertLessEqual(sol, 1.0)

    async def test_curve_quote_matches_constant_product_math(self):
        pos = self.hold(tokens=1_000_000.0)
        self.curve(v_sol=60.0, real_sol=30.0, v_tokens=5e14)
        sol, price_usd, _ = await self.bot._paper_sell_quote(MINT, pos, pos.tokens)
        self.assertAlmostEqual(sol, 60.0 * 1e12 / (5e14 + 1e12), places=9)
        self.assertAlmostEqual(price_usd, 60.0 / (5e14 / 1e6) * 100.0, places=12)

    async def test_graduated_token_uses_market_data(self):
        pos = self.hold(graduated=True)
        self.bot.market.token = token(price_sol=2e-7)
        self.curve(v_sol=115.0, real_sol=0.0, complete=True)
        sol, _, source = await self.bot._paper_sell_quote(MINT, pos, pos.tokens)
        self.assertEqual(source, "market data")
        self.assertAlmostEqual(sol, 0.2)

    async def test_unreadable_curve_falls_back_to_market_data(self):
        pos = self.hold()
        self.bot.market.token = token(price_sol=2e-7)
        sol, _, source = await self.bot._paper_sell_quote(MINT, pos, pos.tokens)
        self.assertEqual(source, "market data")


class GraduationExpiry(unittest.IsolatedAsyncioTestCase):
    async def test_expired_candidate_is_not_detected_again(self):
        # The SINERS case: an unbought graduate expired after 5 minutes and
        # was then "detected" as graduating over and over.
        oc = Onchain()
        grad = GraduationStrategy(None, min_curve_pct=90, onchain=oc)
        oc.pct[MINT] = 95.0
        await grad.evaluate_trade({"mint": MINT})
        oc.pct[MINT] = 100.0
        await grad.evaluate_trade({"mint": MINT})
        self.assertIn(MINT, grad._graduated_at)
        grad._graduated_at[MINT] = time.time() - 1000        # past max_wait
        self.assertEqual(grad.get_post_graduation_candidates(), [])
        await grad.evaluate_trade({"mint": MINT})
        self.assertNotIn(MINT, grad._graduated_at)
