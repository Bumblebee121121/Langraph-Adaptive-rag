"""Async engine, session factory and schema initialisation."""

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config.settings import Settings
from app.repositories.models import Base


def create_engine(settings: Settings) -> AsyncEngine:
    connect_args: dict[str, object] = {}
    kwargs: dict[str, object] = {
        "echo": False,
        "pool_pre_ping": True,
        "connect_args": connect_args,
    }
    if settings.use_sqlite:
        Path("data/local").mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
        if ":memory:" in settings.database_url:
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
        kwargs["pool_timeout"] = settings.db_pool_timeout
        kwargs["pool_recycle"] = 1800
    return create_async_engine(settings.database_url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            # Serialize schema inspection and repair across simultaneous startups.
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await connection.run_sync(Base.metadata.create_all)
        if connection.dialect.name == "sqlite":
            await connection.run_sync(_upgrade_sqlite_memory)


def _upgrade_sqlite_memory(connection: Connection) -> None:
    """Repair the intermediate memory schema without resetting stored history.

    create_all only creates missing tables. A database opened by an earlier
    version may already have conversation_memory without the forgetting cursor.
    """
    columns = {column["name"] for column in inspect(connection).get_columns("conversation_memory")}
    if "forgotten_message_id" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE conversation_memory "
            "ADD COLUMN forgotten_message_id INTEGER NOT NULL DEFAULT 0"
        )


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
