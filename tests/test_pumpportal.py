import unittest
from unittest import mock

import httpx

import helpers
from helpers import FakeHttp, FakeResp, MINT, rpc_method
from pumpportal import PumpPortal, _Sent
import txutil


def make_pp(handler, api_key="secret-api-key", keypair=object()):
    pp = PumpPortal("http://rpc.invalid", keypair=keypair, api_key=api_key)
    pp.http = FakeHttp(handler)
    pp.BUY_MAX_WAIT = 0.05
    pp.SELL_CONFIRM_TIMEOUT = 0.05
    pp.RETRY_DELAY = 0
    return pp


def handler_for(status_value=None, lightning=None, rpc_down=False):
    """Lightning endpoint + getSignatureStatuses."""
    counter = {"n": 0}

    def handler(method, url, kw):
        if "pumpportal.fun/api/trade" in url:
            counter["n"] += 1
            if callable(lightning):
                return lightning(counter["n"])
            return FakeResp(200, {"signature": f"LSIG{counter['n']}"})
        if rpc_down:
            raise ConnectionError("rpc down")
        if rpc_method(kw) == "getSignatureStatuses":
            return FakeResp(200, {"result": {"value": [status_value]}})
        return FakeResp(200, {"result": None})
    handler.counter = counter
    return handler


class BuyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        p = mock.patch("asyncio.sleep", helpers.fast_sleep)
        p.start()
        self.addCleanup(p.stop)

    async def test_lightning_buy_that_fails_onchain_is_not_reported_as_success(self):
        # Finding 2: before the fix, any Lightning signature was returned as
        # a success without looking at the chain.
        h = handler_for({"confirmationStatus": "confirmed",
                         "err": {"InstructionError": [3, {"Custom": 6002}]}})
        pp = make_pp(h, keypair=None)
        result = await pp.buy(MINT, 0.03)
        self.assertTrue(result.failed)
        self.assertFalse(result.confirmed)

    async def test_lightning_buy_is_confirmed_before_success(self):
        h = handler_for({"confirmationStatus": "confirmed", "err": None})
        pp = make_pp(h, keypair=None)
        result = await pp.buy(MINT, 0.03)
        self.assertTrue(result.confirmed)
        self.assertTrue(result.via_lightning)
        self.assertEqual(result.sig, "LSIG1")
        self.assertEqual(h.counter["n"], 1)

    async def test_buy_timeout_never_sends_a_second_transaction(self):
        # Finding 4: a slow tx used to be followed by up to 5 more buys.
        h = handler_for(None)          # signature never appears
        pp = make_pp(h, keypair=None)
        result = await pp.buy(MINT, 0.03)
        self.assertEqual(h.counter["n"], 1, "buy was re-sent after a timeout")
        self.assertTrue(result.failed)     # expired past its blockhash

    async def test_buy_with_rpc_unreachable_is_unknown_and_sent_once(self):
        h = handler_for(None, rpc_down=True)
        pp = make_pp(h, keypair=None)
        result = await pp.buy(MINT, 0.03)
        self.assertEqual(h.counter["n"], 1)
        self.assertTrue(result.unknown)
        self.assertEqual(result.sig, "LSIG1")

    async def test_buy_retries_after_a_definite_onchain_failure(self):
        h = handler_for({"confirmationStatus": "confirmed", "err": {"x": 1}})
        pp = make_pp(h, keypair=None)
        await pp.buy(MINT, 0.03)
        self.assertEqual(h.counter["n"], pp.MAX_RETRIES)

    async def test_lightning_timeout_does_not_fall_back_to_a_local_buy(self):
        # The Lightning request may have been placed; a local send would
        # buy a second time.
        def lightning(n):
            raise httpx.ReadTimeout("timed out")
        pp = make_pp(handler_for(None, lightning=lightning))
        pp._local_trade = mock.AsyncMock(return_value=_Sent(sig="LOCAL"))
        result = await pp.buy(MINT, 0.03)
        pp._local_trade.assert_not_called()
        self.assertTrue(result.unknown)
        self.assertEqual(result.sig, "")

    async def test_lightning_connect_error_does_fall_back_to_local(self):
        # Nothing reached the server, so a local send cannot duplicate it.
        def lightning(n):
            raise httpx.ConnectError("no route")
        h = handler_for({"confirmationStatus": "confirmed", "err": None},
                        lightning=lightning)
        pp = make_pp(h)
        pp._local_trade = mock.AsyncMock(return_value=_Sent(sig="LOCAL"))
        result = await pp.buy(MINT, 0.03)
        pp._local_trade.assert_called_once()
        self.assertTrue(result.confirmed)
        self.assertEqual(result.sig, "LOCAL")

    async def test_lightning_200_without_signature_or_error_is_not_followed_by_a_local_buy(self):
        h = handler_for(None, lightning=lambda n: FakeResp(200, {}))
        pp = make_pp(h)
        pp._local_trade = mock.AsyncMock(return_value=_Sent(sig="LOCAL"))
        result = await pp.buy(MINT, 0.03)
        pp._local_trade.assert_not_called()
        self.assertTrue(result.unknown)

    async def test_lightning_200_with_explicit_errors_falls_back_to_local(self):
        h = handler_for({"confirmationStatus": "confirmed", "err": None},
                        lightning=lambda n: FakeResp(200, {"errors": ["insufficient funds"]}))
        pp = make_pp(h)
        pp._local_trade = mock.AsyncMock(return_value=_Sent(sig="LOCAL"))
        result = await pp.buy(MINT, 0.03)
        self.assertEqual(result.sig, "LOCAL")

    async def test_buy_expiry_seen_then_rpc_down_is_unknown(self):
        calls = {"n": 0}

        def handler(method, url, kw):
            if "pumpportal.fun/api/trade" in url:
                return FakeResp(200, {"signature": "LSIG1"})
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResp(200, {"result": {"value": [None]}})
            raise ConnectionError("rpc down")
        pp = make_pp(handler, keypair=None)
        result = await pp.buy(MINT, 0.03)
        self.assertTrue(result.unknown)

    async def test_lightning_rejection_falls_back_to_local(self):
        h = handler_for({"confirmationStatus": "confirmed", "err": None},
                        lightning=lambda n: FakeResp(400, {"errors": ["bad"]}))
        pp = make_pp(h)
        pp._local_trade = mock.AsyncMock(return_value=_Sent(sig="LOCAL"))
        result = await pp.buy(MINT, 0.03)
        self.assertEqual(result.sig, "LOCAL")


class SellTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        p = mock.patch("asyncio.sleep", helpers.fast_sleep)
        p.start()
        self.addCleanup(p.stop)

    async def test_lightning_sell_that_fails_onchain_is_not_reported_as_success(self):
        # Finding 1
        h = handler_for({"confirmationStatus": "confirmed", "err": {"x": 1}})
        pp = make_pp(h, keypair=None)
        result = await pp.sell(MINT, sell_pct=100)
        self.assertFalse(result.confirmed)
        self.assertTrue(result.failed)

    async def test_partial_sell_is_not_resent_after_a_timeout(self):
        h = handler_for(None)
        pp = make_pp(h, keypair=None)
        result = await pp.sell(MINT, sell_pct=50, resend_on_timeout=False)
        self.assertEqual(h.counter["n"], 1, "partial sell re-sent: would oversell")
        self.assertTrue(result.unknown)

    async def test_full_sell_is_resent_after_a_timeout(self):
        h = handler_for(None)
        pp = make_pp(h, keypair=None)
        result = await pp.sell(MINT, sell_pct=100, resend_on_timeout=True)
        self.assertEqual(h.counter["n"], pp.MAX_RETRIES)
        self.assertTrue(result.unknown)
        self.assertEqual(len(result.sigs), pp.MAX_RETRIES)

    async def test_confirmed_sell(self):
        h = handler_for({"confirmationStatus": "finalized", "err": None})
        pp = make_pp(h, keypair=None)
        result = await pp.sell(MINT, sell_pct=100)
        self.assertTrue(result.confirmed)


class RedactionTests(unittest.TestCase):
    def test_api_key_is_removed_from_logged_errors(self):
        pp = make_pp(handler_for(None))
        err = RuntimeError("POST https://pumpportal.fun/api/trade?api-key=secret-api-key failed")
        self.assertNotIn("secret-api-key", pp._redact(err))


if __name__ == "__main__":
    unittest.main()
