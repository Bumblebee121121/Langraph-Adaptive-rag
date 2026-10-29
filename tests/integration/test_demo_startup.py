from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.config.settings import Settings
from app.repositories.models import OrderRow
from app.services.container import build_container


@pytest.mark.parametrize(
    ("app_env", "enabled", "seed", "expected"),
    [
        ("development", True, None, True),
        ("development", False, None, False),
        ("testing", True, None, False),
        ("production", True, None, False),
        ("testing", False, True, True),
        ("development", True, False, False),
    ],
)
async def test_startup_demo_data_policy(monkeypatch, tmp_path, app_env, enabled, seed, expected):
    # Keep the real database and container composition; retrieval is unrelated to seeding.
    monkeypatch.setattr(
        "app.services.container.build_retrieval",
        AsyncMock(return_value=(Mock(), Mock(), Mock())),
    )
    settings = Settings(
        _env_file=None,
        app_env=app_env,
        seed_demo_on_startup=enabled,
        reset_demo_on_startup=True,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'startup.db').as_posix()}",
        deepseek_api_key="",
        redis_url="",
        qdrant_url="",
    )
    container = await build_container(settings, seed=seed)
    try:
        async with container.session_factory() as session:
            orders = (await session.scalars(select(OrderRow))).all()
            assert {order.order_id for order in orders} == (
                {"ORD10001", "ORD10002", "ORD10003"} if expected else set()
            )
            assert all(order.user_id == "u_demo" for order in orders)
            if expected:
                for order in orders:
                    order.status = "cancelled"
                await session.commit()
    finally:
        await container.aclose()

    container = await build_container(settings, seed=seed)
    try:
        async with container.session_factory() as session:
            order = await session.get(OrderRow, "ORD10003")
            if expected:
                assert order.status == ("paid" if app_env == "development" else "cancelled")
            else:
                assert order is None
    finally:
        await container.aclose()
