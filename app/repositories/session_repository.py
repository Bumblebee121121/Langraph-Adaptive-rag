"""Conversation session and message persistence."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidRequestError
from app.core.time import utcnow
from app.repositories.models import MessageRow, SessionRow
from app.schemas.chat import ChatMessage


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self, session_id: str, user_id: str, *, lock: bool = False
    ) -> SessionRow:
        row = await self._session.get(SessionRow, session_id)
        if row is None:
            row = SessionRow(session_id=session_id, user_id=user_id)
            self._session.add(row)
            await self._session.flush()
        elif row.user_id != user_id:
            raise InvalidRequestError("会话不存在或不属于当前用户。")
        if lock:
            # Acquire a transaction write lock before reading mutable memory. This
            # serializes concurrent turns on SQLite and locks the session on Postgres.
            await self._session.execute(
                update(SessionRow)
                .where(SessionRow.session_id == session_id)
                .values(updated_at=utcnow())
            )
            await self._session.refresh(row)
        return row

    async def recent_messages(self, session_id: str, *, limit: int = 20) -> list[ChatMessage]:
        result = await self._session.execute(
            select(MessageRow)
            .where(MessageRow.session_id == session_id)
            .order_by(MessageRow.id.desc())
            .limit(limit)
        )
        rows = list(reversed(result.scalars().all()))
        return [ChatMessage(role=row.role, content=row.content) for row in rows]  # type: ignore[arg-type]

    async def append_message(self, session_id: str, role: str, content: str) -> MessageRow:
        message = MessageRow(session_id=session_id, role=role, content=content)
        self._session.add(message)
        row = await self._session.get(SessionRow, session_id)
        if row is not None:
            row.updated_at = utcnow()
        await self._session.flush()
        return message

    async def save_summary(self, session_id: str, summary: str) -> None:
        row = await self._session.get(SessionRow, session_id)
        if row is not None:
            row.summary = summary
            row.updated_at = utcnow()
