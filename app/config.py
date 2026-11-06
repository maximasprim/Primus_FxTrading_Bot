"""
Central application configuration.

All tunable values referenced throughout the spec (risk defaults, minimum entry
score, alert channel routing, etc.) live here so they can be changed in one place
or overridden via environment variables / .env.
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "staging", "production"] = "development"
    api_secret_key: str = "change-me"
    # When True (default) every dashboard/API route except the TradingView
    # webhook, the Meta/Twilio inbound callbacks and /health requires an
    # `X-API-Key` header equal to API_SECRET_KEY. Only set False for a
    # throwaway local sandbox.
    api_auth_enabled: bool = True
    tradingview_webhook_secret: str = "change-me"

    # --- Database ---
    database_url: str = "postgresql+asyncpg://forex_user:forex_pass@localhost:5432/forex_ai"
    database_url_sync: str = "postgresql+psycopg2://forex_user:forex_pass@localhost:5432/forex_ai"
    # Set True when database_url points at Supabase's *transaction* pooler
    # (port 6543) - asyncpg must disable prepared-statement caching against
    # pgbouncer in transaction mode, or queries fail intermittently.
    database_use_pgbouncer: bool = False
    # Supabase (and most managed Postgres) requires SSL; local Docker/SQLite don't.
    database_ssl_required: bool = False

    # --- Redis / Celery ---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"

    # --- Market data ---
    market_data_provider: str = "stub"
    market_data_api_key: str = ""
    oanda_account_id: str = ""
    oanda_environment: str = "practice"  # "practice" (demo) or "live" -- defaults to demo
    economic_calendar_api_key: str = ""

     # --- MT5 (execution only, not market data) ---
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_symbol_suffix: str = ""
    # Optional full path to terminal64.exe -- only needed if several MT5
    # terminals are installed on the machine.
    mt5_terminal_path: str = ""

    # --- Scalper (app/scalper) ---
    # Folder for the scalper's saved settings + trade log (JSONL).
    scalper_state_dir: str = "data"

    # --- AI analysis ---
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    # Seeds app/ai/settings_manager.py's runtime AISettingsManager on startup.
    # Changeable afterwards from the frontend (Settings modal) without a
    # restart -- this is just the default for a fresh process.
    ai_provider: str = "gemini"  # "anthropic" | "gemini" | "openai"

    # --- Market hours (informational) ---
    # Forex/XAU trades ~24h, Sunday 17:00 to Friday 17:00 America/New_York.
    # This list is a simple, editable starting point for full-day closures
    # on top of that weekly schedule -- not a broker-accurate holiday
    # calendar, since those vary by broker and shift year to year. Comma-
    # separated MM-DD values.
    market_holidays_mmdd: str = "01-01,12-25"

    # --- Alert channels ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    whatsapp_api_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""  # separate from the API token -- used only to verify inbound webhook signatures
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_from: str = ""
    alert_email_to: str = ""

    # --- Risk management defaults (spec §17) ---
    risk_per_trade_pct: float = 0.75          # 0.5%-1% recommended range
    max_daily_loss_pct: float = 2.0
    max_weekly_loss_pct: float = 5.0
    max_consecutive_losses: int = 3
    min_risk_reward: float = 2.0

    # --- Confluence / alerting (spec §12) ---
    minimum_entry_score: int = 80             # A+ / A threshold to trigger Stage 3 alert
    watchlist_score: int = 60                 # Below this -> NO TRADE, no alert at all

    # --- Instruments (spec §1) ---
    initial_instruments: tuple[str, ...] = ("EUR/USD", "GBP/USD", "XAU/USD")
    future_instruments: tuple[str, ...] = (
        "USD/JPY", "USD/CHF", "AUD/USD", "NZD/USD", "USD/CAD", "XAG/USD",
    )

    # --- News risk windows (spec §9), in minutes ---
    news_block_minutes_before: int = 30
    news_block_minutes_after: int = 15

    # --- Trading mode & auto-execution (execution-feature spec §1-2, §21) ---
    # Default-safe configuration: analysis/alerts on, everything else off.
    # This must NEVER be flipped to "on" automatically by a restart, deploy,
    # reconnect, config change, or error recovery (spec §21).
    default_global_trading_mode: str = "ALERT_MODE"   # ANALYSIS_ONLY | ALERT_MODE | MANUAL_APPROVAL | FULL_AUTO
    default_auto_trading_enabled: bool = False
    default_paper_trading_enabled: bool = True

    broker_adapter: str = "paper"  # "paper" | "mt5" | future adapters
    max_allowed_slippage_pips: float = 2.0
    approval_expiry_seconds: int = 60

    # Auto-execution minimum requirements (execution-feature spec §4) -
    # separate/stricter from the alerting threshold in minimum_entry_score.
    auto_execution_minimum_score: int = 80
    auto_execution_min_risk_reward: float = 2.0

    # Safety shutdown thresholds (execution-feature spec §10) - reuse the
    # risk-management values above (max_daily_loss_pct etc.) plus:
    max_drawdown_pct: float = 8.0
    market_data_staleness_multiplier: float = 3.0

    # --- Notification engine (notification-feature spec §8-9) ---
    notification_max_retries: int = 2
    notification_fallback_enabled: bool = True  # WhatsApp fails + HIGH priority -> SMS -> Email

    # --- CORS ---
    cors_allowed_origins: str = (
    "https://primusfx.vercel.app,"
    "http://localhost:5173,"
    "http://localhost:3000,"
    "http://localhost:5174"
    )

    @property
    def cors_allowed_origins_list(self) -> list[str]:
       if self.cors_allowed_origins.strip() == "*":
           return ["*"]
       return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
