"""Buy/sell lifecycle tests against a live-mode bot with every external
dependency faked. Each test reproduces one failure scenario from the review."""

import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

import helpers
from helpers import (MINT, OWNER, OTHER, make_live_bot, make_tx, open_position,
                     make_config)
from market_data import TokenData
from onchain import OnChainResult
from strategies import Signal
from txutil import TradeResult

MINT2 = "MintBBBB2222222222222222222222222222222222pump"


def confirmed(sig, lightning=False):
    return TradeResult("confirmed", sig, lightning, [sig])


def unknown(sig=""):
    return TradeResult("unknown", sig, False, [sig] if sig else [])


def failed():
    return TradeResult("failed")


def token(price_usd, price_sol=None):
    return TokenData(
        mint=MINT, symbol="TKN", name="Token", price_usd=price_usd,
        price_sol=price_sol if price_sol is not None else price_usd / 100,
        liquidity_usd=50_000, volume_24h=100_000, volume_5m=1000,
        price_change_5m=0, price_change_1h=0, price_change_24h=0,
        market_cap=100_000, fdv=100_000, pair_address="P",
        pair_created_at=int((time.time() - 3600) * 1000), buys_5m=10, sells_5m=10)


def signal(mint=MINT, strategy="sniper"):
    return Signal(mint=mint, symbol="TKN", strategy=strategy, confidence=0.8,
                  reason="test", creator="Creator1111")


class BotCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch("asyncio.sleep", helpers.fast_sleep)
        p.start()
        self.addCleanup(p.stop)
        self.bot = make_live_bot(self.tmp.name)

    async def drain(self):
        while self.bot._bg_tasks:
            await asyncio.gather(*list(self.bot._bg_tasks), return_exceptions=True)

    def db_mints(self):
        return [r["mint"] for r in self.bot.db.get_all_positions()]


# ───────────────────────── SELL ─────────────────────────

