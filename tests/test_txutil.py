import unittest
from unittest import mock

import helpers
from helpers import FakeHttp, FakeResp, MINT, OWNER, OTHER, make_tx, rpc_method
import txutil


def status_http(value=None, raise_exc=False, error=False):
    def handler(method, url, kw):
        if raise_exc:
            raise ConnectionError("rpc down")
        if error:
            return FakeResp(200, {"error": {"code": -32429}})
        return FakeResp(200, {"result": {"value": [value]}})
    return FakeHttp(handler)


class ParseTxOutcome(unittest.TestCase):
    def test_owner_is_looked_up_not_assumed_at_index_zero(self):
        tx = make_tx(sol_delta=-0.031, token_post=1234.5, owner_index=2)
        out = txutil.parse_tx_outcome(tx, OWNER, MINT)
        self.assertTrue(out.owner_found)
        self.assertAlmostEqual(out.sol_delta, -0.031)
        self.assertAlmostEqual(out.token_delta, 1234.5)
        self.assertAlmostEqual(out.token_post, 1234.5)
        self.assertIsNone(out.err)

    def test_sell_reports_sol_gain_and_remaining_tokens(self):
        tx = make_tx(sol_delta=0.045, token_pre=1000, token_post=500)
        out = txutil.parse_tx_outcome(tx, OWNER, MINT)
        self.assertAlmostEqual(out.sol_delta, 0.045)
        self.assertAlmostEqual(out.token_delta, -500)
        self.assertAlmostEqual(out.token_post, 500)

    def test_foreign_wallet_is_reported_as_not_found(self):
        tx = make_tx(owner=OTHER, sol_delta=-0.03, token_post=10)
        out = txutil.parse_tx_outcome(tx, OWNER, MINT)
        self.assertFalse(out.owner_found)
        self.assertEqual(out.token_post, 0)

    def test_onchain_error_is_surfaced(self):
        tx = make_tx(err={"InstructionError": [2, "Custom"]})
        self.assertIsNotNone(txutil.parse_tx_outcome(tx, OWNER, MINT).err)

    def test_missing_tx_gives_none(self):
        self.assertIsNone(txutil.parse_tx_outcome(None, OWNER, MINT))


class ConfirmSignature(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed(self):
        http = status_http({"confirmationStatus": "confirmed", "err": None})
        self.assertEqual(await txutil.confirm_signature(http, ["u"], "sig", timeout=1),
                         txutil.CONFIRMED)

    async def test_onchain_error_is_failed_not_confirmed(self):
        http = status_http({"confirmationStatus": "confirmed",
                            "err": {"InstructionError": [0, "Custom"]}})
        self.assertEqual(await txutil.confirm_signature(http, ["u"], "sig", timeout=1),
                         txutil.FAILED)

    async def test_never_seen_is_pending(self):
        with mock.patch("asyncio.sleep", helpers.fast_sleep):
            got = await txutil.confirm_signature(status_http(None), ["u"], "sig", timeout=0.05)
        self.assertEqual(got, txutil.PENDING)

    async def test_rpc_unreachable_is_unknown_not_pending(self):
        with mock.patch("asyncio.sleep", helpers.fast_sleep):
            got = await txutil.confirm_signature(
                status_http(raise_exc=True), ["u", "v"], "sig", timeout=0.05)
        self.assertEqual(got, txutil.UNKNOWN)

    async def test_rpc_that_answers_then_goes_down_is_unknown_not_expired(self):
        # An early "not seen" says nothing about what happened afterwards.
        # Reporting PENDING here would make a buy look expired and orphan
        # tokens that landed during the outage.
        calls = {"n": 0}

        def handler(method, url, kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResp(200, {"result": {"value": [None]}})
            raise ConnectionError("rpc down")
        with mock.patch("asyncio.sleep", helpers.fast_sleep):
            got = await txutil.confirm_signature(FakeHttp(handler), ["u"], "sig", timeout=0.05)
        self.assertEqual(got, txutil.UNKNOWN)

    async def test_rpc_error_response_is_unknown(self):
        with mock.patch("asyncio.sleep", helpers.fast_sleep):
            got = await txutil.confirm_signature(
                status_http(error=True), ["u"], "sig", timeout=0.05)
        self.assertEqual(got, txutil.UNKNOWN)

    async def test_processed_only_is_not_confirmed(self):
        with mock.patch("asyncio.sleep", helpers.fast_sleep):
            got = await txutil.confirm_signature(
                status_http({"confirmationStatus": "processed", "err": None}),
                ["u"], "sig", timeout=0.05)
        self.assertEqual(got, txutil.PENDING)


if __name__ == "__main__":
    unittest.main()
