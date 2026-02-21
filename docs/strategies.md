# Strategies

Mako runs 4 independent strategies. Each evaluates market conditions and emits `Signal` objects with a confidence score (0.0 to 1.0). The orchestrator picks the highest-confidence signal and routes it through safety checks and risk management before execution.

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

**Logic**: Collects new tokens into the MintPool (stage 1), verifies on-chain safety (stage 2), then evaluates momentum indicators: bonding curve growth rate, transaction count, buyer uniqueness, curve fill percentage (stage 3). Tokens passing all thresholds generate a Signal with confidence proportional to how far they exceed minimums.

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

**Trigger**: Tracked wallet buys a token via PumpPortal WebSocket.

**Logic**: Subscribes to wallet activity via `subscribeAccountTrade`. When a tracked wallet buys, the bot copies the trade with a configurable delay and position size. When a tracked wallet sells, the bot mirrors the exit (copy-sell mirroring).

**Limitations**: PumpPortal WebSocket only covers pump.fun bonding curve trades. Trades on Raydium, Jupiter, or other DEXes are not captured.

**Key config**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `COPY_WALLETS` | (empty) | Comma-separated wallet addresses |
| `COPY_DELAY_SEC` | 2 | Delay before copying a trade |
| `COPY_MAX_BUY_SOL` | 0.02 | Max SOL per copy trade |

---

## Graduation

**Trigger**: Token bonding curve approaching 100% fill (about to "graduate" to Raydium).

**Logic**: Monitors tokens in the MintPool whose bonding curve exceeds `GRAD_MIN_CURVE_PCT`. When a token's curve reaches `GRAD_BUY_AT_PCT`, the strategy detects the graduation event, waits 60-120 seconds for the Raydium pool to establish, then buys via Jupiter V6 swap routing.

**Best for**: Catching the transition from pump.fun to Raydium, where price often pumps due to new liquidity and visibility.

**Key config**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `GRAD_MIN_CURVE_PCT` | 75 | Start tracking at this curve fill % |
| `GRAD_BUY_AT_PCT` | 88 | Trigger graduation buy at this % |
| `GRADUATION_MAX_BUY_SOL` | 0.04 | Max SOL for graduation trades |
