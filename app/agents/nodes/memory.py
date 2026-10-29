"""Conversation memory compaction node."""

from app.agents.state import AgentState
from app.config.settings import Settings
from app.memory.summarizer import ConversationSummarizer
from app.schemas.chat import ChatMessage


async def memory_node(
    state: AgentState,
    *,
    settings: Settings,
    summarizer: ConversationSummarizer,
) -> dict:
    messages = list(state.get("messages") or [])
    messages.append(ChatMessage(role="user", content=state["user_query"]))
    final_answer = state.get("final_answer")
    if final_answer:
        messages.append(ChatMessage(role="assistant", content=final_answer))
    tool_results = list(state.get("tool_results") or [])
    summary = state.get("summary") or ""
    # restore_memory prepares the summary and bounded window before LLM calls.
    context = state.get("conversation_context")
    if context:
        context = context.model_copy(deep=True)
        intent = state.get("intent")
        if intent:
            context.last_intent = intent.label.value
        confirmed = state.get("confirmed_action")
        if confirmed and any(t.success and t.name == confirmed.tool_name for t in tool_results):
            context.pending_tool = None
            context.pending_query = ""
            context.awaiting_order = False
    return {
        "messages": messages,
        "tool_results": tool_results,
        "summary": summary,
        "conversation_context": context,
    }
