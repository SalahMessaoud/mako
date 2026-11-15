"""Configuration management — loads from .env and validates."""

import os
import logging
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

_cfg_log = logging.getLogger(__name__)


def _safe_float(env_key: str, default: str) -> float:
    val = os.getenv(env_key, default)
    try:
        return float(val)
    except (ValueError, TypeError):
        _cfg_log.warning(f"Invalid float for {env_key}={val!r}, using default {default}")
        return float(default)


def _safe_int(env_key: str, default: str) -> int:
    val = os.getenv(env_key, default)
    try:
        return int(val)
    except (ValueError, TypeError):
        _cfg_log.warning(f"Invalid int for {env_key}={val!r}, using default {default}")
        return int(default)

# ── Solana Constants ──────────────────────────────────────────
WSOL = "So11111111111111111111111111111111111111112"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
PUMP_FEE = "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbCJ2waxKVo8"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOC_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
RENT = "SysvarRent111111111111111111111111111111111"
EVENT_AUTHORITY = "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp18C"

# ── API URLs ─────────────────────────────────────────────────
PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"
PUMPPORTAL_LIGHTNING_URL = "https://pumpportal.fun/api/trade"
PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3"
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"
JITO_BUNDLE_URL = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"


@dataclass
class Config:
    """All bot configuration in one place."""

    # RPC
    rpc_url: str = ""
    wss_url: str = ""
    fallback_rpc_url: str = ""

    # Wallet
    private_key: str = ""

    # PumpPortal
    pumpportal_api_key: str = ""

    # Mode
    paper_trading: bool = True
    paper_balance_sol: float = 2.0
    paper_slippage_pct: float = 3.0
    strategy: str = "multi"
    notify_only: bool = False  # Run pipeline but don't execute trades, just notify

    # Position sizing (strategy-specific caps)
    max_buy_sol: float = 0.05
    momentum_max_buy_sol: float = 0.03
    graduation_max_buy_sol: float = 0.04
    copy_max_buy_sol: float = 0.02
    max_portfolio_sol: float = 1.0
    max_concurrent: int = 10
    daily_loss_limit: float = 0.3

    # Sniper
    sniper_buy_delay: float = 2.0
    sniper_auto_sell_sec: int = 30
    sniper_min_sol_liq: float = 5.0
    sniper_min_dev_buy_sol: float = 0.5
    sniper_max_top_holder_pct: float = 70.0
    pipeline_max_top_holder_pct: float = 50.0

    # Exits
    fast_stop_grace_sec: int = 180
    nostop_window_sec: int = 180
    nostop_multiplier: float = 4.0
    ws_stop_loss_enabled: bool = False
    stop_loss_pct: float = 10.0
    momentum_stop_loss_pct: float = 12.0
    take_profit_pct: float = 100.0
    take_profit_2_pct: float = 300.0
    trailing_stop_pct: float = 25.0
    partial_take_pct: float = 50.0
    max_hold_minutes: int = 30
    stale_exit_minutes: float = 10.0
    stale_exit_pct: float = 7.0
    graduated_trailing_2_pct: float = 35.0

    # Peaked filters
    peaked_growth_cap_pct: float = 200.0    # Hard reject above this growth %
    peaked_age_multiplier: float = 20.0     # Max growth = age_min * this

    # Safety: block a buy when on-chain safety data could not be read
    # (RPC failure). False restores the old "proceed anyway" behaviour.
    safety_fail_closed: bool = True

    # Momentum quality filters
    min_momentum_confidence: float = 0.65
    min_momentum_sol_in_curve: float = 15.0
    min_momentum_txns: int = 30
    min_momentum_curve_growth: float = 30.0
    min_momentum_growth_rate: float = 8.0  # min %/min curve growth rate
    max_creator_tx_count: int = 80
    min_holder_count: int = 5
    max_daily_trades: int = 30

    # Cooldown tuning
    cooldown_trigger_losses: int = 5
    cooldown_duration_sec: int = 120
    cooldown_exempt_confidence: float = 0.85

    # Filters
    min_liquidity_usd: float = 5000
    min_token_age_sec: float = 5
    max_top_holder_pct: float = 30
    require_social: bool = False

    # Copy trading
    copy_wallets: list = field(default_factory=list)
    copy_delay_sec: float = 2.0

    # Graduation
    grad_min_curve_pct: float = 75
    grad_buy_at_pct: float = 88

    # Wallet discovery
    auto_discover_wallets: bool = True
    max_auto_wallets: int = 10
    min_wallet_score_to_watch: float = 50.0
    wallet_discovery_min_buys: int = 2
    wallet_discovery_min_sells: int = 1
    wallet_discovery_min_ratio: float = 1.15

    # Rug detection
    rug_detection: bool = True
    rug_creator_sell_exit: bool = True
    rug_curve_drop_pct: float = 40.0

    # Re-entry cooldown
    mint_cooldown_minutes: int = 30

    # Jupiter
    use_jupiter: bool = True
    jupiter_slippage_bps: int = 1500

    # Jito
    jito_tip_sol: float = 0.001
    use_jito: bool = True

    # Execution
    slippage_pct: int = 15
    momentum_slippage_pct: int = 12
    priority_fee_sol: float = 0.005
    poll_interval: int = 3

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        mode = os.getenv("TRADING_MODE", "paper").lower()
        wallets_str = os.getenv("COPY_WALLETS", "")
        wallets = [w.strip() for w in wallets_str.split(",") if w.strip()]

        return cls(
            rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
            wss_url=os.getenv("SOLANA_WSS_URL", "wss://api.mainnet-beta.solana.com"),
            fallback_rpc_url=os.getenv("FALLBACK_RPC_URL", ""),
            private_key=os.getenv("WALLET_PRIVATE_KEY", ""),
            pumpportal_api_key=os.getenv("PUMPPORTAL_API_KEY", ""),
            paper_trading=(mode != "live"),
            paper_balance_sol=_safe_float("PAPER_BALANCE_SOL", "2.0"),
            paper_slippage_pct=_safe_float("PAPER_SLIPPAGE_PCT", "3.0"),
            strategy=os.getenv("STRATEGY", "multi"),
            notify_only=os.getenv("NOTIFY_ONLY", "false").lower() == "true",
            max_buy_sol=_safe_float("MAX_BUY_SOL", "0.05"),
            momentum_max_buy_sol=_safe_float("MOMENTUM_MAX_BUY_SOL", "0.03"),
            graduation_max_buy_sol=_safe_float("GRADUATION_MAX_BUY_SOL", "0.04"),
            copy_max_buy_sol=_safe_float("COPY_MAX_BUY_SOL", "0.02"),
            max_portfolio_sol=_safe_float("MAX_PORTFOLIO_SOL", "1.0"),
            max_concurrent=_safe_int("MAX_CONCURRENT_POSITIONS", "10"),
            daily_loss_limit=_safe_float("DAILY_LOSS_LIMIT_SOL", "0.3"),
            sniper_buy_delay=_safe_float("SNIPER_BUY_DELAY_SEC", "2"),
            sniper_auto_sell_sec=_safe_int("SNIPER_AUTO_SELL_SEC", "30"),
            sniper_min_sol_liq=_safe_float("SNIPER_MIN_SOL_LIQUIDITY", "5"),
            sniper_min_dev_buy_sol=_safe_float("SNIPER_MIN_DEV_BUY_SOL", "0.5"),
            sniper_max_top_holder_pct=_safe_float("SNIPER_MAX_TOP_HOLDER_PCT", "70"),
            pipeline_max_top_holder_pct=_safe_float("PIPELINE_MAX_TOP_HOLDER_PCT", "50"),
            fast_stop_grace_sec=_safe_int("FAST_STOP_GRACE_SEC", "180"),
            nostop_window_sec=_safe_int("NOSTOP_WINDOW_SEC", "180"),
            nostop_multiplier=_safe_float("NOSTOP_MULTIPLIER", "4"),
            ws_stop_loss_enabled=os.getenv("WS_STOP_LOSS_ENABLED", "false").lower() == "true",
            stop_loss_pct=_safe_float("STOP_LOSS_PCT", "10"),
            momentum_stop_loss_pct=_safe_float("MOMENTUM_STOP_LOSS_PCT", "12"),
            take_profit_pct=_safe_float("TAKE_PROFIT_PCT", "100"),
            take_profit_2_pct=_safe_float("TAKE_PROFIT_2_PCT", "300"),
            trailing_stop_pct=_safe_float("TRAILING_STOP_PCT", "25"),
            partial_take_pct=_safe_float("PARTIAL_TAKE_PCT", "50"),
            max_hold_minutes=_safe_int("MAX_HOLD_MINUTES", "30"),
            stale_exit_minutes=_safe_float("STALE_EXIT_MINUTES", "10"),
            stale_exit_pct=_safe_float("STALE_EXIT_PCT", "7"),
            graduated_trailing_2_pct=_safe_float("GRADUATED_TRAILING_2_PCT", "35"),
            peaked_growth_cap_pct=_safe_float("PEAKED_GROWTH_CAP_PCT", "200"),
            peaked_age_multiplier=_safe_float("PEAKED_AGE_MULTIPLIER", "20"),
            safety_fail_closed=os.getenv("SAFETY_FAIL_CLOSED", "true").lower() == "true",
            min_momentum_confidence=_safe_float("MIN_MOMENTUM_CONFIDENCE", "0.65"),
            min_momentum_sol_in_curve=_safe_float("MIN_MOMENTUM_SOL_IN_CURVE", "15"),
            min_momentum_txns=_safe_int("MIN_MOMENTUM_TXNS", "30"),
            min_momentum_curve_growth=_safe_float("MIN_MOMENTUM_CURVE_GROWTH", "30"),
            min_momentum_growth_rate=_safe_float("MIN_MOMENTUM_GROWTH_RATE", "8"),
            max_creator_tx_count=_safe_int("MAX_CREATOR_TX_COUNT", "80"),
            min_holder_count=_safe_int("MIN_HOLDER_COUNT", "5"),
            max_daily_trades=_safe_int("MAX_DAILY_TRADES", "20"),
            cooldown_trigger_losses=_safe_int("COOLDOWN_TRIGGER_LOSSES", "5"),
            cooldown_duration_sec=_safe_int("COOLDOWN_DURATION_SEC", "120"),
            cooldown_exempt_confidence=_safe_float("COOLDOWN_EXEMPT_CONFIDENCE", "0.85"),
            min_liquidity_usd=_safe_float("MIN_LIQUIDITY_USD", "5000"),
            min_token_age_sec=_safe_float("MIN_TOKEN_AGE_SEC", "5"),
            max_top_holder_pct=_safe_float("MAX_TOP_HOLDER_PCT", "30"),
            require_social=os.getenv("REQUIRE_SOCIAL", "false").lower() == "true",
            copy_wallets=wallets,
            copy_delay_sec=_safe_float("COPY_DELAY_SEC", "2"),
            grad_min_curve_pct=_safe_float("GRAD_MIN_CURVE_PCT", "85"),
            grad_buy_at_pct=_safe_float("GRAD_BUY_AT_PCT", "95"),
            auto_discover_wallets=os.getenv("AUTO_DISCOVER_WALLETS", "true").lower() == "true",
            max_auto_wallets=_safe_int("MAX_AUTO_WALLETS", "10"),
            min_wallet_score_to_watch=_safe_float("MIN_WALLET_SCORE_TO_WATCH", "50"),
            wallet_discovery_min_buys=_safe_int("WALLET_DISCOVERY_MIN_BUYS", "2"),
            wallet_discovery_min_sells=_safe_int("WALLET_DISCOVERY_MIN_SELLS", "1"),
            wallet_discovery_min_ratio=_safe_float("WALLET_DISCOVERY_MIN_RATIO", "1.15"),
            rug_detection=os.getenv("RUG_DETECTION", "true").lower() == "true",
            rug_creator_sell_exit=os.getenv("RUG_CREATOR_SELL_EXIT", "true").lower() == "true",
            rug_curve_drop_pct=_safe_float("RUG_CURVE_DROP_PCT", "40"),
            mint_cooldown_minutes=_safe_int("MINT_COOLDOWN_MINUTES", "30"),
            use_jupiter=os.getenv("USE_JUPITER", "true").lower() == "true",
            jupiter_slippage_bps=_safe_int("JUPITER_SLIPPAGE_BPS", "1500"),
            jito_tip_sol=_safe_float("JITO_TIP_SOL", "0.001"),
            use_jito=os.getenv("USE_JITO", "true").lower() == "true",
            slippage_pct=_safe_int("SLIPPAGE_PCT", "15"),
            momentum_slippage_pct=_safe_int("MOMENTUM_SLIPPAGE_PCT", "12"),
            priority_fee_sol=_safe_float("PRIORITY_FEE_SOL", "0.005"),
            poll_interval=_safe_int("POLL_INTERVAL_SEC", "3"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )

    def validate(self) -> list[str]:
        issues = []
        if not self.rpc_url:
            issues.append("SOLANA_RPC_URL required")
        if not self.paper_trading and not self.private_key:
            issues.append("WALLET_PRIVATE_KEY required for live trading")
        if self.strategy == "copy_trade" and not self.copy_wallets:
            issues.append("COPY_WALLETS required for copy_trade strategy")
        if self.max_buy_sol <= 0:
            issues.append("MAX_BUY_SOL must be > 0")
        if self.take_profit_pct <= self.stop_loss_pct:
            issues.append(f"TAKE_PROFIT_PCT ({self.take_profit_pct}) must be > STOP_LOSS_PCT ({self.stop_loss_pct})")
        if self.slippage_pct > 50:
            issues.append(f"SLIPPAGE_PCT ({self.slippage_pct}) must be <= 50")
        if self.max_concurrent <= 0:
            issues.append("MAX_CONCURRENT_POSITIONS must be > 0")
        if self.daily_loss_limit <= 0:
            issues.append("DAILY_LOSS_LIMIT_SOL must be > 0")
        if self.peaked_growth_cap_pct <= 0:
            issues.append("PEAKED_GROWTH_CAP_PCT must be > 0")
        if self.peaked_age_multiplier <= 0:
            issues.append("PEAKED_AGE_MULTIPLIER must be > 0")
        if self.max_buy_sol > self.max_portfolio_sol:
            issues.append(f"MAX_BUY_SOL ({self.max_buy_sol}) must be <= MAX_PORTFOLIO_SOL ({self.max_portfolio_sol})")
        return issues
