import os
import tempfile
import time
import unittest

import helpers
from helpers import MINT, make_config
from db import Database
from risk_manager import RiskManager


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "t.db")
        self.cfg = make_config()
        self.db = Database(self.path)
        self.risk = RiskManager(self.cfg, self.db)

    def reopen(self):
        self.db.close()
        self.db = Database(self.path)
        self.risk = RiskManager(self.cfg, self.db)
        self.risk.recover_positions()
        return self.risk


class DatabasePathTests(unittest.TestCase):
    def test_explicit_path_is_used(self):
        # Finding 13: the path used to be frozen at import time.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.db")
            db = Database(path)
            self.assertEqual(db.path, path)
            self.assertTrue(os.path.exists(path))
            db.close()

    def test_env_var_is_read_at_construction_not_import(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "env.db")
            os.environ["BOT_DB_PATH"] = path
            try:
                db = Database()
                self.assertEqual(db.path, path)
                db.close()
            finally:
                os.environ.pop("BOT_DB_PATH", None)


class PersistenceTests(Base):
    def test_graduation_and_tp2_survive_a_restart(self):
        # Finding 16
        pos = self.risk.open(MINT, "TKN", 0.001, 0.00001, 1000, 0.03, "momentum",
                             creator="C")
        self.risk.mark_graduated(MINT)
        pos.tp2_taken = True
        self.risk.persist(pos)
        self.risk.partial_close(MINT, sol_received=0.02, reason="tp1")

        back = self.reopen().positions[MINT]
        self.assertTrue(back.graduated)
        self.assertGreater(back.graduated_at, 0)
        self.assertTrue(back.tp2_taken)
        self.assertTrue(back.partial_taken)
        self.assertAlmostEqual(back.partial_sol_received, 0.02)
        self.assertEqual(back.creator, "C")

    def test_partial_close_does_not_erase_graduation(self):
        # The three writers of `extra` used to overwrite each other's keys.
        self.risk.open(MINT, "TKN", 0.001, 0.00001, 1000, 0.03, "graduation",
                       graduated=True)
        self.risk.partial_close(MINT, sol_received=0.01)
        self.assertTrue(self.reopen().positions[MINT].graduated)

    def test_crash_mid_buy_recovers_as_unverified(self):
        self.db.save_position(
            mint=MINT, symbol="TKN", entry_price=0, entry_sol_price=0, tokens=0,
            sol_invested=0.03, entry_time=time.time(), strategy="sniper",
            extra={"creator": "C", "pending": True})
        back = self.reopen().positions[MINT]
        self.assertTrue(back.buy_unverified)
        self.assertEqual(back.tokens, 0)

    def test_late_entry_price_is_persisted(self):
        self.risk.open(MINT, "TKN", 0, 0, 1000, 0.03, "sniper")
        self.assertEqual(self.risk.positions[MINT].pnl_pct(0.5), 0.0)  # no stop-loss yet
        self.risk.set_entry_price(MINT, 0.002, 0.00002)
        back = self.reopen().positions[MINT]
        self.assertAlmostEqual(back.entry_price, 0.002)
        self.assertAlmostEqual(back.pnl_pct(0.001), -50.0)

    def test_daily_trade_count_survives_a_restart(self):
        self.risk.open(MINT, "TKN", 0.001, 0.00001, 1000, 0.03, "sniper")
        self.risk.open(MINT + "2", "TK2", 0.001, 0.00001, 1000, 0.03, "sniper")
        self.assertEqual(self.reopen().daily_trades, 2)

    def test_recently_closed_mints_are_reloaded(self):
        self.risk.open(MINT, "TKN", 0.001, 0.00001, 1000, 0.03, "sniper")
        self.risk.close(MINT, 0.04, "tp")
        self.assertIn(MINT, self.db.get_recently_closed_mints())


class RiskRuleTests(Base):
    def test_discard_books_no_pnl_and_no_loss_streak(self):
        self.risk.open(MINT, "TKN", 0, 0, 0, 0.03, "sniper", buy_unverified=True)
        self.assertTrue(self.risk.discard(MINT, "buy never landed"))
        self.assertEqual(self.risk.daily_pnl, 0.0)
        self.assertEqual(self.db.get_loss_streak("sniper"), 0)
        self.assertEqual(self.db.get_all_positions(), [])
        self.assertEqual(self.risk.daily_trades, 0)

    def test_reservations_count_against_the_position_cap(self):
        self.cfg.max_concurrent = 2
        self.risk.open(MINT, "TKN", 0.001, 0.00001, 1000, 0.03, "sniper")
        self.assertTrue(self.risk.can_open(0.03, "sniper")[0])
        ok, why = self.risk.can_open(0.03, "sniper", reserved_sol=0.03, reserved_count=1)
        self.assertFalse(ok)
        self.assertIn("Max positions", why)

    def test_reservations_count_against_the_portfolio_cap(self):
        self.cfg.max_portfolio_sol = 0.1
        ok, why = self.risk.can_open(0.05, "sniper", reserved_sol=0.08, reserved_count=1)
        self.assertFalse(ok)
        self.assertIn("Portfolio", why)

    def test_noisy_ws_price_cannot_reset_the_stop_loss_timer(self):
        # Finding 10: one noisy trade used to restart the 8s confirmation.
        pos = self.risk.open(MINT, "TKN", 1.0, 0.01, 1000, 0.03, "sniper")
        pos.entry_time = time.time() - 600        # past the no-stop window
        self.risk.check_exits(MINT, 0.5)          # real price: -50%, starts timer
        started = pos.stop_loss_first_seen
        self.assertGreater(started, 0)
        self.risk.check_exits(MINT, 1.0, noisy=True)   # noisy "recovery"
        self.assertEqual(pos.stop_loss_first_seen, started)
        self.risk.check_exits(MINT, 1.0)               # a real recovery does reset
        self.assertEqual(pos.stop_loss_first_seen, 0.0)

    def test_noisy_ws_price_cannot_start_a_stop_loss(self):
        pos = self.risk.open(MINT, "TKN", 1.0, 0.01, 1000, 0.03, "sniper")
        pos.entry_time = time.time() - 600
        action, _ = self.risk.check_exits(MINT, 0.5, noisy=True)
        self.assertEqual(pos.stop_loss_first_seen, 0.0)
        self.assertNotEqual(action, "stop_loss")

    def test_config_rejects_non_positive_peaked_settings(self):
        cfg = make_config(peaked_growth_cap_pct=0, peaked_age_multiplier=-1)
        issues = " ".join(cfg.validate())
        self.assertIn("PEAKED_GROWTH_CAP_PCT", issues)
        self.assertIn("PEAKED_AGE_MULTIPLIER", issues)


if __name__ == "__main__":
    unittest.main()
