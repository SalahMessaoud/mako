import base64
import struct
import unittest

import helpers
from helpers import FakeHttp, FakeResp, rpc_method
from onchain import OnChainAnalyzer
from rug_detector import RugDetector

MINT = "So11111111111111111111111111111111111111112"   # any valid pubkey


def curve_account(complete: bool, real_sol=30.0):
    raw = bytearray(49)
    struct.pack_into("<Q", raw, 8, 10**15)
    struct.pack_into("<Q", raw, 16, int(60e9))
    struct.pack_into("<Q", raw, 24, 10**14)
    struct.pack_into("<Q", raw, 32, int(real_sol * 1e9))
    raw[48] = 1 if complete else 0
    return {"data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


def mint_account(decimals=6, freeze=False):
    raw = bytearray(82)
    struct.pack_into("<Q", raw, 36, 10**15)
    raw[44] = decimals
    struct.pack_into("<I", raw, 46, 1 if freeze else 0)
    return {"data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


def analyzer(handler):
    oc = OnChainAnalyzer("http://rpc.invalid", "http://fallback.invalid")
    oc.http = FakeHttp(handler)
    return oc


def rpc_down(method, url, kw):
    raise ConnectionError("rpc down")


def rate_limited(method, url, kw):
    return FakeResp(200, {"error": {"code": -32429, "message": "rate limited"}})


class GraduationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_failure_is_unknown_not_graduated(self):
        # Finding 5: this used to return True and switch off the stop-loss.
        self.assertIsNone(await analyzer(rpc_down).is_graduated(MINT))

    async def test_rate_limit_is_unknown_not_graduated(self):
        self.assertIsNone(await analyzer(rate_limited).is_graduated(MINT))

    async def test_complete_curve_is_graduated(self):
        oc = analyzer(lambda m, u, kw: FakeResp(200, {"result": {"value": curve_account(True)}}))
        self.assertIs(await oc.is_graduated(MINT), True)

    async def test_open_curve_is_not_graduated(self):
        oc = analyzer(lambda m, u, kw: FakeResp(200, {"result": {"value": curve_account(False)}}))
        self.assertIs(await oc.is_graduated(MINT), False)

    async def test_missing_curve_account_is_a_real_answer(self):
        oc = analyzer(lambda m, u, kw: FakeResp(200, {"result": {"value": None}}))
        self.assertIs(await oc.is_graduated(MINT), True)

    async def test_fallback_rpc_is_used_when_primary_fails(self):
        def handler(method, url, kw):
            if "fallback" not in url:
                raise ConnectionError("primary down")
            return FakeResp(200, {"result": {"value": curve_account(False)}})
        self.assertIs(await analyzer(handler).is_graduated(MINT), False)


class DecimalsAndSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_decimals_unknown_on_rpc_failure(self):
        # Finding 3: this used to return 0, scaling a sell to a millionth.
        self.assertIsNone(await analyzer(rpc_down).get_token_decimals(MINT))

    async def test_decimals_read_from_mint(self):
        oc = analyzer(lambda m, u, kw: FakeResp(200, {"result": {"value": mint_account(9)}}))
        self.assertEqual(await oc.get_token_decimals(MINT), 9)

    async def test_failed_analysis_is_flagged_unverified_and_not_cached(self):
        # Finding 15
        oc = analyzer(rpc_down)
        result = await oc.analyze(MINT, "Creator")
        self.assertFalse(result.mint_read_ok)
        self.assertFalse(result.holders_read_ok)
        self.assertFalse(result.creator_read_ok)
        self.assertFalse(result.verified)
        self.assertNotIn(MINT, oc._cache)

    async def test_successful_analysis_is_verified_and_cached(self):
        def handler(method, url, kw):
            m = rpc_method(kw)
            if m == "getAccountInfo":
                return FakeResp(200, {"result": {"value": mint_account(6)}})
            if m == "getTokenLargestAccounts":
                return FakeResp(200, {"result": {"value": [
                    {"address": "A", "uiAmount": 100.0},
                    {"address": "B", "uiAmount": 50.0}]}})
            if m == "getSignaturesForAddress":
                return FakeResp(200, {"result": [{"signature": "s"}]})
            return FakeResp(200, {"result": None})
        oc = analyzer(handler)
        result = await oc.analyze(MINT, "Creator")
        self.assertTrue(result.verified)
        self.assertTrue(result.freeze_authority_revoked)
        self.assertTrue(result.creator_read_ok)
        self.assertEqual(result.creator_token_count, 1)
        self.assertIn(MINT, oc._cache)

    async def test_active_freeze_authority_is_still_detected(self):
        def handler(method, url, kw):
            if rpc_method(kw) == "getAccountInfo":
                return FakeResp(200, {"result": {"value": mint_account(6, freeze=True)}})
            return FakeResp(200, {"result": {"value": []}})
        result = await analyzer(handler).analyze(MINT)
        self.assertTrue(result.mint_read_ok)
        self.assertFalse(result.freeze_authority_revoked)


class RugBaselineTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_baseline_read_does_not_disable_curve_drop_detection(self):
        class Positions(dict):
            pass

        class Oc:
            def __init__(self):
                self.sol = 40.0

            async def analyze(self, mint):
                from onchain import OnChainResult
                return OnChainResult(is_pump_token=True, bonding_curve_sol=self.sol)

            async def is_graduated(self, mint):
                return False

        oc = Oc()
        det = RugDetector(oc)
        det.start_monitoring("M", "creator", curve_sol=0)   # baseline read failed
        self.assertIsNone(await det.check_onchain("M", {}))  # sets baseline = 40
        self.assertAlmostEqual(det._monitored["M"].baseline_curve_sol, 40.0)
        det._monitored["M"].last_check_time = 0
        oc.sol = 10.0                                        # -75%
        signal = await det.check_onchain("M", {})
        self.assertIsNotNone(signal)
        self.assertEqual(signal.severity, "critical")


if __name__ == "__main__":
    unittest.main()