class SellVerification(BotCase):
    async def test_failed_sell_never_closes_the_position(self):
        # Finding 1: a sell that did not execute used to close the position
        # with 0 SOL received and book a -100% loss.
        bot = self.bot
        open_position(bot)
        bot.pump.sell_results = [failed()]
        await bot._execute_sell(MINT, 100, "stop_loss")

        self.assertIn(MINT, bot.risk.positions)
        self.assertIn(MINT, self.db_mints())
        self.assertEqual(bot.risk.daily_pnl, 0.0)
        self.assertNotIn(MINT, bot._closed_mints)
        self.assertEqual(bot._stuck_positions[MINT], 1)

    async def test_unconfirmed_sell_with_unchanged_balance_keeps_position(self):
        bot = self.bot
        open_position(bot)
        bot.pump.sell_results = [unknown("S1")]
        await bot._execute_sell(MINT, 100, "stop_loss")
        self.assertIn(MINT, bot.risk.positions)
        self.assertEqual(bot.risk.daily_pnl, 0.0)

    async def test_unverifiable_sell_keeps_position_and_stops_trying_venues(self):
        bot = self.bot
        open_position(bot)
        bot.wallet.tokens[MINT] = None          # RPC cannot read the balance
        bot.pump.sell_results = [unknown("S1")]
        await bot._execute_sell(MINT, 100, "rug: creator sold")
        self.assertIn(MINT, bot.risk.positions)
        self.assertEqual(len(bot.pump.sell_calls), 1)
        self.assertEqual(bot._stuck_positions[MINT], 1)
        self.assertEqual(bot.risk.daily_pnl, 0.0)

    async def test_confirmed_sell_books_proceeds_from_the_transaction(self):
        # Finding 14: proceeds used to be the wallet-wide SOL change, which a
        # concurrent trade on another mint turns into 0 (a full loss).
        bot = self.bot
        open_position(bot, sol=0.03)
        bot.pump.sell_results = [confirmed("S1")]
        bot.pump.txs["S1"] = make_tx(sol_delta=0.05, token_pre=1000, token_post=0)
        # wallet SOL does not move at all — as if another buy consumed it
        await bot._execute_sell(MINT, 100, "take_profit_full")

        self.assertNotIn(MINT, bot.risk.positions)
        self.assertEqual(self.db_mints(), [])
        self.assertAlmostEqual(bot.risk.daily_pnl, 0.02, places=6)
        self.assertIn(MINT, bot._closed_mints)

    async def test_sell_that_landed_despite_unknown_status_is_recorded(self):
        bot = self.bot
        open_position(bot, sol=0.03)
        bot.wallet.script[MINT] = [1000.0, 0.0]     # before, then after
        bot.pump.sell_results = [unknown("S9")]
        bot.pump.txs["S9"] = make_tx(sol_delta=0.04, token_pre=1000, token_post=0)
        await bot._execute_sell(MINT, 100, "stop_loss")
        self.assertNotIn(MINT, bot.risk.positions)
        self.assertAlmostEqual(bot.risk.daily_pnl, 0.01, places=6)

    async def test_tokens_already_gone_credits_the_earlier_unconfirmed_sell(self):
        # The sell landed late; the retry finds 0 tokens. That must not be
        # booked as a -100% loss.
        bot = self.bot
        open_position(bot, sol=0.03, age=120)
        bot.wallet.tokens[MINT] = 0.0
        bot._sell_sigs[MINT] = ["S5"]
        bot.pump.txs["S5"] = make_tx(sol_delta=0.045, token_pre=1000, token_post=0)
        await bot._execute_sell(MINT, 100, "stuck_retry")
        self.assertNotIn(MINT, bot.risk.positions)
        self.assertEqual(len(bot.pump.sell_calls), 0)
        self.assertAlmostEqual(bot.risk.daily_pnl, 0.015, places=6)

    async def test_zero_balance_right_after_a_buy_is_not_trusted(self):
        bot = self.bot
        open_position(bot, age=5)
        bot.wallet.tokens[MINT] = 0.0           # RPC lag just after the buy
        await bot._execute_sell(MINT, 100, "fast_stop_loss")
        self.assertIn(MINT, bot.risk.positions)
        self.assertEqual(bot.risk.daily_pnl, 0.0)

    async def test_zero_balance_must_be_seen_twice(self):
        bot = self.bot
        open_position(bot, age=120)
        bot.wallet.script[MINT] = [0.0, 1000.0]  # one bad read, then the truth
        await bot._execute_sell(MINT, 100, "stop_loss")
        self.assertIn(MINT, bot.risk.positions)


class SellBackoff(BotCase):
    async def test_unsellable_token_is_not_retried_in_a_tight_loop(self):
        # Without force-close, every trigger (fast check every 0.5s, poll
        # loop, WebSocket) would otherwise re-send the sell immediately, and
        # each failed attempt can still cost a priority fee.
        bot = self.bot
        open_position(bot)
        bot.pump.sell_results = [failed()]
        for _ in range(25):
            bot._spawn_sell(MINT, 100, "fast_stop_loss")
            await self.drain()
        # One attempt = the normal route + the last-resort route.
        self.assertEqual(len(bot.pump.sell_calls), 2)
        self.assertIn(MINT, bot.risk.positions)

    async def test_emergency_exit_gets_one_try_after_a_routine_sell_failed(self):
        bot = self.bot
        open_position(bot)
        bot.pump.sell_results = [failed()]
        bot._spawn_sell(MINT, 100, "take_profit_full")
        await self.drain()
        self.assertEqual(len(bot.pump.sell_calls), 2)
        bot._spawn_sell(MINT, 100, "stale_exit")          # routine: held back
        await self.drain()
        self.assertEqual(len(bot.pump.sell_calls), 2)
        bot._spawn_sell(MINT, 100, "rug: creator sold")   # emergency: allowed once
        await self.drain()
        self.assertEqual(len(bot.pump.sell_calls), 4)
        bot._spawn_sell(MINT, 100, "fast_stop_loss")      # then held back too
        await self.drain()
        self.assertEqual(len(bot.pump.sell_calls), 4)

    async def test_backoff_expires_and_a_successful_sell_clears_it(self):
        bot = self.bot
        open_position(bot)
        bot.pump.sell_results = [failed()]
        bot._spawn_sell(MINT, 100, "stop_loss")
        await self.drain()
        bot._sell_backoff[MINT] = (time.time() - 1, True)   # pause elapsed
        bot.pump.sell_results = [confirmed("S1")]
        bot.pump.txs["S1"] = make_tx(sol_delta=0.05, token_pre=1000, token_post=0)
        bot._spawn_sell(MINT, 100, "stop_loss")
        await self.drain()
        self.assertNotIn(MINT, bot.risk.positions)
        self.assertNotIn(MINT, bot._sell_backoff)
        self.assertNotIn(MINT, bot._stuck_positions)

    async def test_stop_loss_with_failing_sells_stays_bounded_in_the_fast_loop(self):
        bot = self.bot
        pos = open_position(bot, age=600, price_usd=1.0)
        pos.stop_loss_first_seen = time.time() - 20      # already confirmed
        bot.market.token = token(0.3)                    # -70%
        bot.pump.sell_results = [failed()]
        bot.running = True
        n = {"i": 0}

        async def counting_sleep(_s=0, *a, **k):
            n["i"] += 1
            if n["i"] >= 60:
                bot.running = False
            await helpers._REAL_SLEEP(0)
        with mock.patch("asyncio.sleep", counting_sleep):
            await bot._fast_position_check()
            await self.drain()
        self.assertEqual(len(bot.pump.sell_calls), 2)


