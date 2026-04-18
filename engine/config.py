"""
Configuration for the Kraken BTC Trading Bot.
Loads settings from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class KrakenConfig:
    """Kraken API credentials and settings."""
    api_key: str = ""
    api_secret: str = ""
    # Kraken uses "XXBTZUSD" internally, but the SDK accepts "XBTUSD"
    symbol: str = "XBTUSD"
    # Human-readable pair for display
    display_symbol: str = "BTC/USD"


@dataclass
class StrategyConfig:
    """Trading strategy parameters."""
    # --- EMA crossover ---
    ema_fast_period: int = 9
    ema_slow_period: int = 21
    # --- RSI ---
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    # --- Bollinger Bands ---
    bb_period: int = 20
    bb_std_dev: float = 2.0
    # --- Risk management ---
    stop_loss_pct: float = 3.0       # percent below entry to cut losses
    take_profit_pct: float = 6.0     # percent above entry to lock profit
    # --- Position sizing ---
    risk_per_trade_pct: float = 2.0  # percent of capital risked per trade
    max_position_pct: float = 30.0   # max percent of capital in a single position
    # --- Scan interval ---
    scan_interval_seconds: int = 60  # how often to check signals (seconds)
    # --- OHLCV history needed for indicators ---
    history_bars: int = 100          # number of candles to fetch for indicator warmup


@dataclass
class PaperTradingConfig:
    """Paper trading (simulation) settings."""
    starting_capital: float = 10000.0  # USD
    maker_fee_pct: float = 0.16        # Kraken maker fee
    taker_fee_pct: float = 0.26        # Kraken taker fee


@dataclass
class AgentConfig:
    """v4.0 Multi-agent system configuration."""
    # Opus PM decision cycle (seconds) — default every 2 hours
    pm_interval_seconds: int = 7200
    # Agent fast loop (seconds) — how often Haiku agents run
    agent_interval_seconds: int = 300
    # Max emergency wakes per day
    max_wakes_per_day: int = 6
    # Minimum cooldown between wakes (seconds)
    wake_cooldown_seconds: int = 1800


@dataclass
class BotConfig:
    """Top-level bot configuration."""
    # Trading mode: "paper" or "live"
    mode: str = "paper"
    # Database file for persistence
    db_path: str = "bot_data.db"
    # Log level
    log_level: str = "INFO"
    # Kraken OHLCV candle interval in minutes (1, 5, 15, 30, 60, 240, 1440)
    candle_interval: int = 15
    # AI-powered strategy (requires Anthropic API key)
    use_ai_strategy: bool = True
    anthropic_api_key: str = ""
    # v4.0 Model hierarchy
    ai_model: str = "claude-opus-4-6"                # PM brain (Opus)
    haiku_model: str = "claude-haiku-4-5-20251001"   # Agent workers (Haiku)
    chat_model: str = "claude-haiku-4-5-20251001"    # Dashboard chat (Haiku)
    # Kraken
    kraken: KrakenConfig = field(default_factory=KrakenConfig)
    # Strategy
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    # Paper trading
    paper: PaperTradingConfig = field(default_factory=PaperTradingConfig)
    # v4.0 Multi-agent system
    agents: AgentConfig = field(default_factory=AgentConfig)


def load_config() -> BotConfig:
    """Load configuration from environment variables."""
    config = BotConfig(
        mode=os.getenv("BOT_MODE", "paper"),
        db_path=os.getenv("BOT_DB_PATH", "bot_data.db"),
        log_level=os.getenv("BOT_LOG_LEVEL", "INFO"),
        candle_interval=int(os.getenv("BOT_CANDLE_INTERVAL", "15")),
        use_ai_strategy=os.getenv("USE_AI_STRATEGY", "true").lower() == "true",
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        ai_model=os.getenv("AI_MODEL", "claude-opus-4-6"),
        haiku_model=os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001"),
        chat_model=os.getenv("CHAT_MODEL", "claude-haiku-4-5-20251001"),
        kraken=KrakenConfig(
            api_key=os.getenv("KRAKEN_API_KEY", ""),
            api_secret=os.getenv("KRAKEN_API_SECRET", ""),
            symbol=os.getenv("KRAKEN_SYMBOL", "XBTUSD"),
            display_symbol=os.getenv("KRAKEN_DISPLAY_SYMBOL", "BTC/USD"),
        ),
        strategy=StrategyConfig(
            ema_fast_period=int(os.getenv("EMA_FAST_PERIOD", "9")),
            ema_slow_period=int(os.getenv("EMA_SLOW_PERIOD", "21")),
            rsi_period=int(os.getenv("RSI_PERIOD", "14")),
            rsi_overbought=float(os.getenv("RSI_OVERBOUGHT", "70")),
            rsi_oversold=float(os.getenv("RSI_OVERSOLD", "30")),
            bb_period=int(os.getenv("BB_PERIOD", "20")),
            bb_std_dev=float(os.getenv("BB_STD_DEV", "2.0")),
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "3.0")),
            take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "6.0")),
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "2.0")),
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", "30.0")),
            scan_interval_seconds=int(os.getenv("SCAN_INTERVAL_SECONDS", "60")),
            history_bars=int(os.getenv("HISTORY_BARS", "100")),
        ),
        paper=PaperTradingConfig(
            starting_capital=float(os.getenv("PAPER_STARTING_CAPITAL", "10000")),
            maker_fee_pct=float(os.getenv("PAPER_MAKER_FEE_PCT", "0.16")),
            taker_fee_pct=float(os.getenv("PAPER_TAKER_FEE_PCT", "0.26")),
        ),
        agents=AgentConfig(
            pm_interval_seconds=int(os.getenv("PM_INTERVAL_SECONDS", "7200")),
            agent_interval_seconds=int(os.getenv("AGENT_INTERVAL_SECONDS", "300")),
            max_wakes_per_day=int(os.getenv("MAX_WAKES_PER_DAY", "6")),
            wake_cooldown_seconds=int(os.getenv("WAKE_COOLDOWN_SECONDS", "1800")),
        ),
    )
    return config
