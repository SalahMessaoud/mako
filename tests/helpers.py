"""Shared test fixtures. Everything here is fake: no network, no real keys,
no real database. Importing this module blocks outbound sockets."""

import logging
import os
import socket
import sys
import time

# MAKO_SRC lets the suite run against another copy of the source tree.
SRC = os.environ.get("MAKO_SRC") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

# Never load the real .env (wallet key, Telegram token) into a test process.
import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
for _k in ("WALLET_PRIVATE_KEY", "PUMPPORTAL_API_KEY", "TELEGRAM_BOT_TOKEN",
           "TELEGRAM_CHAT_ID", "BOT_DB_PATH"):
    os.environ.pop(_k, None)


def _blocked(*a, **k):
    raise AssertionError("network access attempted in a test")


logging.disable(logging.CRITICAL)   # keep test output readable

socket.socket.connect = _blocked
socket.create_connection = _blocked

from config import Config  # noqa: E402
from onchain import OnChainResult  # noqa: E402
from txutil import TradeResult  # noqa: E402

OWNER = "BotWallet11111111111111111111111111111111111"
MINT = "MintAAAA1111111111111111111111111111111111pump"
OTHER = "SomeoneElse111111111111111111111111111111111"


async def fast_sleep(_seconds=0, *a, **k):
    """Stand-in for asyncio.sleep: yields to the loop but does not wait."""
    await _REAL_SLEEP(0)

import asyncio  # noqa: E402
_REAL_SLEEP = asyncio.sleep


def make_tx(owner=OWNER, mint=MINT, sol_delta=0.0, token_pre=0.0,
            token_post=0.0, err=None, fee_lamports=5000, owner_index=1,
            include_owner=True):
    """A getTransaction(jsonParsed) result. The owner is deliberately NOT at
    index 0 by default, to catch code that assumes it is."""
    keys = [{"pubkey": f"Filler{i}"} for i in range(owner_index)]
    pre = [10_000_000_000] * owner_index
    post = [10_000_000_000] * owner_index
    if include_owner:
        keys.append({"pubkey": owner, "signer": True})
        pre.append(5_000_000_000)
        post.append(5_000_000_000 + int(round(sol_delta * 1e9)))

    def tok(amount):
        return [{"accountIndex": 3, "mint": mint, "owner": owner,
                 "uiTokenAmount": {"uiAmount": amount, "decimals": 6,
                                   "amount": str(int(amount * 1e6))}}]
    return {
        "transaction": {"message": {"accountKeys": keys}},
        "meta": {
            "err": err, "fee": fee_lamports,
            "preBalances": pre, "postBalances": post,
            "preTokenBalances": tok(token_pre) if token_pre else [],
            "postTokenBalances": tok(token_post) if token_post else [],
        },
    }


class FakeResp:
    def __init__(self, status_code=200, data=None, content=b"", text=""):
        self.status_code = status_code
        self._data = data
        self.content = content
        self.text = text or str(data)

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Async http client driven by a handler(method, url, kwargs) -> FakeResp."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.handler("POST", url, kw)

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.handler("GET", url, kw)

    async def aclose(self):
        pass


def rpc_method(kw):
    body = kw.get("json") or {}
    return body.get("method", "")


class FakeWallet:
    """Live-wallet stand-in. A token value of None means 'RPC unreadable'."""
    pubkey = OWNER
    keypair = object()

    def __init__(self):
        self.sol = 1.0
        self.tokens = {}          # mint -> ui amount, or None for unreadable
        self.script = {}          # mint -> list of successive reads
        self.reads = 0

    def get_sol_balance(self):
        return self.sol

    @property
    def sol_balance(self):
        return self.sol

    def get_token_amount(self, mint):
        self.reads += 1
        script = self.script.get(mint)
        if script:
            value = script.pop(0) if len(script) > 1 else script[0]
        else:
            value = self.tokens.get(mint, 0.0)
        if value is None:
            return None
        return value, int(round(value * 1e6)), 6

    def get_token_balance(self, mint):
        amount = self.get_token_amount(mint)
        return amount[0] if amount is not None else None


