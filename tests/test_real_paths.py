"""Offline tests for the code that touches real money in production:
wallet balance reads, transaction signing and sending. Real solders objects
and a throwaway keypair are used; only the network is faked."""

import base64
import unittest
from types import SimpleNamespace
from unittest import mock

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

import helpers
from helpers import FakeHttp, FakeResp, rpc_method
from jupiter import JupiterSwap
from pumpportal import PumpPortal
import txutil
from wallet import Wallet

MINT = "So11111111111111111111111111111111111111112"


def unsigned_tx_bytes(kp: Keypair) -> bytes:
    """What PumpPortal / Jupiter hand back: a transaction for us to sign."""
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(),
                                 to_pubkey=Keypair().pubkey(), lamports=1000))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    return bytes(VersionedTransaction(msg, [kp]))


def expected_sig(kp: Keypair, raw: bytes) -> str:
    msg = VersionedTransaction.from_bytes(raw).message
    return str(VersionedTransaction(msg, [kp]).signatures[0])


def token_account(raw_amount: int, decimals: int = 6):
    info = {"tokenAmount": {"amount": str(raw_amount), "decimals": decimals,
                            "uiAmount": raw_amount / 10 ** decimals}}
    return SimpleNamespace(account=SimpleNamespace(
        data=SimpleNamespace(parsed={"info": info})))


class StubClient:
    def __init__(self, accounts=None, fail=False):
        self.accounts = accounts or []
        self.fail = fail

    def get_token_accounts_by_owner_json_parsed(self, owner, opts):
        if self.fail:
            raise ConnectionError("rpc down")
        return SimpleNamespace(value=self.accounts)


def make_wallet(primary, fallback=None):
    w = Wallet.__new__(Wallet)        # skip key loading — no real key needed
    w.client = primary
    w._fallback_client = fallback
    w.pubkey = str(Keypair().pubkey())
    w._cached_balance = 0.0
    return w


class WalletReads(unittest.TestCase):
    def test_raw_amount_and_decimals_come_from_the_token_account(self):
        w = make_wallet(StubClient([token_account(1_234_567_890, 6)]))
        self.assertEqual(w.get_token_amount(MINT), (1234.56789, 1_234_567_890, 6))
        self.assertAlmostEqual(w.get_token_balance(MINT), 1234.56789)

    def test_multiple_token_accounts_are_summed(self):
        w = make_wallet(StubClient([token_account(1_000_000), token_account(2_500_000)]))
        self.assertEqual(w.get_token_amount(MINT), (3.5, 3_500_000, 6))

    def test_no_token_account_is_a_real_zero(self):
        w = make_wallet(StubClient([]))
        self.assertEqual(w.get_token_amount(MINT)[0], 0.0)
        self.assertEqual(w.get_token_balance(MINT), 0.0)

    def test_rpc_failure_is_none_not_zero(self):
        w = make_wallet(StubClient(fail=True), StubClient(fail=True))
        self.assertIsNone(w.get_token_amount(MINT))
        self.assertIsNone(w.get_token_balance(MINT))

    def test_fallback_client_is_used(self):
        w = make_wallet(StubClient(fail=True), StubClient([token_account(5_000_000)]))
        self.assertEqual(w.get_token_balance(MINT), 5.0)


