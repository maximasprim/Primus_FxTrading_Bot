"""
Alembic environment script.

Pulls the DB URL from app.config.get_settings() (i.e. your .env) instead of
duplicating it in alembic.ini, and points autogenerate at Base.metadata from
app/models/db_models.py so `alembic revision --autogenerate` picks up every
table defined there (candles, signals, signal_events, backtest_runs,
trade_journal, trading_mode_audit, instrument_config, ...).
"""
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the project root importable when Alembic runs from app/db/migrations/
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.config import get_settings  # noqa: E402
from app.models.db_models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_sync)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
