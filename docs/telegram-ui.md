# Telegram Interface

Mako includes a full Telegram GUI with button-driven navigation, inspired by popular Solana trading bots like Trojan and BonkBot.

## Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram
2. Copy the bot token to `TELEGRAM_BOT_TOKEN` in `.env`
3. Send `/start` to your bot, then copy your chat ID to `TELEGRAM_CHAT_ID` in `.env`
4. Restart Mako

## Screens

### Home Dashboard

The main screen shows balance, daily P&L, positions, and win rate. All navigation happens by editing this single message in place.

Buttons: Positions, Trades, Settings, Stats, Strategies, Pause/Resume, Refresh

### Positions

Lists all open positions with current P&L percentage, entry size, age, strategy, and exit parameters. Each position has sell buttons (50% and 100%). A "Sell All" button with confirmation is available for emergency exits.

### Settings

Displays current config values with edit buttons. Tapping a setting shows preset options and a custom input option. Settings take effect immediately without restart.

Configurable parameters:
- Stop Loss %
- Take Profit %
- TP2 %
- Trailing Stop %
- Max Buy SOL
- Confidence threshold
- Curve Growth %
- Growth Rate %/min
- Max Hold Minutes
- Slippage %

### Trades

Shows recent trade history with entry/exit details, P&L, strategy used, and exit reason.

### Stats

Strategy breakdown showing trade count and P&L per strategy.

### Copy Wallets

Add or remove copy trade wallets live. Changes take effect immediately (WebSocket subscriptions update in real-time).

### Strategies

Enable or disable individual strategies without restarting the bot.

## Security

- All events are checked against the configured `TELEGRAM_CHAT_ID`. Unknown chat IDs are silently ignored.
- Setting changes are validated against min/max bounds.
- Destructive actions (sell all, pause) require two-step confirmation.
- Wallet private keys are never displayed.
