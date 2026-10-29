"""Demo data seeding."""

import json
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import OrderStatus
from app.core.time import utcnow
from app.repositories.models import (
    AuditLogRow,
    ConversationMemoryRow,
    IdempotencyRow,
    OrderItemRow,
    OrderRow,
    PendingActionRow,
    ProductRow,
    RefundRow,
    ReturnRow,
    SessionRow,
)


async def seed_demo_data(session: AsyncSession, *, reset: bool = False) -> None:
    """Fill missing demo rows, optionally restoring orders and their related activity.

    The caller commits the reset and seed together in one transaction.
    """
    now = utcnow()
    products = [
        ProductRow(
            sku="SKU-HEADSET",
            name="降噪耳机 Pro",
            category="数码",
            price_cents=29900,
            inventory=42,
            tags="耳机,降噪",
            description="头戴式主动降噪耳机",
        ),
        ProductRow(
            sku="SKU-MUG-CUSTOM",
            name="定制马克杯",
            category="家居",
            price_cents=3900,
            inventory=200,
            tags="定制,杯子",
            description="支持印字的定制杯，定制商品不适用7天无理由",
        ),
        ProductRow(
            sku="SKU-TEE",
            name="纯棉T恤",
            category="服饰",
            price_cents=7900,
            inventory=0,
            tags="衣服,基础款",
            description="基础纯棉短袖",
        ),
    ]
    for product in products:
        if await session.get(ProductRow, product.sku) is None:
            session.add(product)
    orders = [
        OrderRow(
            order_id="ORD10001",
            user_id="u_demo",
            status=OrderStatus.SHIPPED.value,
            total_cents=29900,
            created_at=now - timedelta(days=3),
            paid_at=now - timedelta(days=3),
            shipped_at=now - timedelta(days=2),
            tracking_no="SF1234567890",
            receiver_city="上海",
        ),
        OrderRow(
            order_id="ORD10002",
            user_id="u_demo",
            status=OrderStatus.DELIVERED.value,
            total_cents=3900,
            created_at=now - timedelta(days=20),
            paid_at=now - timedelta(days=20),
            shipped_at=now - timedelta(days=19),
            delivered_at=now - timedelta(days=12),
            tracking_no="YT998877",
            receiver_city="北京",
        ),
        OrderRow(
            order_id="ORD10003",
            user_id="u_demo",
            status=OrderStatus.PAID.value,
            total_cents=7900,
            created_at=now - timedelta(hours=5),
            paid_at=now - timedelta(hours=5),
            receiver_city="杭州",
        ),
    ]
    demo_order_ids = [order.order_id for order in orders]
    if reset:
        for model in (RefundRow, ReturnRow, OrderItemRow):
            await session.execute(delete(model).where(model.order_id.in_(demo_order_ids)))
        await session.execute(delete(AuditLogRow).where(AuditLogRow.payload.in_(demo_order_ids)))
        # Old successful responses must not skip writes in the next demonstration.
        for row in (await session.scalars(select(IdempotencyRow))).all():
            if json.loads(row.response_json).get("order_id") in demo_order_ids:
                await session.delete(row)
        for action_row in (
            await session.scalars(
                select(PendingActionRow).where(PendingActionRow.user_id == "u_demo")
            )
        ).all():
            if (
                json.loads(action_row.action_json).get("arguments", {}).get("order_id")
                in demo_order_ids
            ):
                await session.delete(action_row)
        # Demo business facts are reset; keep historical text, but discard any
        # unfinished action carried over from a previous demonstration.
        for memory_row in (
            await session.scalars(
                select(ConversationMemoryRow).join(SessionRow).where(SessionRow.user_id == "u_demo")
            )
        ).all():
            data = json.loads(memory_row.context_json)
            if data.get("order_id") in demo_order_ids or (
                data.get("awaiting_order") and data.get("pending_tool")
            ):
                data.update(pending_tool=None, pending_query="", awaiting_order=False)
                memory_row.context_json = json.dumps(data, ensure_ascii=False)
    for order in orders:
        existing = await session.get(OrderRow, order.order_id)
        if existing is None:
            session.add(order)
        elif reset:
            for column in OrderRow.__table__.columns:
                setattr(existing, column.key, getattr(order, column.key))
    items = [
        OrderItemRow(
            order_id="ORD10001",
            sku="SKU-HEADSET",
            name="降噪耳机 Pro",
            quantity=1,
            unit_price_cents=29900,
            customized=False,
        ),
        OrderItemRow(
            order_id="ORD10002",
            sku="SKU-MUG-CUSTOM",
            name="定制马克杯",
            quantity=1,
            unit_price_cents=3900,
            customized=True,
        ),
        OrderItemRow(
            order_id="ORD10003",
            sku="SKU-TEE",
            name="纯棉T恤",
            quantity=1,
            unit_price_cents=7900,
            customized=False,
        ),
    ]
    existing_items = set(
        (await session.execute(select(OrderItemRow.order_id, OrderItemRow.sku))).all()
    )
    session.add_all(item for item in items if (item.order_id, item.sku) not in existing_items)
