"""Durable, user-scoped conversation context. Facts never authorize execution."""

from pydantic import BaseModel


class ConversationContext(BaseModel):
    order_id: str | None = None
    pending_tool: str | None = None
    pending_query: str = ""
    awaiting_order: bool = False
    last_query: str = ""
    last_intent: str = ""