class PartialSells(BotCase):
    async def test_partial_sell_on_the_last_resort_path_is_recorded(self):
        # Finding 7: TP1 via the retry path never set partial_taken, so TP1
        # fired again on the next tick.
        bot = self.bot
        pos = open_position(bot, sol=0.04)
        bot.pump.sell_results = [failed(), confirmed("S2")]
        bot.pump.txs["S2"] = make_tx(sol_delta=0.02, token_pre=1000, token_post=500)
        await bot._execute_sell(MINT, 50, "take_profit_partial")

        self.assertIn(MINT, bot.risk.positions)
        self.assertTrue(pos.partial_taken)
        self.assertAlmostEqual(pos.partial_sol_received, 0.02)
        self.assertAlmostEqual(pos.tokens, 500)
        self.assertAlmostEqual(pos.sol_invested, 0.02)
        self.assertAlmostEqual(pos.original_sol_invested, 0.04)
        self.assertNotIn(MINT, bot._stuck_positions)

    async def test_unknown_partial_sell_is_not_repeated_while_it_may_still_land(self):
        bot = self.bot
        pos = open_position(bot, sol=0.04)
        bot.pump.sell_results = [unknown("S7")]
        await bot._execute_sell(MINT, 50, "take_profit_partial")
        self.assertEqual(len(bot.pump.sell_calls), 1)
        self.assertFalse(bot.pump.sell_calls[0][1]["resend_on_timeout"])
        self.assertIn(MINT, bot._partial_hold)
        self.assertNotIn(MINT, bot._stuck_positions)   # no escalation to a full exit

        await bot._execute_sell(MINT, 50, "take_profit_partial")
        self.assertEqual(len(bot.pump.sell_calls), 1, "second partial sent during hold")

        # The first one did land: once the hold expires it is recorded, and
        # no extra sell is sent.
        bot._partial_hold[MINT]["until"] = 0
        bot.wallet.tokens[MINT] = 500.0
        bot.pump.txs["S7"] = make_tx(sol_delta=0.02, token_pre=1000, token_post=500)
        await bot._execute_sell(MINT, 50, "take_profit_partial")
        self.assertEqual(len(bot.pump.sell_calls), 1)
        self.assertTrue(pos.partial_taken)
        self.assertAlmostEqual(pos.partial_sol_received, 0.02)
        self.assertAlmostEqual(pos.tokens, 500)

    async def test_full_exit_still_goes_through_during_a_partial_hold(self):
        bot = self.bot
        open_position(bot, sol=0.04)
        bot._partial_hold[MINT] = {"until": time.time() + 90, "bal_before": 1000.0,
                                   "sell_pct": 50, "reason": "tp"}
        bot.pump.sell_results = [confirmed("S3")]
        bot.pump.txs["S3"] = make_tx(sol_delta=0.05, token_pre=1000, token_post=0)
        await bot._execute_sell(MINT, 100, "rug: creator sold")
        self.assertNotIn(MINT, bot.risk.positions)


