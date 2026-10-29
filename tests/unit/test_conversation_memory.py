import json

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.nodes.generate import generate_node
from app.agents.nodes.intent import IntentClassifier
from app.agents.nodes.planner import Planner, planner_node
from app.config.settings import Settings
from app.memory.context import model_history, resolve_context
from app.memory.session import restore_memory
from app.memory.summarizer import ConversationSummarizer
from app.providers.fake import FakeLLMProvider, OfflineLLMProvider
from app.repositories.db import init_schema
from app.repositories.memory_repository import MemoryRepository
from app.repositories.models import ConversationMemoryRow
from app.repositories.session_repository import SessionRepository
from app.schemas.chat import ChatMessage
from app.schemas.memory import ConversationContext
from app.tools.aftersale_tools import CancelOrderTool
from app.tools.registry import ToolRegistry


def test_slot_filling_and_reference_resolution_do_not_execute_actions():
    first = resolve_context({"user_query": "我要把马克杯退掉，太丑了"})
    assert first["conversation_context"].awaiting_order
    second = resolve_context(
        {"user_query": "订单号是ORD10002", "conversation_context": first["conversation_context"]}
    )
    assert second["requested_tool"] == "create_return_request"
    assert "ORD10002" in second["resolved_query"]
    assert "太丑了" in second["resolved_query"]
    assert not second["conversation_context"].awaiting_order
    assert (
        resolve_context(
            {"user_query": "帮我取消它", "conversation_context": second["conversation_context"]}
        )["conversation_context"].order_id
        == "ORD10002"
    )
    assert resolve_context({"user_query": "ORD10003"})["requested_tool"] is None


def test_context_handles_order_switches_negations_and_ambiguity():
    context = ConversationContext(order_id="ORD10003", last_query="查订单 ORD10003 的 T恤")
    changed = resolve_context({"user_query": "取消订单 ORD10001", "conversation_context": context})
    assert changed["conversation_context"].order_id == "ORD10001"
    assert changed["requested_tool"] == "cancel_order"
    stopped = resolve_context({"user_query": "不要取消它", "conversation_context": context})
    assert stopped["requested_tool"] is None
    assert stopped["dismiss_action"]
    assert (
        not resolve_context({"user_query": "帮我把马克杯退掉", "conversation_context": context})[
            "conversation_context"
        ].order_id
        == "ORD10003"
    )
    ambiguous = resolve_context(
        {"user_query": "取消订单 ORD10001 和 ORD10003", "conversation_context": context}
    )
    assert ambiguous["extra"]["ambiguous_order"]
    assert ambiguous["conversation_context"].order_id is None


async def test_models_receive_history_and_summary_without_authorizing_writes():
    llm = FakeLLMProvider(
        {
            "": '{"steps":[{"step_id":1,"goal":"bad write","tool_name":"cancel_order","arguments":{"order_id":"ORD99999"}}],"answer":"ok","citation_chunk_ids":[],"refused":false,"label":"faq_policy","confidence":0.9,"prompt_version":"t"}'
        }
    )
    state = {
        "user_query": "能取消吗？",
        "resolved_query": "ORD10003 能取消吗？",
        "context_resolved": True,
        "requested_tool": None,
        "summary": "用户喜欢蓝色。",
        "messages": [ChatMessage(role="user", content="我的名字是小王，订单 ORD10003")],
    }
    await IntentClassifier(llm).classify(state["resolved_query"], history=model_history(state))
    registry = ToolRegistry()
    registry.register(CancelOrderTool())
    result = await planner_node(state, Planner(llm, registry))
    assert result["pending_action"] is None
    assert result["plan"].steps == []
    await generate_node(state, llm)
    assert len(llm.calls) == 3
    for messages in llm.calls:
        text = json.dumps(messages, ensure_ascii=False)
        assert "小王" in text
        assert "喜欢蓝色" in text
        assert "ORD10003" in text


async def test_summary_rolls_forward_and_restores_across_new_repository_instances():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, memory_recent_messages=4)
    summarizer = ConversationSummarizer(OfflineLLMProvider())
    try:
        async with factory.begin() as db:
            repo = SessionRepository(db)
            session = await repo.get_or_create("history", "owner")
            for text in [
                "我的名字是小王，喜欢蓝色",
                "我的订单是 ORD10003",
                *[f"第 {i} 条历史" for i in range(10)],
            ]:
                await repo.append_message("history", "user", text)
            window, summary, context = await restore_memory(db, session, settings, summarizer)
            assert len(window) == 4
            assert "小王" in summary
            assert context.order_id == "ORD10003"
            memory = await db.get(ConversationMemoryRow, "history")
            cursor = memory.summary_message_id
        async with factory.begin() as db:
            session = await SessionRepository(db).get_or_create("history", "owner")
            restored, summary, context = await restore_memory(db, session, settings, summarizer)
            assert restored == window
            assert "小王" in summary
            assert context.order_id == "ORD10003"
            assert (await MemoryRepository(db).get("history")).summary_message_id == cursor
            await SessionRepository(db).append_message("history", "user", "新增：想买绿色款")
            await restore_memory(db, session, settings, summarizer)
            assert (await MemoryRepository(db).get("history")).summary_message_id > cursor
    finally:
        await engine.dispose()


async def test_summary_provider_failure_preserves_previous_information():
    summarizer = ConversationSummarizer(FakeLLMProvider())
    summary = await summarizer.summarize(
        [ChatMessage(role="user", content="现在改成绿色")],
        previous_summary="名字小王，之前喜欢蓝色",
    )
    assert "名字小王" in summary
    assert "现在改成绿色" in summary


async def test_history_window_is_bounded_before_llm_calls():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, memory_context_tokens=512, memory_recent_messages=10)
    try:
        async with factory.begin() as db:
            repo = SessionRepository(db)
            session = await repo.get_or_create("bounded", "owner")
            for i in range(30):
                await repo.append_message("bounded", "user", f"message {i}: " + "内容" * 2000)
            window, summary, _ = await restore_memory(
                db, session, settings, ConversationSummarizer(OfflineLLMProvider())
            )
            assert len(summary) + sum(len(m.content) for m in window) <= 512
    finally:
        await engine.dispose()
