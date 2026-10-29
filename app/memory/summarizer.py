"""LLM-backed conversation summarisation."""

from app.core.exceptions import LLMOutputError, LLMProviderError
from app.prompts.summary import SUMMARY_SYSTEM, SUMMARY_USER
from app.providers.base import LLMProvider
from app.providers.fake import OfflineLLMProvider
from app.providers.json_parser import extract_json_object
from app.schemas.chat import ChatMessage


class ConversationSummarizer:
    def __init__(self, llm: LLMProvider, *, max_chars: int = 4000) -> None:
        self._llm = llm
        self._max_chars = max_chars

    async def summarize(self, messages: list[ChatMessage], *, previous_summary: str = "") -> str:
        history = "\n".join(f"{m.role}: {m.content[:2000]}" for m in messages)
        try:
            if isinstance(self._llm, OfflineLLMProvider):
                raise LLMOutputError("offline summary")
            response = await self._llm.generate(
                [
                    {"role": "system", "content": SUMMARY_SYSTEM},
                    {
                        "role": "user",
                        "content": SUMMARY_USER.format(
                            history=history, previous_summary=previous_summary
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=256,
                response_format={"type": "json_object"},
            )
            payload = extract_json_object(response.content)
            if not payload.get("summary"):
                raise LLMOutputError("empty summary")
            return str(payload["summary"])[: self._max_chars]
        except (LLMProviderError, LLMOutputError):
            text = (previous_summary + "\n" + history).strip()
            if len(text) <= self._max_chars:
                return text
            half = (self._max_chars - 5) // 2
            return text[:half] + "\n…\n" + text[-half:]
