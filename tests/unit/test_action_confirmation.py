import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.nodes.planner import Planner, planner_node
from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError
from app.providers.fake import OfflineLLMProvider
from app.repositories.db import init_schema
from app.repositories.memory_repository import MemoryRepository
from app.repositories.session_repository import SessionRepository
from app.schemas.chat import ActionProposal, ChatRequest
from app.services.chat_service import ChatService
from app.tools.aftersale_tools import CancelOrderTool
from app.tools.order_tools import GetOrderTool
from app.tools.registry import ToolRegistry


def _action() -> ActionProposal:
    return ActionProposal(
        action_id="act_test",
        tool_name="cancel_order",
        arguments={"order_id": "ORD10003"},
        risk=ToolRisk.DESTRUCTIVE,
        summary="确认后将取消订单 ORD10003。",
    )


@pytest.mark.asyncio
async def test_cancellation_needs_explicit_request_and_confirmation() -> None:
    registry = ToolRegistry()
    registry.register(GetOrderTool())
    registry.register(CancelOrderTool())
    planner = Planner(OfflineLLMProvider(), registry)

    proposal = await planner_node({"user_query": "取消订单 ORD10003"}, planner)
    assert proposal["pending_action"].tool_name == "cancel_order"
    assert "cancel_order" not in proposal["pending_action"].summary

    question = await planner_node({"user_query": "取消订单有什么规则？"}, planner)
    assert question["pending_action"] is None

    confirmed = await planner_node(
        {"user_query": "确认执行", "confirmed_action": _action()}, planner
    )
    assert len(confirmed["plan"].steps) == 1
    assert confirmed["plan"].steps[0].arguments == {"order_id": "ORD10003"}


async def test_confirmation_is_bound_to_session_and_user() -> None:
    service = ChatService.__new__(ChatService)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            await SessionRepository(db).get_or_create("sess_demo", "u_demo")
            await MemoryRepository(db).remember_action("sess_demo", "u_demo", _action())
            await db.flush()
            confirmed = ChatRequest(
                message="确认执行",
                user_id="u_demo",
                session_id="sess_demo",
                confirm_action_id="act_test",
            )
            assert await service._resolve_confirmation(confirmed, "sess_demo", db) == _action()
            forged = confirmed.model_copy(update={"user_id": "someone_else"})
            with pytest.raises(InvalidRequestError):
                await service._resolve_confirmation(forged, "sess_demo", db)
            with pytest.raises(InvalidRequestError):
                await service._resolve_confirmation(confirmed, "another_session", db)
    finally:
        await engine.dispose()