class FakePump:
    use_jito = False

    def __init__(self):
        self.api_key = "test-key"
        self.buy_result = TradeResult("failed")
        self.sell_results = []        # consumed in order; last one repeats
        self.buy_calls = []
        self.sell_calls = []
        self.txs = {}                 # sig -> getTransaction result
        self.on_sell = None           # optional side effect(call_index)
        self.on_buy = None

    async def buy(self, mint, sol_amount, **kw):
        self.buy_calls.append((mint, sol_amount, kw))
        if self.on_buy:
            self.on_buy()
        return self.buy_result

    async def sell(self, mint, **kw):
        self.sell_calls.append((mint, kw))
        if self.on_sell:
            self.on_sell(len(self.sell_calls))
        if len(self.sell_results) > 1:
            return self.sell_results.pop(0)
        return self.sell_results[0] if self.sell_results else TradeResult("failed")

    async def get_tx_result(self, sig, retries=1):
        return self.txs.get(sig)

    def get_recent_failures(self, minutes=10):
        return 0

    async def close(self):
        pass


class FakeJupiter:
    def __init__(self):
        self.sell_result = TradeResult("failed")
        self.buy_result = TradeResult("failed")
        self.sell_calls = []
        self.buy_calls = []

    async def sell_token(self, mint, raw_amount, **kw):
        self.sell_calls.append((mint, raw_amount, kw))
        return self.sell_result

    async def buy_token(self, mint, sol_amount, **kw):
        self.buy_calls.append((mint, sol_amount, kw))
        return self.buy_result

    async def close(self):
        pass


class FakeOnchain:
    def __init__(self):
        self.graduated = False        # True / False / None (RPC failed)
        self.result = OnChainResult(
            mint_authority_revoked=True, freeze_authority_revoked=True,
            holder_count=20, top_holder_pct=10,
            mint_read_ok=True, holders_read_ok=True, creator_read_ok=True)
        self.decimals = 0             # deliberately wrong: must not be used
        self.curve_price_state = None

    async def is_graduated(self, mint):
        return self.graduated

    async def analyze(self, mint, creator=""):
        return self.result

    async def get_token_decimals(self, mint):
        return self.decimals

    async def get_bonding_curve_state(self, mint):
        return self.curve_price_state

    async def get_bonding_curve_pct(self, mint):
        return 0.0

    async def get_account_sol_balance(self, pubkey):
        return None

    async def close(self):
        pass


class FakeMarket:
    def __init__(self):
        self.token = None
        self.sol_price = 100.0

    async def get_token(self, mint):
        return self.token

    async def get_sol_price(self):
        return self.sol_price

    async def close(self):
        pass


class FakeStream:
    _watched_tokens = set()

    async def subscribe_token(self, mint):
        pass

    async def unsubscribe_token(self, mint):
        pass

    async def subscribe_account(self, wallet):
        pass


class FakeNotify:
    enabled = False

    def __init__(self):
        self.sent = []
        self.alerts = []

    def send(self, msg):
        self.sent.append(msg)

    def trade_alert(self, action, symbol, sol, **kw):
        self.alerts.append((action, symbol, sol, kw))

    def status_alert(self, status):
        pass

    def graduation_alert(self, *a, **k):
        pass

    def poll_events(self):
        return []

    def close(self):
        pass


def make_config(**over):
    base = dict(
        rpc_url="http://rpc.invalid", paper_trading=True, strategy="sniper",
        auto_discover_wallets=False, rug_detection=False,
        telegram_bot_token="", telegram_chat_id="",
        max_buy_sol=0.05, max_portfolio_sol=1.0, max_concurrent=10,
    )
    base.update(over)
    return Config(**base)


def make_live_bot(tmpdir, **cfg_over):
    """A MemeBot in live mode whose every external dependency is a fake."""
    from bot import MemeBot
    cfg = make_config(**cfg_over)
    bot = MemeBot(cfg, db_path=os.path.join(tmpdir, "test.db"))
    cfg.paper_trading = False
    bot.wallet = FakeWallet()
    bot.pump = FakePump()
    bot.jupiter = FakeJupiter()
    bot.onchain = FakeOnchain()
    bot.market = FakeMarket()
    bot.stream = FakeStream()
    bot.notify = FakeNotify()
    # No waiting in tests
    bot.ZERO_RECHECK_DELAY = 0
    bot.ZERO_CLOSE_MIN_AGE_SEC = 30
    bot.SELL_BALANCE_POLLS = 2
    bot.BUY_BALANCE_POLLS_CONFIRMED = (2, 0)
    bot.BUY_BALANCE_POLLS_UNKNOWN = (2, 0)
    bot.BUY_BALANCE_POLLS_NO_SIG = (2, 0)
    return bot


def open_position(bot, mint=MINT, tokens=1000.0, sol=0.03, age=60,
                  price_usd=0.001, strategy="sniper"):
    pos = bot.risk.open(mint, "TKN", price_usd, price_usd / 100, tokens, sol,
                        strategy, creator="Creator1111")
    pos.entry_time = time.time() - age
    bot.wallet.tokens[mint] = tokens
    return pos
