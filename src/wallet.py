"""Wallet management — real and paper trading wallets."""

import base58
import logging
import time
from dataclasses import dataclass, field
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAccountOpts

log = logging.getLogger(__name__)


class Wallet:
    """Real Solana wallet for live trading."""

    def __init__(self, rpc_url: str, private_key: str):
        self.client = Client(rpc_url, commitment=Confirmed)
        key_bytes = base58.b58decode(private_key)
        self.keypair = Keypair.from_bytes(key_bytes)
        self.pubkey = str(self.keypair.pubkey())
        self._cached_balance: float = 0.0
        log.info(f"Wallet loaded: {self.pubkey}")

    @property
    def sol_balance(self) -> float:
        return self.get_sol_balance()

    def get_sol_balance(self) -> float:
        try:
            resp = self.client.get_balance(Pubkey.from_string(self.pubkey))
            bal = resp.value / 1e9
            self._cached_balance = bal
            return bal
        except Exception as e:
            log.error(f"Balance error: {type(e).__name__}: {e}")
            return self._cached_balance

    def get_token_balance(self, mint: str) -> float:
        try:
            resp = self.client.get_token_accounts_by_owner_json_parsed(
                Pubkey.from_string(self.pubkey),
                TokenAccountOpts(mint=Pubkey.from_string(mint)),
            )
            if resp.value:
                for acct in resp.value:
                    info = acct.account.data.parsed["info"]
                    return float(info["tokenAmount"]["uiAmount"] or 0)
            return 0.0
        except Exception as e:
            log.error(f"Token balance error: {e}")
            return 0.0


@dataclass
class PaperPosition:
    mint: str
    symbol: str
    tokens: float
    sol_cost: float
    entry_price: float
    entry_time: float


class PaperWallet:
    """Simulated wallet for paper trading with real market data."""

    def __init__(self, initial_sol: float = 5.0):
        self.sol_balance = initial_sol
        self.initial_sol = initial_sol
        self.positions: dict[str, PaperPosition] = {}
        self.trade_log: list[dict] = []
        self.total_pnl = 0.0
        self.pubkey = "PAPER_WALLET"
        self.keypair = None
        log.info(f"💵 Paper wallet: {initial_sol} SOL")

    def get_sol_balance(self) -> float:
        return self.sol_balance

    def get_token_balance(self, mint: str) -> float:
        pos = self.positions.get(mint)
        return pos.tokens if pos else 0.0

    def buy(self, mint: str, symbol: str, sol_amount: float,
            tokens: float, price_usd: float) -> bool:
        if sol_amount > self.sol_balance:
            log.warning(f"Paper: insufficient SOL ({self.sol_balance:.4f})")
            return False
        self.sol_balance -= sol_amount
        if mint in self.positions:
            pos = self.positions[mint]
            pos.tokens += tokens
            pos.sol_cost += sol_amount
        else:
            self.positions[mint] = PaperPosition(
                mint=mint, symbol=symbol, tokens=tokens,
                sol_cost=sol_amount, entry_price=price_usd,
                entry_time=time.time(),
            )
        self.trade_log.append({
            "time": time.time(), "action": "buy", "mint": mint,
            "symbol": symbol, "sol": sol_amount, "tokens": tokens,
            "price": price_usd,
        })
        log.info(f"📗 PAPER BUY: {symbol} | {sol_amount:.4f} SOL → {tokens:.2f} tokens")
        return True

    def sell(self, mint: str, token_amount: float, sol_received: float,
             price_usd: float) -> float:
        pos = self.positions.get(mint)
        if not pos:
            return 0.0
        ratio = min(token_amount / pos.tokens, 1.0) if pos.tokens > 0 else 1.0
        sol_cost_portion = pos.sol_cost * ratio
        pnl = sol_received - sol_cost_portion
        self.total_pnl += pnl

        pos.tokens -= token_amount
        pos.sol_cost -= sol_cost_portion
        self.sol_balance += sol_received

        if pos.tokens < 0.001:
            del self.positions[mint]

        self.trade_log.append({
            "time": time.time(), "action": "sell", "mint": mint,
            "symbol": pos.symbol, "sol": sol_received,
            "tokens": token_amount, "price": price_usd, "pnl": pnl,
        })
        emoji = "🟢" if pnl >= 0 else "🔴"
        pnl_pct = (pnl / sol_cost_portion * 100) if sol_cost_portion > 0 else 0
        log.info(f"{emoji} PAPER SELL: {pos.symbol} | {sol_received:.4f} SOL | PnL: {pnl:+.4f} ({pnl_pct:+.1f}%)")
        return pnl

    def get_summary(self) -> dict:
        return {
            "sol_balance": self.sol_balance,
            "positions": len(self.positions),
            "total_pnl": self.total_pnl,
            "total_value": self.sol_balance + sum(p.sol_cost for p in self.positions.values()),
            "roi_pct": (self.total_pnl / self.initial_sol) * 100,
        }
