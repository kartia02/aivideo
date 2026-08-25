"""In-memory task registry.

Deliberately tiny and swappable: replace `TaskStore` with a Redis/Postgres-backed
implementation exposing the same coroutines and nothing else has to change.
Note that in-memory state does not survive a restart and is not shared across
multiple worker processes.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from schemas import TaskStatus

TERMINAL_STATUSES: frozenset[str] = frozenset({"COMPLETED", "FAILED"})


def _now() -> datetime:
    return datetime.now(UTC)


class VideoTask(BaseModel):
    task_id: str
    status: TaskStatus
    prompt: str
    provider: str
    provider_task_id: str | None = None
    video_url: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, VideoTask] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, prompt: str, provider: str) -> VideoTask:
        now = _now()
        task = VideoTask(
            task_id=str(uuid.uuid4()),
            status="PENDING",
            prompt=prompt,
            provider=provider,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._tasks[task.task_id] = task
        return task

    async def get(self, task_id: str) -> VideoTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def update(self, task_id: str, **fields: object) -> VideoTask | None:
        """Apply a partial update and bump `updated_at`."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            updated = task.model_copy(update={**fields, "updated_at": _now()})
            self._tasks[task_id] = updated
            return updated
