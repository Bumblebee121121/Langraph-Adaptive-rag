"""Preparation progress outside LangGraph; optional live delivery for SSE."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal

from app.schemas.chat import PreparationStep

ProgressReporter = Callable[[dict], Awaitable[None]]


class TurnProgress:
    def __init__(self, reporter: ProgressReporter | None = None):
        self.reporter = reporter
        self.steps: list[PreparationStep] = []

    async def mark(self, stage: str, status: Literal["running", "completed", "failed"]) -> None:
        step = PreparationStep(stage=stage, status=status)
        for index, previous in enumerate(self.steps):
            if previous.stage == stage:
                self.steps[index] = step
                break
        else:
            self.steps.append(step)
        if self.reporter:
            await self.reporter({"event": "stage", **step.model_dump()})

    @asynccontextmanager
    async def phase(self, stage: str) -> AsyncIterator[None]:
        await self.mark(stage, "running")
        try:
            yield
        except Exception:
            await self.mark(stage, "failed")
            raise
        else:
            await self.mark(stage, "completed")

    def snapshot(self) -> list[PreparationStep]:
        return [step.model_copy() for step in self.steps]
