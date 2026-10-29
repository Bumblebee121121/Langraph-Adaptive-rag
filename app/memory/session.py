"""Restore recent turns and roll older messages into a persisted summary."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.memory.budget import estimate_tokens
from app.memory.context import resolve_context
from app.memory.summarizer import ConversationSummarizer
from app.repositories.memory_repository import MemoryRepository
from app.repositories.models import MessageRow, SessionRow
from app.schemas.chat import ChatMessage
from app.schemas.memory import ConversationContext


async def restore_memory(
    db: AsyncSession, session: SessionRow, settings: Settings, summarizer: ConversationSummarizer
):
    memory = await MemoryRepository(db).get(session.session_id)
    context = ConversationContext.model_validate_json(memory.context_json)
    recent = list(
        reversed(
            (
                await db.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.session_id == session.session_id,
                        MessageRow.id > memory.forgotten_message_id,
                    )
                    .order_by(MessageRow.id.desc())
                    .limit(settings.memory_recent_messages)
                )
            ).all()
        )
    )
    budget = min(settings.memory_context_tokens, max(64, settings.token_budget // 2))
    while len(recent) > 2 and sum(estimate_tokens(m.content) for m in recent) > budget * 2 // 3:
        recent.pop(0)
    boundary = recent[0].id if recent else memory.summary_message_id + 1
    summary = session.summary or ""
    while True:
        older = (
            await db.scalars(
                select(MessageRow)
                .where(
                    MessageRow.session_id == session.session_id,
                    MessageRow.id > memory.summary_message_id,
                    MessageRow.id < boundary,
                )
                .order_by(MessageRow.id)
                .limit(8)
            )
        ).all()
        if not older:
            break
        if memory.context_json == "{}":
            for old in older:
                if old.role == "user":
                    context = resolve_context(
                        {"user_query": old.content, "conversation_context": context}
                    )["conversation_context"]
        summary = await summarizer.summarize(
            [ChatMessage(role=m.role, content=m.content) for m in older],  # type: ignore[arg-type]
            previous_summary=summary,
        )
        memory.summary_message_id = older[-1].id
        session.summary = summary
    # Upgrade existing sessions without trusting assistant-generated order numbers.
    if memory.context_json == "{}":
        for row in recent:
            if row.role == "user":
                context = resolve_context(
                    {"user_query": row.content, "conversation_context": context}
                )["conversation_context"]
        memory.context_json = context.model_dump_json()
    summary = summary[: min(settings.memory_summary_max_chars, budget // 3)]
    remaining_chars = max(0, budget - len(summary) - 16)
    window: list[ChatMessage] = []
    # A conservative one-character-per-token cap keeps CJK and ASCII history bounded.
    for row in reversed(recent):
        if remaining_chars <= 0:
            break
        content = row.content[:remaining_chars]
        remaining_chars -= len(content)
        window.append(ChatMessage(role=row.role, content=content))  # type: ignore[arg-type]
    return list(reversed(window)), summary, context
