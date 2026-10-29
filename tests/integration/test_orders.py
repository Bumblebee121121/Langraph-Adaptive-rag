import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.request_scope import request_session
from app.repositories.db import init_schema
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.models import (
    AuditLogRow,
    IdempotencyRow,
    OrderItemRow,
    OrderRow,
    ProductRow,
    RefundRow,
    ReturnRow,
)
from app.repositories.order_repository import OrderRepository
from app.services.seed import seed_demo_data
from app.tools.aftersale_tools import CreateRefundRequestTool
from app.tools.base import ToolContext
from app.tools.order_tools import GetOrderTool


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await init_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        await seed_demo_data(session)
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_refund_is_idempotent_and_transactional(session_factory) -> None:
    async with request_session(session_factory) as session:
        repo = OrderRepository(session)
        first = await repo.create_refund(order_id="ORD10003", reason="不想要了", trace_id="tr_1")
        keys = IdempotencyRepository(session)
        await keys.put("act_1", "fp-refund", first)
        again = await keys.get_if_matches("act_1", "fp-refund")
        assert again == first
        await session.commit()

    async with request_session(session_factory) as session:
        refunds = await OrderRepository(session).get_refunds("ORD10003")
        assert len(refunds) == 1


@pytest.mark.asyncio
async def test_order_is_scoped_to_owner(session_factory) -> None:
    async with request_session(session_factory) as session:
        session.add(OrderRow(order_id="ORD99999", user_id="other", status="paid", total_cents=100))
        await session.flush()
        tool = GetOrderTool(OrderRepository(session))
        ctx = ToolContext(user_id="someone_else", session_id="s", trace_id="t")
        with pytest.raises(Exception, match="order not found"):
            await tool.execute(tool.parse({"order_id": "ORD10001"}), ctx)

        for user_id in ("anonymous", "u_demo"):
            ctx = ToolContext(user_id=user_id, session_id="s", trace_id="t")
            with pytest.raises(Exception, match="order not found"):
                await tool.execute(tool.parse({"order_id": "ORD99999"}), ctx)


@pytest.mark.asyncio
async def test_seed_fills_missing_rows_without_overwriting_existing_orders(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(
            OrderItemRow.__table__.delete().where(OrderItemRow.order_id == "ORD10003")
        )
        await session.delete(await session.get(OrderRow, "ORD10003"))
        await session.delete(await session.get(ProductRow, "SKU-TEE"))
        await session.commit()

    async with session_factory() as session:
        await seed_demo_data(session)
        await seed_demo_data(session)
        await session.commit()
        assert await session.get(OrderRow, "ORD10003") is not None
        assert await session.get(ProductRow, "SKU-TEE") is not None
        count = await session.scalar(
            select(func.count())
            .select_from(OrderItemRow)
            .where(OrderItemRow.order_id == "ORD10003")
        )
        assert count == 1


@pytest.mark.asyncio
async def test_refund_tool_persists_via_repository(session_factory) -> None:
    async with request_session(session_factory) as session:
        tool = CreateRefundRequestTool(OrderRepository(session))
        ctx = ToolContext(user_id="u_demo", session_id="s", trace_id="t")
        result = await tool.run({"order_id": "ORD10003", "reason": "质量问题"}, ctx)
        assert result.success
        await session.commit()
    async with request_session(session_factory) as session:
        refunds = await OrderRepository(session).get_refunds("ORD10003")
        assert refunds[0].status == "requested"


async def test_reset_restores_demo_orders_and_clears_only_their_activity(session_factory) -> None:
    async with session_factory() as session:
        repo = OrderRepository(session)
        await repo.cancel_order("ORD10003", trace_id="demo_cancel")
        await repo.create_refund(order_id="ORD10001", reason="演示退款", trace_id="demo_refund")
        await repo.create_return(order_id="ORD10002", reason="演示退货", trace_id="demo_return")
        await IdempotencyRepository(session).put("demo_key", "demo_fp", {"order_id": "ORD10003"})
        session.add(
            OrderRow(order_id="OTHER001", user_id="other", status="cancelled", total_cents=100)
        )
        await session.flush()
        await repo.create_refund(order_id="OTHER001", reason="保留", trace_id="other_refund")
        await repo.create_return(order_id="OTHER001", reason="保留", trace_id="other_return")
        await IdempotencyRepository(session).put("other_key", "other_fp", {"order_id": "OTHER001"})
        await session.commit()

    async with session_factory() as session:
        await seed_demo_data(session, reset=True)
        await seed_demo_data(session, reset=True)
        await session.commit()

    async with session_factory() as session:
        orders = (await session.scalars(select(OrderRow).order_by(OrderRow.order_id))).all()
        assert {row.order_id: row.status for row in orders} == {
            "ORD10001": "shipped",
            "ORD10002": "delivered",
            "ORD10003": "paid",
            "OTHER001": "refunding",
        }
        order = await session.get(OrderRow, "ORD10003")
        assert order.paid_at is not None
        assert order.shipped_at is None
        assert order.delivered_at is None
        assert await session.scalar(select(func.count()).select_from(OrderItemRow)) == 3
        for model in (RefundRow, ReturnRow):
            rows = (await session.scalars(select(model))).all()
            assert [row.order_id for row in rows] == ["OTHER001"]
        logs = (await session.scalars(select(AuditLogRow))).all()
        assert all(row.payload == "OTHER001" for row in logs)
        assert len(logs) == 2
        assert await session.get(IdempotencyRow, "demo_key") is None
        assert await session.get(IdempotencyRow, "other_key") is not None