class JupiterSells(BotCase):
    async def test_jupiter_sell_amount_comes_from_the_token_account(self):
        # Finding 3: a failed decimals lookup returned 0 and the bot sold a
        # millionth of the position, then closed it.
        bot = self.bot
        pos = open_position(bot)
        pos.graduated = True
        bot.onchain.decimals = 0                 # would have been used before
        bot.jupiter.sell_result = confirmed("J1")
        bot.pump.txs["J1"] = make_tx(sol_delta=0.05, token_pre=1000, token_post=0)
        await bot._execute_sell(MINT, 100, "take_profit_full")
        self.assertEqual(bot.jupiter.sell_calls[0][1], 1_000_000_000)
        self.assertNotIn(MINT, bot.risk.positions)

    async def test_jupiter_partial_sell_amount(self):
        bot = self.bot
        pos = open_position(bot)
        pos.graduated = True
        bot.jupiter.sell_result = confirmed("J2")
        bot.pump.txs["J2"] = make_tx(sol_delta=0.02, token_pre=1000, token_post=500)
        await bot._execute_sell(MINT, 50, "take_profit_graduated")
        self.assertEqual(bot.jupiter.sell_calls[0][1], 500_000_000)
        self.assertFalse(bot.jupiter.sell_calls[0][2]["resend_on_timeout"])

    async def test_unreadable_balance_never_reaches_jupiter(self):
        bot = self.bot
        pos = open_position(bot)
        pos.graduated = True
        bot.wallet.tokens[MINT] = None
        bot.pump.sell_results = [failed()]
        await bot._execute_sell(MINT, 100, "take_profit_full")
        self.assertEqual(bot.jupiter.sell_calls, [])
        self.assertIn(MINT, bot.risk.positions)


class Graduation(BotCase):
    async def test_rpc_failure_does_not_mark_a_position_graduated(self):
        # Finding 5: one 429 used to suspend the stop-loss for 5 minutes.
        bot = self.bot
        pos = open_position(bot)
        bot.onchain.graduated = None
        self.assertFalse(await bot._is_graduated(MINT, pos))
        self.assertFalse(pos.graduated)
        self.assertFalse(pos.in_graduation_grace)
        self.assertNotIn(MINT, bot._graduated_cache)

    async def test_real_graduation_is_recorded_and_persisted(self):
        bot = self.bot
        pos = open_position(bot)
        bot.onchain.graduated = True
        self.assertTrue(await bot._is_graduated(MINT, pos))
        self.assertTrue(pos.graduated)
        self.assertIn('"graduated": true', bot.db.get_position(MINT)["extra"])


# ───────────────────────── BUY ─────────────────────────

