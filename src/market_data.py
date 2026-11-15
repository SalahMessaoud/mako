"""Market data from DexScreener and Jupiter Price API."""

import time
import logging
import httpx
from dataclasses import dataclass
from config import DEXSCREENER_BASE, JUPITER_PRICE_URL

log = logging.getLogger(__name__)


@dataclass
class TokenData:
    """Unified token market data."""
    mint: str
    symbol: str
    name: str
    price_usd: float
    price_sol: float
    liquidity_usd: float
    volume_24h: float
    volume_5m: float
    price_change_5m: float
    price_change_1h: float
    price_change_24h: float
    market_cap: float
    fdv: float
    pair_address: str
    pair_created_at: int  # ms
    buys_5m: int
    sells_5m: int

    @property
    def age_seconds(self) -> float:
        return (time.time() * 1000 - self.pair_created_at) / 1000

    @property
    def age_minutes(self) -> float:
        return self.age_seconds / 60

    @property
    def buy_ratio_5m(self) -> float:
        total = self.buys_5m + self.sells_5m
        return self.buys_5m / total if total > 0 else 0.5

    @property
    def is_active(self) -> bool:
        return (self.buys_5m + self.sells_5m) > 3


class MarketData:
    """Aggregates market data from multiple sources."""

    def __init__(self):
        self.http = httpx.AsyncClient(timeout=10)
        self._cache: dict[str, tuple[float, TokenData]] = {}
        self._cache_ttl = 4  # seconds
        self._sol_price: float = 0.0
        self._sol_price_time: float = 0.0

    async def get_token(self, mint: str) -> TokenData | None:
        """Get token data by mint address (cached)."""
        now = time.time()
        if mint in self._cache:
            ts, data = self._cache[mint]
            if now - ts < self._cache_ttl:
                return data

        # Evict stale cache entries to prevent unbounded growth
        if len(self._cache) > 500:
            stale = [k for k, (ts, _) in self._cache.items() if now - ts > 60]
            for k in stale:
                del self._cache[k]

        try:
            resp = await self.http.get(f"{DEXSCREENER_BASE}/tokens/{mint}")
            resp.raise_for_status()
            pairs = resp.json().get("pairs") or []

            sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            if not sol_pairs:
                return None

            pair = max(sol_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            data = self._parse(pair, mint)
            self._cache[mint] = (now, data)
            return data
        except Exception as e:
            log.error(f"DexScreener error ({mint[:8]}...): {e}")
            return None

    async def get_trending(self, min_liq: float = 10_000, limit: int = 20) -> list[TokenData]:
        """Get trending Solana tokens from DexScreener boosted tokens."""
        results = []
        try:
            # Step 1: discover trending token addresses via boosts
            mints = set()
            for endpoint in [
                "https://api.dexscreener.com/token-boosts/top/v1",
                "https://api.dexscreener.com/token-profiles/latest/v1",
            ]:
                try:
                    resp = await self.http.get(endpoint, timeout=5)
                    if resp.status_code == 200:
                        for item in resp.json():
                            if item.get("chainId") == "solana":
                                mints.add(item["tokenAddress"])
                except Exception:
                    pass

            if mints:
                results = await self.get_tokens_batch(list(mints)[:30], min_liq)

        except Exception as e:
            log.error(f"Trending discovery error: {e}")

        # Fallback: search if boosts returned nothing
        if not results:
            try:
                resp = await self.http.get(f"{DEXSCREENER_BASE}/search", params={"q": "SOL"})
                resp.raise_for_status()
                for pair in resp.json().get("pairs", []):
                    if pair.get("chainId") != "solana":
                        continue
                    liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                    if liq < min_liq:
                        continue
                    mint = pair.get("baseToken", {}).get("address", "")
                    if mint:
                        results.append(self._parse(pair, mint))
            except Exception as e:
                log.error(f"Trending search fallback error: {e}")

        results.sort(key=lambda t: t.volume_5m, reverse=True)
        return results[:limit]

    async def get_tokens_batch(self, mints: list[str], min_liq: float = 0) -> list[TokenData]:
        """Fetch data for multiple tokens in one DexScreener call (max 30)."""
        results = []
        if not mints:
            return results
        try:
            batch = ",".join(mints[:30])
            resp = await self.http.get(f"{DEXSCREENER_BASE}/tokens/{batch}")
            resp.raise_for_status()
            seen = set()
            for pair in resp.json().get("pairs", []):
                if pair.get("chainId") != "solana":
                    continue
                mint = pair.get("baseToken", {}).get("address", "")
                if not mint or mint in seen:
                    continue
                liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                if liq < min_liq:
                    continue
                seen.add(mint)
                results.append(self._parse(pair, mint))
        except Exception as e:
            log.error(f"Batch token fetch error: {e}")
        return results

    async def get_sol_price(self) -> float:
        """Get SOL/USD price."""
        now = time.time()
        if now - self._sol_price_time < 30 and self._sol_price > 0:
            return self._sol_price
        # Try Jupiter first
        try:
            from config import WSOL
            resp = await self.http.get(JUPITER_PRICE_URL, params={"ids": WSOL}, timeout=5)
            resp.raise_for_status()
            body = resp.json()
            # Price API v3: {mint: {"usdPrice": ...}}. Older: {"data": {mint: {"price": ...}}}
            entry = body.get(WSOL) or (body.get("data") or {}).get(WSOL) or {}
            price = float(entry.get("usdPrice") or entry.get("price") or 0)
            if price > 0:
                self._sol_price = price
                self._sol_price_time = now
                return self._sol_price
        except Exception:
            pass
        # Fallback: CoinGecko
        try:
            resp = await self.http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
                timeout=5,
            )
            resp.raise_for_status()
            price = float(resp.json().get("solana", {}).get("usd", 0))
            if price > 0:
                self._sol_price = price
                self._sol_price_time = now
                return self._sol_price
        except Exception as e:
            log.warning(f"SOL price fetch failed: {e}")
        return self._sol_price or 0.0

    def _parse(self, pair: dict, mint: str) -> TokenData:
        base = pair.get("baseToken", {})
        txns = pair.get("txns", {}).get("m5", {})
        pc = pair.get("priceChange", {})
        vol = pair.get("volume", {})
        return TokenData(
            mint=mint,
            symbol=base.get("symbol", "???"),
            name=base.get("name", "Unknown"),
            price_usd=float(pair.get("priceUsd", 0) or 0),
            price_sol=float(pair.get("priceNative", 0) or 0),
            liquidity_usd=float(pair.get("liquidity", {}).get("usd", 0) or 0),
            volume_24h=float(vol.get("h24", 0) or 0),
            volume_5m=float(vol.get("m5", 0) or 0),
            price_change_5m=float(pc.get("m5", 0) or 0),
            price_change_1h=float(pc.get("h1", 0) or 0),
            price_change_24h=float(pc.get("h24", 0) or 0),
            market_cap=float(pair.get("marketCap", 0) or 0),
            fdv=float(pair.get("fdv", 0) or 0),
            pair_address=pair.get("pairAddress", ""),
            pair_created_at=int(pair.get("pairCreatedAt", 0) or 0),
            buys_5m=int(txns.get("buys", 0) or 0),
            sells_5m=int(txns.get("sells", 0) or 0),
        )

    async def close(self):
        await self.http.aclose()
