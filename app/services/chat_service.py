"""Chat orchestration: graph execution, streaming and persistence."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.graph import build_graph
from app.agents.nodes.generate import REFUSAL_ANSWER
from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.enums import RetrievalStrategy
from app.core.exceptions import AppError, GuardrailError, RateLimitError
from app.core.ids import new_id
from app.core.request_scope import request_session
from app.guardrails.injection import detect_prompt_injection
from app.infra.rate_limit import RateLimiter
from app.memory.context import ContextResolution, resolve_context
from app.memory.session import restore_memory
from app.memory.summarizer import ConversationSummarizer
from app.observability.metrics import REQUEST_LATENCY, REQUESTS, ROUTING, STAGE_LATENCY, TOKEN_USAGE
from app.providers.base import LLMProvider
from app.repositories.memory_repository import MemoryRepository
from app.repositories.models import MessageMetadataRow, PendingActionRow
from app.repositories.session_repository import SessionRepository
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.reranker import DocumentReranker
from app.schemas.chat import ActionProposal, ChatMessage, ChatRequest, ChatResponse
from app.schemas.common import LatencyBreakdown, TokenUsage
from app.schemas.memory import ConversationContext
from app.services.progress import ProgressReporter, TurnProgress
from app.tools.factory import build_tool_registry
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedTurn:
    history: list[ChatMessage]
    summary: str
    resolved: ContextResolution
    confirmed_action: ActionProposal | None


class ChatService:
    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMProvider,
        retriever: HybridRetriever,
        reranker: DocumentReranker,
        session_factory: async_sessionmaker[AsyncSession],
        rate_limiter: RateLimiter,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._retriever = retriever
        self._reranker = reranker
        self._session_factory = session_factory
        self._rate_limiter = rate_limiter
        self.registry = registry or build_tool_registry(retriever)
        self._summarizer = ConversationSummarizer(llm, max_chars=settings.memory_summary_max_chars)
        self._graph = build_graph(
            llm=llm,
            settings=settings,
            retriever=retriever,
            reranker=reranker,
            registry=self.registry,
        )

    async def chat(self, request: ChatRequest, *, trace_id: str) -> ChatResponse:
        return await self._run_turn(request, trace_id=trace_id)

    async def stream(self, request: ChatRequest, *, trace_id: str) -> AsyncIterator[dict]:
        """Deliver preparation events while preparation is actually running."""
        yield {"event": "started", "trace_id": trace_id}
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        holder: dict = {"response": None, "error": None}

        async def produce() -> None:
            try:
                holder["response"] = await self._run_turn(
                    request, trace_id=trace_id, on_event=queue.put
                )
            except Exception as exc:
                holder["error"] = exc
            finally:
                await queue.put(None)

        task = asyncio.create_task(produce())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
            await task
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if holder["error"] is not None:
            raise holder["error"]
        response: ChatResponse = holder["response"]
        # Never stream the LLM's JSON envelope or an answer that may still be
        # replaced by the faithfulness check. Only the final answer is public.
        for index in range(0, len(response.answer), 48):
            yield {"event": "token", "text": response.answer[index : index + 48]}
        yield {"event": "answer", "data": response.model_dump(mode="json")}
        yield {"event": "done", "trace_id": trace_id}

    async def _run_turn(
        self, request: ChatRequest, *, trace_id: str, on_event: ProgressReporter | None = None
    ) -> ChatResponse:
        progress = TurnProgress(on_event)
        started = time.perf_counter()
        async with progress.phase("rate_limit"):
            await self._enforce_rate_limit(request.user_id)
        await progress.mark("apply_guardrail", "running")
        refused = self._apply_guardrail(request, trace_id=trace_id)
        await progress.mark("apply_guardrail", "failed" if refused is not None else "completed")
        if refused is not None:
            refused.preparation_steps = progress.snapshot()
            self._observe(refused, started)
            return refused
        session_id = request.session_id or new_id("sess_")
        async with request_session(self._session_factory) as db:
            prepared = await self._prepare_turn(db, request, session_id, progress=progress)
            async with progress.phase("initialize_state"):
                state = self._initial_state(request, session_id, trace_id, prepared)
            nodes: list[str] = []
            if on_event:
                merged = dict(state)
                try:
                    async for update in self._graph.astream(state, stream_mode="updates"):
                        for node, payload in update.items():
                            nodes.append(node)
                            if isinstance(payload, dict):
                                merged.update(payload)
                            event: dict = {"event": "node", "node": node}
                            if isinstance(payload, dict) and payload.get("intent"):
                                event["intent"] = payload["intent"].label.value
                            if isinstance(payload, dict) and payload.get("strategy"):
                                event["strategy"] = payload["strategy"].value
                            await on_event(event)
                except GuardrailError as exc:
                    merged.update(
                        final_answer=exc.message, refused=True, error=exc.error_code.value
                    )
                result = cast(AgentState, merged)
            else:
                result = await self._invoke(state)
            response = self._to_response(result, session_id=session_id, trace_id=trace_id)
            response.progress_nodes = nodes
            response.preparation_steps = progress.snapshot()
            await self._persist(db, request, response, result=result)
            await db.commit()
        self._observe(response, started)
        return response

    async def _enforce_rate_limit(self, user_id: str) -> None:
        allowed = await self._rate_limiter.allow(f"user:{user_id}")
        if not allowed:
            raise RateLimitError()

    def _apply_guardrail(self, request: ChatRequest, *, trace_id: str) -> ChatResponse | None:
        """Reject raw input before opening a DB session or restoring memory."""
        try:
            detect_prompt_injection(request.message)
        except GuardrailError as exc:
            return ChatResponse(
                answer=REFUSAL_ANSWER,
                session_id=request.session_id or new_id("sess_"),
                trace_id=trace_id,
                strategy=RetrievalStrategy.REFUSE,
                refused=True,
                error_code=exc.error_code.value,
            )
        return None

    async def _prepare_turn(
        self,
        db: AsyncSession,
        request: ChatRequest,
        session_id: str,
        *,
        progress: TurnProgress | None = None,
    ) -> PreparedTurn:
        progress = progress or TurnProgress()
        async with progress.phase("validate_session"):
            sessions = SessionRepository(db)
            session = await sessions.get_or_create(session_id, request.user_id, lock=True)
            confirmed_action = await self._resolve_confirmation(request, session_id, db)
        async with progress.phase("restore_memory"):
            history, summary, context = await restore_memory(
                db, session, self._settings, self._summarizer
            )
        async with progress.phase("resolve_context"):
            resolved = resolve_context(request.message, context)
        return PreparedTurn(history, summary, resolved, confirmed_action)

    @staticmethod
    def _initial_state(
        request: ChatRequest, session_id: str, trace_id: str, prepared: PreparedTurn
    ) -> AgentState:
        """Construct fresh graph state only after all turn preparation is done."""
        return {
            "trace_id": trace_id,
            "session_id": session_id,
            "user_id": request.user_id,
            "user_query": request.message,
            "messages": prepared.history,
            "summary": prepared.summary,
            **prepared.resolved,
            "retrieved_chunks": [],
            "tool_results": [],
            "citations": [],
            "refused": False,
            "step_count": 0,
            "token_usage": TokenUsage(),
            "latency": LatencyBreakdown(),
            "confirm_action_id": request.confirm_action_id,
            "confirmed_action": prepared.confirmed_action,
            "idempotency_key": request.idempotency_key,
        }

    async def _resolve_confirmation(
        self, request: ChatRequest, session_id: str, db: AsyncSession
    ) -> ActionProposal | None:
        if not request.confirm_action_id:
            return None
        return await MemoryRepository(db).resolve_action(
            request.confirm_action_id, session_id, request.user_id
        )

    async def _invoke(self, state: AgentState) -> AgentState:
        try:
            # LangGraph widens the state to dict[str, Any]; the graph contract is AgentState.
            return cast(AgentState, await self._graph.ainvoke(state))
        except GuardrailError as exc:
            return {
                **state,
                "final_answer": exc.message,
                "refused": True,
                "error": exc.error_code.value,
            }
        except AppError:
            raise

    async def _persist(
        self,
        db: AsyncSession,
        request: ChatRequest,
        response: ChatResponse,
        *,
        result: AgentState,
    ) -> None:
        sessions = SessionRepository(db)
        await sessions.append_message(response.session_id, "user", request.message)
        assistant = await sessions.append_message(response.session_id, "assistant", response.answer)
        db.add(
            MessageMetadataRow(message_id=assistant.id, response_json=response.model_dump_json())
        )
        if result.get("refused"):
            return
        memories = MemoryRepository(db)
        memory = await memories.get(response.session_id)
        context = result.get("conversation_context") or ConversationContext.model_validate_json(
            memory.context_json
        )
        memory.context_json = context.model_dump_json()
        if response.pending_action:
            await memories.remember_action(
                response.session_id, request.user_id, response.pending_action
            )
        elif request.confirm_action_id:
            confirmed = result.get("confirmed_action")
            successful = any(
                t.success and confirmed is not None and t.name == confirmed.tool_name
                for t in result.get("tool_results", [])
            )
            if successful:
                pending = await db.get(PendingActionRow, request.confirm_action_id)
                if pending:
                    pending.status = "completed"
        else:
            await memories.invalidate_pending(response.session_id)

    def _to_response(
        self,
        result: AgentState,
        *,
        session_id: str,
        trace_id: str,
    ) -> ChatResponse:
        answer = result.get("final_answer") or "暂时无法回答，请稍后重试。"
        strategy = result.get("strategy")
        intent = result.get("intent")
        return ChatResponse(
            answer=answer,
            session_id=session_id,
            trace_id=trace_id,
            intent=intent.label if intent else None,
            complexity=result.get("query_complexity"),
            strategy=strategy,
            citations=result.get("citations") or [],
            pending_action=result.get("pending_action"),
            refused=bool(result.get("refused")),
            error_code=result.get("error"),
            token_usage=result.get("token_usage") or TokenUsage(),
            latency=result.get("latency") or LatencyBreakdown(),
        )

    def _observe(self, response: ChatResponse, started: float) -> None:
        total_ms = (time.perf_counter() - started) * 1000
        response.latency.total_ms = total_ms
        REQUESTS.labels(
            strategy=(response.strategy.value if response.strategy else "unknown"),
            intent=(response.intent.value if response.intent else "unknown"),
            status="ok" if not response.refused else "refused",
        ).inc()
        if response.strategy:
            ROUTING.labels(strategy=response.strategy.value).inc()
        REQUEST_LATENCY.observe(total_ms / 1000)
        STAGE_LATENCY.labels(stage="intent").observe(response.latency.intent_ms / 1000)
        STAGE_LATENCY.labels(stage="retrieval").observe(response.latency.retrieval_ms / 1000)
        STAGE_LATENCY.labels(stage="llm").observe(response.latency.llm_ms / 1000)
        TOKEN_USAGE.labels(kind="total").inc(response.token_usage.total_tokens)
        logger.info(
            "chat_completed",
            extra={
                "trace_id": response.trace_id,
                "strategy": response.strategy.value if response.strategy else None,
                "intent": response.intent.value if response.intent else None,
                "latency_ms": round(total_ms, 1),
            },
        )