class Buys(BotCase):
    async def test_confirmed_buy_uses_the_transaction_for_tokens_and_cost(self):
        bot = self.bot
        bot.pump.buy_result = confirmed("B1")
        bot.pump.txs["B1"] = make_tx(sol_delta=-0.032, token_post=5000)
        await bot._execute_buy(signal())

        pos = bot.risk.positions[MINT]
        self.assertAlmostEqual(pos.tokens, 5000)
        self.assertAlmostEqual(pos.sol_invested, 0.032)
        self.assertGreater(pos.entry_price, 0)
        self.assertFalse(pos.buy_unverified)
        self.assertEqual(len(bot.pump.buy_calls), 1)
        self.assertNotIn("pending", bot.db.get_position(MINT)["extra"])

    async def test_failed_buy_opens_nothing_and_is_not_retried(self):
        # Finding 4: the bot used to call pump.buy a second time here.
        bot = self.bot
        bot.pump.buy_result = failed()
        await bot._execute_buy(signal())
        self.assertEqual(bot.risk.positions, {})
        self.assertEqual(self.db_mints(), [])
        self.assertEqual(len(bot.pump.buy_calls), 1)

    async def test_buy_tx_that_failed_onchain_opens_nothing(self):
        # Finding 2 scenario A: a phantom position with 0 tokens.
        bot = self.bot
        bot.pump.buy_result = unknown("B2")
        bot.pump.txs["B2"] = make_tx(err={"InstructionError": [2, "Custom"]})
        await bot._execute_buy(signal())
        self.assertEqual(bot.risk.positions, {})
        self.assertEqual(self.db_mints(), [])

    async def test_unknown_buy_with_no_tokens_opens_nothing(self):
        bot = self.bot
        bot.pump.buy_result = unknown()
        bot.wallet.tokens[MINT] = 0.0
        await bot._execute_buy(signal())
        self.assertEqual(bot.risk.positions, {})
        self.assertEqual(self.db_mints(), [])
        self.assertEqual(bot.risk.daily_pnl, 0.0)

    async def test_unknown_buy_that_did_land_is_tracked(self):
        bot = self.bot
        bot.pump.buy_result = unknown()
        bot.wallet.script[MINT] = [0.0, 4200.0]     # before the buy, then after
        await bot._execute_buy(signal())
        self.assertAlmostEqual(bot.risk.positions[MINT].tokens, 4200)

    async def test_unknown_buy_with_unreadable_wallet_is_tracked_as_unverified(self):
        bot = self.bot
        bot.pump.buy_result = unknown("B3")
        bot.wallet.tokens[MINT] = None
        await bot._execute_buy(signal())
        pos = bot.risk.positions[MINT]
        self.assertTrue(pos.buy_unverified)
        self.assertEqual(pos.tokens, 0)

        # Wallet readable again and the tokens are there: adopt them.
        bot.wallet.tokens[MINT] = 700.0
        await bot._reconcile_positions(force=True)
        self.assertAlmostEqual(pos.tokens, 700)
        self.assertFalse(pos.buy_unverified)

    async def test_unverified_buy_that_never_landed_is_dropped_without_a_loss(self):
        bot = self.bot
        bot.pump.buy_result = unknown("B4")
        bot.wallet.tokens[MINT] = None
        await bot._execute_buy(signal())
        pos = bot.risk.positions[MINT]
        pos.entry_time = time.time() - 300
        bot.wallet.tokens[MINT] = 0.0
        await bot._reconcile_positions(force=True)

        self.assertEqual(bot.risk.positions, {})
        self.assertEqual(self.db_mints(), [])
        self.assertEqual(bot.risk.daily_pnl, 0.0)
        self.assertEqual(bot.db.get_loss_streak("sniper"), 0)

    async def test_position_without_an_entry_price_gets_one_from_the_first_read(self):
        # Finding 2 scenario B: entry_price 0 meant no stop-loss at all.
        bot = self.bot
        bot.pump.buy_result = confirmed("B5")       # no tx body available
        bot.wallet.script[MINT] = [0.0, 3000.0]
        bot.market.sol_price = 0                     # no price source at buy time
        await bot._execute_buy(signal())
        pos = bot.risk.positions[MINT]
        self.assertEqual(pos.entry_price, 0)
        self.assertAlmostEqual(pos.tokens, 3000)

        bot.market.sol_price = 100.0
        bot.market.token = token(0.002)
        await bot._check_positions()
        self.assertAlmostEqual(pos.entry_price, 0.002)
        self.assertAlmostEqual(pos.pnl_pct(0.001), -50.0)

    async def test_lightning_trade_from_another_wallet_disables_lightning(self):
        # Finding 9
        bot = self.bot
        bot.pump.buy_result = confirmed("B6", lightning=True)
        bot.pump.txs["B6"] = make_tx(owner=OTHER, sol_delta=-0.03, token_post=5000)
        await bot._execute_buy(signal())
        self.assertEqual(bot.risk.positions, {})
        self.assertEqual(bot.pump.api_key, "")
        self.assertTrue(any("CRITICAL" in m for m in bot.notify.sent))

    async def test_repeat_signals_for_a_mint_in_flight_are_dropped_not_queued(self):
        # Momentum re-signals the same mint every few seconds. Queued behind
        # the lock, those would fire one after another once the first failed.
        bot = self.bot
        bot.pump.buy_result = failed()
        real_buy = bot.pump.buy

        async def slow_buy(mint, sol, **kw):
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return await real_buy(mint, sol, **kw)
        bot.pump.buy = slow_buy
        await asyncio.gather(*[bot._execute_buy(signal()) for _ in range(4)])
        self.assertEqual(len(bot.pump.buy_calls), 1)
        self.assertEqual(bot.risk.positions, {})

    async def test_concurrent_buys_cannot_overshoot_the_position_cap(self):
        bot = self.bot
        bot.cfg.max_concurrent = 1
        bot.pump.buy_result = confirmed("B7")
        bot.wallet.script[MINT] = [0.0, 100.0]
        bot.wallet.script[MINT2] = [0.0, 100.0]
        real_buy = bot.pump.buy

        async def slow_buy(mint, sol, **kw):
            await asyncio.sleep(0)      # let the other buy run its risk check
            return await real_buy(mint, sol, **kw)
        bot.pump.buy = slow_buy

        await asyncio.gather(bot._execute_buy(signal(MINT)),
                             bot._execute_buy(signal(MINT2)))
        self.assertEqual(len(bot.pump.buy_calls), 1)
        self.assertEqual(len(bot.risk.positions), 1)
        self.assertEqual(bot._reserved, {})


