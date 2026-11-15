import time
import unittest

import helpers
from helpers import FakeHttp, FakeResp
from config import WSOL, JUPITER_PRICE_URL
from market_data import MarketData
from notifications import Notifier


class SolPrice(unittest.IsolatedAsyncioTestCase):
    async def test_jupiter_price_v3_shape_is_parsed(self):
        # Finding 12: the old endpoint is gone and the parser expected a
        # "data" wrapper the current API does not send.
        md = MarketData()
        md.http = FakeHttp(lambda m, url, kw: FakeResp(
            200, {WSOL: {"usdPrice": 71.95, "decimals": 9}}))
        self.assertAlmostEqual(await md.get_sol_price(), 71.95)
        self.assertEqual(md.http.calls[0][1], JUPITER_PRICE_URL)
        self.assertNotIn("price.jup.ag", JUPITER_PRICE_URL)

    async def test_falls_back_to_coingecko(self):
        def handler(method, url, kw):
            if "jup.ag" in url:
                return FakeResp(500, {})
            return FakeResp(200, {"solana": {"usd": 70.0}})
        md = MarketData()
        md.http = FakeHttp(handler)
        self.assertAlmostEqual(await md.get_sol_price(), 70.0)


class NotifierThreading(unittest.TestCase):
    def test_send_does_not_block_the_caller(self):
        # Finding 8: a slow Telegram call used to freeze the event loop.
        posted = []

        class SlowHttp:
            def post(self, url, json=None):
                time.sleep(0.3)
                posted.append(json["text"])
                return FakeResp(200, {"ok": True})

            def close(self):
                pass

        n = Notifier("token", "chat")
        n.http = SlowHttp()
        start = time.time()
        n.send("one")
        n.send("two")
        self.assertLess(time.time() - start, 0.1)
        n.close()                       # flushes the queue
        self.assertEqual(posted, ["one", "two"])


if __name__ == "__main__":
    unittest.main()
