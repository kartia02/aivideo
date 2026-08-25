"""Task registry, kept in memory and mirrored to disk.

Every task is written to `<output_dir>/<task_id>.json` on create and on each
update, and the whole directory is read back at startup. That matters because a
render costs real money: without it, an `--reload` restart in the middle of a
job orphans a clip that Veo has already started billing for and deletes after
two days.

The same JSON file doubles as the sidecar record for the saved `.mp4`, so one
directory holds both the video and the story of how it was made.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from schemas import TaskStatus

logger = logging.getLogger(__name__)

TERMINAL_STATUSES: frozenset[str] = frozenset({"COMPLETED", "FAILED"})

INTERRUPTED_ERROR = (
    "서버가 재시작되어 이 작업의 제출 결과를 확인할 수 없습니다. "
    "과금되었을 수 있으니 프로바이더 콘솔을 확인해 주세요."
)


def _now() -> datetime:
    return datetime.now(UTC)


class VideoTask(BaseModel):
    task_id: str
    status: TaskStatus
    prompt: str
    provider: str
    provider_task_id: str | None = None
    video_url: str | None = None
    # Where the finished mp4 was saved locally, once it has been downloaded.
    local_path: str | None = None
    duration_seconds: int | None = None
    aspect_ratio: str | None = None
    estimated_cost_usd: float | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class TaskStore:
    def __init__(self, persist_dir: Path | None = None) -> None:
        self._tasks: dict[str, VideoTask] = {}
        self._lock = asyncio.Lock()
        self._persist_dir = persist_dir
        if persist_dir is not None:
            persist_dir.mkdir(parents=True, exist_ok=True)

    # -- persistence ------------------------------------------------------ #

    def _path_for(self, task_id: str) -> Path | None:
        if self._persist_dir is None:
            return None
        return self._persist_dir / f"{task_id}.json"

    def _write(self, task: VideoTask) -> None:
        path = self._path_for(task.task_id)
        if path is None:
            return
        try:
            # Write-then-rename so a crash mid-write can't leave a half file
            # that fails to parse on the next startup.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(task.model_dump_json(indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.exception("Could not persist task %s", task.task_id)

    def load(self) -> int:
        """Read persisted tasks back in. Returns how many were restored.

        Tasks left mid-flight by a restart are reconciled:
          * PROCESSING with a provider id -> kept, so polling picks it up again.
          * PENDING (never got a provider id) -> FAILED, since the background
            submit that owned it is gone and its result is unknowable.
        """
        if self._persist_dir is None or not self._persist_dir.is_dir():
            return 0

        restored = 0
        for path in sorted(self._persist_dir.glob("*.json")):
            try:
                task = VideoTask.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError, json.JSONDecodeError):
                logger.warning("Skipping unreadable task file %s", path.name)
                continue

            if task.status == "PENDING" and task.provider_task_id is None:
                task = task.model_copy(
                    update={
                        "status": "FAILED",
                        "error": INTERRUPTED_ERROR,
                        "updated_at": _now(),
                    }
                )
                self._write(task)

            self._tasks[task.task_id] = task
            restored += 1

        return restored

    def resumable(self) -> list[VideoTask]:
        """Tasks that were still rendering when the process last stopped."""
        return [t for t in self._tasks.values() if not t.is_terminal]

    # -- registry --------------------------------------------------------- #

    async def create(
        self,
        *,
        prompt: str,
        provider: str,
        duration_seconds: int | None = None,
        aspect_ratio: str | None = None,
        estimated_cost_usd: float | None = None,
    ) -> VideoTask:
        now = _now()
        task = VideoTask(
            task_id=str(uuid.uuid4()),
            status="PENDING",
            prompt=prompt,
            provider=provider,
            duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio,
            estimated_cost_usd=estimated_cost_usd,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._tasks[task.task_id] = task
            self._write(task)
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
            self._write(updated)
            return updated
