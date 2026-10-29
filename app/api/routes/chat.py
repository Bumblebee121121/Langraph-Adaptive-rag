"""Chat routes: single-shot JSON and SSE streaming."""

import logging

from fastapi import APIRouter, Depends, Request
from sse_starlette import JSONServerSentEvent
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.container import AppContainer

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> ChatResponse:
    """Answer one turn and return the full JSON response."""
    return await container.chat_service.chat(payload, trace_id=request.state.trace_id)


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> EventSourceResponse:
    """Answer one turn as an SSE stream of node, token and answer events."""

    async def events():
        try:
            async for item in container.chat_service.stream(
                payload, trace_id=request.state.trace_id
            ):
                yield JSONServerSentEvent(item)
        except AppError as exc:
            message = "请求过于频繁，请稍后再试。" if exc.http_status == 429 else exc.message
            yield JSONServerSentEvent(
                {
                    "event": "error",
                    "message": message,
                    "error_code": exc.error_code.value,
                    "trace_id": request.state.trace_id,
                }
            )
        except Exception:
            logger.exception("chat_stream_failed")
            yield JSONServerSentEvent(
                {
                    "event": "error",
                    "message": "服务暂时不可用，请稍后重试。",
                    "error_code": "INTERNAL_ERROR",
                    "trace_id": request.state.trace_id,
                }
            )

    return EventSourceResponse(events())
