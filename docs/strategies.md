# Strategies

Mako runs 4 independent strategies. Each evaluates market conditions and emits `Signal` objects with a confidence score (0.0 to 1.0). Every signal goes through safety checks and risk management before execution, and a signal for a token that already has a trade in flight is dropped.

Sniper, Copy Trade and Graduation are classes in `src/strategies.py`. Momentum is the discovery pipeline in `src/bot.py` (`_run_momentum`).

## Sniper

**Trigger**: New token creation event via PumpPortal WebSocket.

**Logic**: Fires on `subscribeNewToken` events. Checks for developer initial buy (minimum SOL threshold), validates the token has sufficient initial liquidity, and applies a configurable delay before entry.

**Best for**: Catching tokens in the first seconds of life. High risk, high reward.

**Key config**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `SNIPER_BUY_DELAY_SEC` | 2 | Wait time before buying after creation |
| `SNIPER_MIN_SOL_LIQUIDITY` | 5 | Minimum SOL in bonding curve |
| `SNIPER_MIN_DEV_BUY_SOL` | 0.5 | Minimum developer buy size |
| `SNIPER_AUTO_SELL_SEC` | 30 | Auto-sell timer after entry |

---

## Momentum

**Trigger**: 3-stage discovery pipeline (MintPool -> on-chain verify -> momentum score).

**Logic**: Collects new tokens into the MintPool and ranks them, favouring tokens under 6 minutes old and ones with live trade activity (stage 1). Checks ten candidates per cycle for recent on-chain activity: the six best-ranked plus four that rotate through the rest (stage 2). Then scores momentum: bonding curve growth rate, transaction rate over the last 5 minutes, buyer uniqueness, curve fill percentage (stage 3).

A token that passes the score must also show a higher curve than at an earlier pipeline cycle (4-90 seconds before). A token seen for the first time waits one cycle. On-chain safety checks run on the resulting signal before the buy.

**Best for**: Tokens with organic traction that have survived the initial volatility.

**Key config**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_MOMENTUM_CONFIDENCE` | 0.65 | Minimum confidence to buy |
| `MIN_MOMENTUM_SOL_IN_CURVE` | 15 | Minimum SOL in bonding curve |
| `MIN_MOMENTUM_TXNS` | 30 | Minimum transaction count |
| `MIN_MOMENTUM_CURVE_GROWTH` | 15 | Minimum curve growth % |
| `MIN_MOMENTUM_GROWTH_RATE` | 3 | Minimum growth rate %/min |

---

## Copy Trade

**Trigger**: Tracked wallet buys a token, seen on the PumpPortal WebSocket or by RPC polling of the wallet.

**Logic**: Subscribes to wallet activity via `subscribeAccountTrade`. When a tracked wallet buys, the bot copies the trade with a configurable delay and position size. When a tracked wallet sells, the bot mirrors the exit (copy-sell mirroring).

**Limitations**: PumpPortal WebSocket only covers pump.fun bonding curve trades. Trades on Raydium, Jupiter, or other DEXes are picked up by an RPC poll that runs about every 30 seconds, so those copies can be up to ~30 seconds late. The poll reads the last 15 signatures per wallet, for up to 8 wallets (manual wallets first), and ignores swaps into wrapped SOL, USDC and USDT. A very active wallet can have trades missed between polls.

**Trade feed**: The WebSocket only delivers wallet and token trade events when `PUMPPORTAL_TRADE_FEED=true`, `PUMPPORTAL_API_KEY` is set and its wallet holds at least 0.02 SOL. PumpPortal bills those events to that wallet. Without that, copy trade runs on the RPC poll alone and wallet discovery sees nothing.

**Wallet discovery**: With `AUTO_DISCOVER_WALLETS=true`, observed wallets are promoted to copy targets after complete, mostly profitable round trips on at least `WALLET_DISCOVERY_MIN_TOKENS` different tokens. Token creators are never promoted. See the README, Section 13.

**Key config**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `COPY_WALLETS` | (empty) | Comma-separated wallet addresses |
| `COPY_DELAY_SEC` | 2 | Delay before copying a trade |
| `COPY_MAX_BUY_SOL` | 0.02 | Max SOL per copy trade |

---

## Graduation

**Trigger**: Token bonding curve approaching 100% fill (about to "graduate" to Raydium).

**Logic**: Monitors tokens in the MintPool whose bonding curve exceeds `GRAD_MIN_CURVE_PCT`. A graduation is detected when the curve reads 100% on-chain and the bot read the same token below 100% earlier, so a token that graduated long ago is never mistaken for a fresh one. The strategy then waits 60-300 seconds for the pool to establish and buys via Jupiter.

**Entry guards**: The DEX pool must be no older than `GRAD_MAX_POOL_AGE_MIN`, have $5,000+ liquidity and at least `GRAD_MIN_TXNS_5M` trades in the last 5 minutes, and a 5-minute price change between -30% and +150%.

**Stops**: A post-graduation position has its stop-loss armed from entry. The 5-minute graduation grace applies only to positions that were held on the bonding curve through the migration.

**Best for**: Catching the transition from pump.fun to Raydium, where price often pumps due to new liquidity and visibility.

**Key config**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `GRAD_MIN_CURVE_PCT` | 75 | Start tracking at this curve fill % |
| `GRAD_BUY_AT_PCT` | 95 | Not used: buys happen only after graduation |
| `GRADUATION_MAX_BUY_SOL` | 0.04 | Max SOL for graduation trades |
| `GRAD_MAX_POOL_AGE_MIN` | 30 | Maximum DEX pool age for a post-graduation buy |
| `GRAD_MIN_TXNS_5M` | 5 | Minimum trades in 5 minutes before entry |
