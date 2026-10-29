"""Persistent conversation context and expiring confirmation proposals."""

from datetime import UTC, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidRequestError
from app.core.time import utcnow
from app.repositories.models import ConversationMemoryRow, IdempotencyRow, PendingActionRow
from app.schemas.chat import ActionProposal


class MemoryRepository:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def get(self, session_id: str) -> ConversationMemoryRow:
        row = await self.db.get(ConversationMemoryRow, session_id)
        if row is None:
            row = ConversationMemoryRow(
                session_id=session_id, context_json="{}", summary_message_id=0
            )
            self.db.add(row)
            await self.db.flush()
        return row

    async def invalidate_pending(self, session_id: str) -> None:
        await self.db.execute(
            update(PendingActionRow)
            .where(PendingActionRow.session_id == session_id, PendingActionRow.status == "pending")
            .values(status="cancelled")
        )

    async def remember_action(self, session_id: str, user_id: str, action: ActionProposal) -> None:
        await self.invalidate_pending(session_id)
        self.db.add(
            PendingActionRow(
                action_id=action.action_id,
                session_id=session_id,
                user_id=user_id,
                action_json=action.model_dump_json(),
                expires_at=utcnow() + timedelta(minutes=15),
            )
        )

    async def resolve_action(self, action_id: str, session_id: str, user_id: str) -> ActionProposal:
        row = await self.db.get(PendingActionRow, action_id)
        if (
            row is None
            or row.session_id != session_id
            or row.user_id != user_id
            or row.expires_at.replace(tzinfo=UTC) < utcnow()
            or row.status not in {"pending", "completed"}
        ):
            raise InvalidRequestError("待确认操作不存在或已过期，请重新发起。")
        if row.status == "completed" and await self.db.get(IdempotencyRow, action_id) is None:
            raise InvalidRequestError("该操作已结束，请重新发起。")
        return ActionProposal.model_validate_json(row.action_json)

    async def latest_action(self, session_id: str) -> ActionProposal | None:
        rows = await self.db.scalars(
            select(PendingActionRow).where(
                PendingActionRow.session_id == session_id,
                PendingActionRow.status == "pending",
                PendingActionRow.expires_at > utcnow(),
            )
        )
        row = rows.first()
        return ActionProposal.model_validate_json(row.action_json) if row else None