class SafetyGate(BotCase):
    async def test_unreadable_mint_blocks_the_buy_without_blacklisting(self):
        # Finding 15: this used to look like "freeze authority active" and
        # blacklist the token for 7 days.
        bot = self.bot
        bot.onchain.result = OnChainResult()        # every read failed
        self.assertFalse(await bot._safety_gate(signal()))
        self.assertFalse(bot.db.is_blacklisted(MINT))

    async def test_unreadable_holders_block_the_buy(self):
        bot = self.bot
        bot.onchain.result = OnChainResult(
            freeze_authority_revoked=True, mint_read_ok=True,
            holders_read_ok=False, creator_read_ok=True)
        self.assertFalse(await bot._safety_gate(signal()))
        self.assertFalse(bot.db.is_blacklisted(MINT))

    async def test_fail_open_setting_restores_old_behaviour(self):
        bot = self.bot
        bot.cfg.safety_fail_closed = False
        bot.onchain.result = OnChainResult(
            freeze_authority_revoked=True, mint_read_ok=True,
            holders_read_ok=False, creator_read_ok=False)
        self.assertTrue(await bot._safety_gate(signal(strategy="momentum")))

    async def test_real_freeze_authority_is_still_blacklisted(self):
        bot = self.bot
        bot.onchain.result = OnChainResult(
            freeze_authority_revoked=False, mint_read_ok=True,
            holders_read_ok=True, creator_read_ok=True)
        self.assertFalse(await bot._safety_gate(signal()))
        self.assertTrue(bot.db.is_blacklisted(MINT))

    async def test_clean_token_passes(self):
        self.assertTrue(await self.bot._safety_gate(signal()))

    async def test_layered_check_reports_unverified(self):
        from safety import SafetyChecker
        bot = self.bot
        bot.onchain.result = OnChainResult()
        checker = SafetyChecker(bot.cfg, onchain=bot.onchain)
        result = await checker.check(token(0.001))
        self.assertFalse(result.passed)
        self.assertTrue(result.unverified)


# ───────────────────────── LOOPS ─────────────────────────

