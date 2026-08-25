"""Orchestrates the task lifecycle: store + provider."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from services.errors import ServiceError, TaskNotFoundError
from services.task_store import TaskStore, VideoTask
from services.video_provider import VideoProvider

logger = logging.getLogger(__name__)


class VideoService:
    def __init__(self, provider: VideoProvider, store: TaskStore) -> None:
        self._provider = provider
        self._store = store

    @property
    def provider(self) -> VideoProvider:
        return self._provider

    async def create_task(self, prompt: str) -> VideoTask:
        """Register a PENDING task. Submission happens in the background."""
        return await self._store.create(prompt=prompt, provider=self._provider.name)

    async def submit_task(
        self,
        task_id: str,
        *,
        duration_seconds: int | None = None,
        aspect_ratio: str | None = None,
    ) -> None:
        """Send the job upstream. Runs after the response is returned."""
        task = await self._store.get(task_id)
        if task is None:  # pragma: no cover - the caller just created it
            logger.warning("submit_task called for unknown task %s", task_id)
            return

        try:
            provider_task_id = await self._provider.submit(
                task.prompt,
                duration_seconds=duration_seconds,
                aspect_ratio=aspect_ratio,
            )
        except ServiceError as exc:
            logger.error("Submission failed for task %s: %s", task_id, exc.message)
            await self._store.update(task_id, status="FAILED", error=exc.message)
            return
        except Exception:  # noqa: BLE001 - never let a background task die silently
            logger.exception("Unexpected submission error for task %s", task_id)
            await self._store.update(
                task_id, status="FAILED", error="Internal error while submitting task."
            )
            return

        await self._store.update(
            task_id, status="PROCESSING", provider_task_id=provider_task_id
        )

    async def get_task(self, task_id: str) -> VideoTask:
        """Return the task, refreshing it from the provider when still in flight."""
        task = await self._store.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found.")

        if task.is_terminal or task.provider_task_id is None:
            return task

        try:
            result = await self._provider.fetch(task.provider_task_id)
        except ServiceError as exc:
            # A transient upstream hiccup shouldn't kill the task; report last state.
            logger.warning("Polling failed for task %s: %s", task_id, exc.message)
            return task

        if result.status == task.status and result.video_url is None:
            return task

        refreshed = await self._store.update(
            task_id,
            status=result.status,
            video_url=result.video_url,
            error=result.error,
        )
        return refreshed or task

    def stream_video(self, video_url: str) -> AsyncIterator[bytes]:
        """Proxy the finished video, so provider credentials stay server-side."""
        return self._provider.stream(video_url)
