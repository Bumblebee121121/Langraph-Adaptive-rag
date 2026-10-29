"""Owned server-side history, explicit forgetting, and durable confirmations."""

from datetime import UTC

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_container
from app.core.exceptions import InvalidRequestError
from app.core.time import utcnow
from app.repositories.memory_repository import MemoryRepository
from app.repositories.models import (
    ConversationMemoryRow,
    MessageMetadataRow,
    MessageRow,
    PendingActionRow,
    SessionRow,
)
from app.repositories.session_repository import SessionRepository
from app.schemas.chat import ChatResponse
from app.schemas.memory import ConversationContext
from app.services.container import AppContainer

router = APIRouter(prefix="/sessions", tags=["sessions"])
UserId = Query(min_length=1, max_length=64)


async def owned_session(
    db: AsyncSession, session_id: str, user_id: str, *, lock: bool = False
) -> SessionRow:
    row = await db.get(SessionRow, session_id)
    if row is None or row.user_id != user_id:
        raise InvalidRequestError("会话不存在或不属于当前用户。")
    if lock:
        return await SessionRepository(db).get_or_create(session_id, user_id, lock=True)
    return row


@router.get("")
async def list_sessions(
    user_id: str = UserId,
    limit: int = Query(default=50, ge=1, le=100),
    container: AppContainer = Depends(get_container),
) -> dict:
    async with container.session_factory() as db:
        sessions = (
            await db.scalars(
                select(SessionRow)
                .where(SessionRow.user_id == user_id)
                .order_by(SessionRow.updated_at.desc())
                .limit(limit)
            )
        ).all()
        items = []
        for row in sessions:
            first = await db.scalar(
                select(MessageRow.content)
                .where(MessageRow.session_id == row.session_id, MessageRow.role == "user")
                .order_by(MessageRow.id)
                .limit(1)
            )
            items.append(
                {
                    "session_id": row.session_id,
                    "title": (first or "新对话")[:60],
                    "updated_at": row.updated_at.replace(tzinfo=UTC).isoformat(),
                }
            )
        return {"sessions": items}


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    user_id: str = UserId,
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = None,
    container: AppContainer = Depends(get_container),
) -> dict:
    async with container.session_factory() as db:
        session = await owned_session(db, session_id, user_id)
        query = select(MessageRow).where(MessageRow.session_id == session_id)
        if before_id is not None:
            query = query.where(MessageRow.id < before_id)
        rows = list(
            reversed(
                (await db.scalars(query.order_by(MessageRow.id.desc()).limit(limit + 1))).all()
            )
        )
        has_more = len(rows) > limit
        rows = rows[-limit:]
        pending = await MemoryRepository(db).latest_action(session_id)
        metadata = {
            m.message_id: m
            for m in (
                await db.scalars(
                    select(MessageMetadataRow).where(
                        MessageMetadataRow.message_id.in_([r.id for r in rows])
                    )
                )
            ).all()
        }
        messages = []
        for row in rows:
            meta = metadata.get(row.id)
            response = ChatResponse.model_validate_json(meta.response_json) if meta else None
            proposal = response.pending_action if response else None
            if proposal and (pending is None or proposal.action_id != pending.action_id):
                proposal = None
            messages.append(
                {
                    "id": str(row.id),
                    "role": row.role,
                    "content": row.content,
                    "citations": response.citations if response else [],
                    "pending_action": proposal,
                    "progress_nodes": response.progress_nodes if response else [],
                    "preparation_steps": response.preparation_steps if response else [],
                    "trace_id": response.trace_id if response else None,
                }
            )
        return {
            "session_id": session_id,
            "updated_at": session.updated_at.replace(tzinfo=UTC).isoformat(),
            "messages": messages,
            "has_more": has_more,
            "next_before_id": rows[0].id if has_more and rows else None,
        }


@router.delete("/{session_id}/memory", status_code=204)
async def forget_session_memory(
    session_id: str, user_id: str = UserId, container: AppContainer = Depends(get_container)
) -> Response:
    async with container.session_factory.begin() as db:
        session = await owned_session(db, session_id, user_id, lock=True)
        memory = await MemoryRepository(db).get(session_id)
        last_id = (
            await db.scalar(
                select(func.max(MessageRow.id)).where(MessageRow.session_id == session_id)
            )
            or 0
        )
        memory.summary_message_id = last_id
        memory.forgotten_message_id = last_id
        memory.context_json = ConversationContext().model_dump_json()
        session.summary = ""
        session.updated_at = utcnow()
        await MemoryRepository(db).invalidate_pending(session_id)
    return Response(status_code=204)


@router.delete("/{session_id}/actions/{action_id}", status_code=204)
async def dismiss_action(
    session_id: str,
    action_id: str,
    user_id: str = UserId,
    container: AppContainer = Depends(get_container),
) -> Response:
    async with container.session_factory.begin() as db:
        await owned_session(db, session_id, user_id, lock=True)
        row = await db.get(PendingActionRow, action_id)
        if row is None or row.session_id != session_id or row.user_id != user_id:
            raise InvalidRequestError("待确认操作不存在。")
        if row.status == "pending":
            row.status = "cancelled"
        memory = await MemoryRepository(db).get(session_id)
        context = ConversationContext.model_validate_json(memory.context_json)
        context.pending_tool, context.pending_query, context.awaiting_order = None, "", False
        memory.context_json = context.model_dump_json()
    return Response(status_code=204)


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str, user_id: str = UserId, container: AppContainer = Depends(get_container)
) -> Response:
    async with container.session_factory.begin() as db:
        await owned_session(db, session_id, user_id, lock=True)
        message_ids = select(MessageRow.id).where(MessageRow.session_id == session_id)
        await db.execute(
            delete(MessageMetadataRow).where(MessageMetadataRow.message_id.in_(message_ids))
        )
        for model in (PendingActionRow, ConversationMemoryRow, MessageRow, SessionRow):
            await db.execute(delete(model).where(model.session_id == session_id))
    return Response(status_code=204)
