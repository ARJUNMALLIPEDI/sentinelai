"""Engine and session factory.

These are built inside the app lifespan and stored on ``app.state`` rather than
as module globals. That keeps import side-effect free (importing the app never
opens a connection) and lets tests build an app against a different database.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # transparently replace connections killed by a DB restart
    )


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit, which matters
    # in async code where lazy-refreshing an attribute would need an await.
    return async_sessionmaker(engine, expire_on_commit=False)