class LocalTradeSigning(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.kp = Keypair()
        self.raw = unsigned_tx_bytes(self.kp)
        self.sig = expected_sig(self.kp, self.raw)
        self.sent_bodies = []

    def pp(self, rpc, use_jito=False, jito=None):
        def handler(method, url, kw):
            if "trade-local" in url:
                return FakeResp(200, content=self.raw)
            if "jito" in url:
                return jito(kw)
            self.sent_bodies.append(kw.get("content"))
            return rpc(url)
        p = PumpPortal("http://rpc.invalid", keypair=self.kp, api_key="",
                       use_jito=use_jito, fallback_rpc_url="http://fallback.invalid")
        p.http = FakeHttp(handler)
        return p

    async def trade(self, p):
        return await p._local_trade("buy", MINT, 0.03, True, 15, 0.005, "auto")

    async def test_signs_and_sends_returning_the_real_signature(self):
        p = self.pp(lambda url: FakeResp(200, {"result": self.sig}))
        sent = await self.trade(p)
        self.assertEqual(sent.sig, self.sig)
        self.assertFalse(sent.ambiguous)
        self.assertIn("sendTransaction", self.sent_bodies[0])

    async def test_explicit_rejection_by_every_rpc_is_a_clean_failure(self):
        p = self.pp(lambda url: FakeResp(200, {"error": {"code": -32002, "message": "bad"}}))
        sent = await self.trade(p)
        self.assertEqual(sent.sig, "")
        self.assertEqual(len(self.sent_bodies), 2)       # primary + fallback tried

    async def test_send_timeout_still_returns_the_signature_to_watch(self):
        # The tx may be in flight: losing its signature here is what used to
        # lead to a second buy.
        def rpc(url):
            raise ConnectionError("timeout")
        sent = await self.trade(self.pp(rpc))
        self.assertEqual(sent.sig, self.sig)

    async def test_jito_bundle_path_returns_the_same_signature(self):
        bundles = []

        def jito(kw):
            bundles.append(kw["json"])
            return FakeResp(200, {"result": "bundle-id"})
        p = self.pp(lambda url: FakeResp(200, {"result": self.sig}),
                    use_jito=True, jito=jito)
        sent = await self.trade(p)
        self.assertEqual(sent.sig, self.sig)
        self.assertEqual(bundles[0]["method"], "sendBundle")
        self.assertEqual(len(bundles[0]["params"][0]), 2)   # trade tx + tip tx
        self.assertEqual(self.sent_bodies, [])              # no direct send needed

    async def test_jito_failure_falls_back_to_the_same_transaction(self):
        p = self.pp(lambda url: FakeResp(200, {"result": self.sig}), use_jito=True,
                    jito=lambda kw: FakeResp(200, {"error": {"message": "rate limited"}}))
        sent = await self.trade(p)
        self.assertEqual(sent.sig, self.sig)
        self.assertEqual(len(self.sent_bodies), 1)

    async def test_full_local_buy_confirms(self):
        def handler(method, url, kw):
            if "trade-local" in url:
                return FakeResp(200, content=self.raw)
            if rpc_method(kw) == "getSignatureStatuses":
                return FakeResp(200, {"result": {"value": [
                    {"confirmationStatus": "confirmed", "err": None}]}})
            return FakeResp(200, {"result": self.sig})
        p = PumpPortal("http://rpc.invalid", keypair=self.kp, api_key="")
        p.http = FakeHttp(handler)
        result = await p.buy(MINT, 0.03)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.sig, self.sig)
        self.assertFalse(result.via_lightning)


class SendSignedTx(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        kp = Keypair()
        raw = unsigned_tx_bytes(kp)
        self.tx = VersionedTransaction(VersionedTransaction.from_bytes(raw).message, [kp])

    async def test_accepted(self):
        http = FakeHttp(lambda m, u, kw: FakeResp(200, {"result": "x"}))
        sig, accepted = await txutil.send_signed_tx(http, ["a"], self.tx)
        self.assertEqual(sig, str(self.tx.signatures[0]))
        self.assertIs(accepted, True)

    async def test_rejected(self):
        http = FakeHttp(lambda m, u, kw: FakeResp(200, {"error": {"code": 1}}))
        self.assertIs((await txutil.send_signed_tx(http, ["a", "b"], self.tx))[1], False)

    async def test_ambiguous(self):
        def handler(m, u, kw):
            raise TimeoutError()
        self.assertIsNone((await txutil.send_signed_tx(FakeHttp(handler), ["a"], self.tx))[1])


class JupiterSwapPath(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        p = mock.patch("asyncio.sleep", helpers.fast_sleep)
        p.start()
        self.addCleanup(p.stop)
        self.kp = Keypair()
        self.raw = unsigned_tx_bytes(self.kp)
        self.sig = expected_sig(self.kp, self.raw)
        self.quotes = []

    def jup(self, status):
        def handler(method, url, kw):
            if method == "GET":
                self.quotes.append(kw["params"])
                return FakeResp(200, {"outAmount": "123456", "routePlan": []})
            if "swap" in url and "jup.ag" in url:
                return FakeResp(200, {"swapTransaction": base64.b64encode(self.raw).decode()})
            if rpc_method(kw) == "getSignatureStatuses":
                return FakeResp(200, {"result": {"value": [status]}})
            return FakeResp(200, {"result": self.sig})
        j = JupiterSwap("http://rpc.invalid", keypair=self.kp)
        j.http = FakeHttp(handler)
        j.SELL_CONFIRM_TIMEOUT = 0.05
        return j

    async def test_sell_quotes_signs_sends_and_confirms(self):
        j = self.jup({"confirmationStatus": "confirmed", "err": None})
        result = await j.sell_token(MINT, 500_000_000, use_jito=False)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.sig, self.sig)
        self.assertEqual(self.quotes[0]["amount"], "500000000")
        self.assertEqual(self.quotes[0]["inputMint"], MINT)

    async def test_partial_sell_is_sent_once_when_unconfirmed(self):
        j = self.jup(None)
        result = await j.sell_token(MINT, 500_000_000, use_jito=False,
                                    resend_on_timeout=False)
        self.assertTrue(result.unknown)
        self.assertEqual(len(self.quotes), 1)

    async def test_buy_confirms(self):
        j = self.jup({"confirmationStatus": "finalized", "err": None})
        result = await j.buy_token(MINT, 0.03, use_jito=False)
        self.assertTrue(result.confirmed)
        self.assertEqual(self.quotes[0]["amount"], str(int(0.03 * 1e9)))
        self.assertEqual(len(self.quotes), 1)


if __name__ == "__main__":
    unittest.main()