class Loops(BotCase):
    async def run_loop(self, coro_fn, ticks):
        bot = self.bot
        bot.running = True
        bot.cfg.poll_interval = 0
        count = {"n": 0}

        async def counting_sleep(_s=0, *a, **k):
            count["n"] += 1
            if count["n"] >= ticks:
                bot.running = False
            await helpers._REAL_SLEEP(0)

        with mock.patch("asyncio.sleep", counting_sleep):
            await coro_fn()
            await self.drain()

    async def test_stuck_position_is_never_force_closed(self):
        # Finding 6: after 10 failed sells the position was written off at
        # 0 SOL while the tokens were still in the wallet.
        bot = self.bot
        open_position(bot, age=30)
        bot._stuck_positions[MINT] = 12
        bot._sell_backoff[MINT] = (time.time() + 300, False)   # as _mark_stuck leaves it
        bot.pump.sell_results = [failed()]
        await self.run_loop(bot._poll_loop, ticks=12)

        self.assertIn(MINT, bot.risk.positions)
        self.assertIn(MINT, self.db_mints())
        self.assertEqual(bot.risk.daily_pnl, 0.0)
        self.assertTrue(any("UNSOLD" in m for m in bot.notify.sent))
        self.assertEqual(len(bot.pump.sell_calls), 0)   # backed off, not hammering

    async def test_stuck_position_is_retried_before_the_backoff(self):
        bot = self.bot
        open_position(bot, age=30)
        bot._stuck_positions[MINT] = 2
        bot.pump.sell_results = [confirmed("S1")]
        bot.pump.txs["S1"] = make_tx(sol_delta=0.05, token_pre=1000, token_post=0)
        await self.run_loop(bot._poll_loop, ticks=12)
        self.assertNotIn(MINT, bot.risk.positions)
        self.assertNotIn(MINT, bot._stuck_positions)

    async def test_catastrophic_drop_in_grace_exits_after_one_8s_confirmation(self):
        # Finding 10: the new timer fed into a second 8s timer (~16s total).
        bot = self.bot
        pos = open_position(bot, age=20, price_usd=1.0)
        bot.market.token = token(0.4)               # -60%
        pos.catastrophic_first_seen = time.time() - 9
        calls = []

        def record(mint, pct, reason, followup=False):
            calls.append((mint, pct, reason))
            bot.running = False
        bot._spawn_sell = record
        await self.run_loop(bot._fast_position_check, ticks=5)

        self.assertEqual(calls, [(MINT, 100, "fast_stop_loss_catastrophic")])
        self.assertEqual(pos.stop_loss_first_seen, 0.0)

    async def test_catastrophic_drop_is_not_acted_on_before_8s(self):
        bot = self.bot
        pos = open_position(bot, age=20, price_usd=1.0)
        bot.market.token = token(0.4)
        calls = []
        bot._spawn_sell = lambda *a, **k: calls.append(a)
        await self.run_loop(bot._fast_position_check, ticks=4)
        self.assertEqual(calls, [])
        self.assertGreater(pos.catastrophic_first_seen, 0)

    async def test_ws_reconnect_audit_keeps_position_when_balance_unreadable(self):
        bot = self.bot
        open_position(bot, age=300)
        bot.wallet.tokens[MINT] = None
        await bot._on_ws_reconnect()
        await self.drain()
        self.assertIn(MINT, bot.risk.positions)


class Startup(unittest.TestCase):
    def test_db_path_is_passed_through(self):
        with tempfile.TemporaryDirectory() as d:
            bot = make_live_bot(d)
            self.assertEqual(bot.db.path, os.path.join(d, "test.db"))

    def test_paper_run_refuses_a_database_holding_live_positions(self):
        # Finding 13: a paper run used to recover live positions, paper-sell
        # them and delete their rows.
        from bot import MemeBot
        from db import Database
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "live.db")
            db = Database(path)
            db.set_state("trading_mode", "live")
            db.save_position(mint=MINT, symbol="TKN", entry_price=0.001,
                             entry_sol_price=0.00001, tokens=1000, sol_invested=0.03,
                             entry_time=time.time(), strategy="sniper")
            db.close()
            with self.assertRaises(RuntimeError):
                MemeBot(make_config(paper_trading=True), db_path=path)
            db = Database(path)
            self.assertEqual(len(db.get_all_positions()), 1)   # untouched
            db.close()

    def test_same_mode_restart_recovers_positions(self):
        from bot import MemeBot
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "paper.db")
            bot = MemeBot(make_config(paper_trading=True), db_path=path)
            bot.risk.open(MINT, "TKN", 0.001, 0.00001, 1000, 0.03, "sniper")
            bot.db.close()
            again = MemeBot(make_config(paper_trading=True), db_path=path)
            self.assertIn(MINT, again.risk.positions)


if __name__ == "__main__":
    unittest.main()
