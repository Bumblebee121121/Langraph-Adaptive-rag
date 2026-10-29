import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from alembic.config import Config
from app.config.settings import clear_settings_cache
from app.main import create_app
from app.repositories.db import init_schema
from app.repositories.models import Base, ConversationMemoryRow, MessageRow, SessionRow


async def make_legacy_database(engine):
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(
                conn,
                tables=[
                    table
                    for table in Base.metadata.sorted_tables
                    if table.name != "conversation_memory"
                ],
            )
        )
        await connection.exec_driver_sql(
            "CREATE TABLE conversation_memory (session_id VARCHAR(64) PRIMARY KEY, "
            "context_json TEXT NOT NULL, summary_message_id INTEGER NOT NULL)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO sessions(session_id,user_id,summary,created_at,updated_at) "
            "VALUES ('legacy','u_demo','remembered summary',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO messages(session_id,role,content,created_at) "
            "VALUES ('legacy','user','preserved history',CURRENT_TIMESTAMP)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO conversation_memory(session_id,context_json,summary_message_id) "
            "VALUES ('legacy','{\"order_id\":\"ORD10003\"}',42)"
        )


@pytest.mark.parametrize("concurrent", [False, True])
async def test_old_memory_schema_upgrades_without_losing_data(tmp_path, concurrent):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    engine = create_async_engine(url)
    second_engine = create_async_engine(url)
    try:
        await make_legacy_database(engine)
        if concurrent:
            await asyncio.gather(init_schema(engine), init_schema(second_engine))
        else:
            await init_schema(engine)
        await init_schema(engine)
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda conn: inspect(conn).get_columns("conversation_memory")
            )
            assert sum(c["name"] == "forgotten_message_id" for c in columns) == 1
        async with async_sessionmaker(engine)() as db:
            row = await db.get(ConversationMemoryRow, "legacy")
            assert row.context_json == '{"order_id":"ORD10003"}'
            assert row.summary_message_id == 42
            assert row.forgotten_message_id == 0
            assert (await db.get(SessionRow, "legacy")).summary == "remembered summary"
            assert (await db.scalar(select(MessageRow))).content == "preserved history"
    finally:
        await second_engine.dispose()
        await engine.dispose()


async def test_application_starts_with_an_intermediate_memory_database(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'startup.db').as_posix()}"
    engine = create_async_engine(url)
    await make_legacy_database(engine)
    await engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    clear_settings_cache()
    try:
        with TestClient(create_app()) as client:
            assert client.get("/api/v1/ready").status_code == 200
            response = client.post(
                "/api/v1/chat",
                json={
                    "message": "我的订单号是什么",
                    "session_id": "legacy",
                    "user_id": "u_demo",
                },
            )
            assert response.status_code == 200
            assert "ORD10003" in response.json()["answer"]
            history = client.get("/api/v1/sessions/legacy?user_id=u_demo").json()
            assert history["messages"][0]["content"] == "preserved history"
    finally:
        clear_settings_cache()


async def test_alembic_repairs_an_intermediate_0003_database(tmp_path, monkeypatch):
    # Alembic's CLI logging setup disables existing app loggers by default.
    # Keep this in-process migration test from changing later tests' log capture.
    monkeypatch.setattr("logging.config.fileConfig", lambda *args, **kwargs: None)
    path = tmp_path / "migrate.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        await make_legacy_database(engine)
        await engine.dispose()
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
        command.stamp(config, "0003")
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        async with async_sessionmaker(engine)() as db:
            memory = await db.get(ConversationMemoryRow, "legacy")
            assert memory.forgotten_message_id == 0
            assert memory.summary_message_id == 42
            assert (await db.get(SessionRow, "legacy")).summary == "remembered summary"
    finally:
        await engine.dispose()
