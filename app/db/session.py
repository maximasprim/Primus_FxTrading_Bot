"""
Async SQLAlchemy engine/session factory.

Usage (in a route):
    from app.db.session import get_db_session

    @router.get(...)
    async def handler(db: AsyncSession = Depends(get_db_session)):
        ...

Supabase notes (see SETUP.md for the full walkthrough):
  - Supabase requires SSL - set DATABASE_SSL_REQUIRED=true in .env.
  - If DATABASE_URL points at Supabase's *transaction* pooler (port 6543),
    set DATABASE_USE_PGBOUNCER=true. asyncpg's server-side prepared
    statement cache is incompatible with pgbouncer in transaction mode
    (connections are shared/rotated between transactions), so it must be
    disabled -- that's what `statement_cache_size=0` below does. This is
    NOT needed for the *session* pooler (port 5432) or a direct connection.
"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

_connect_args: dict = {}
if settings.database_use_pgbouncer:
    _connect_args["statement_cache_size"] = 0
if settings.database_ssl_required:
    _connect_args["ssl"] = "require"

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args=_connect_args,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
