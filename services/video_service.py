"""Orchestrates the task lifecycle: store + provider.

Once a job is accepted the service watches it on its own rather than relying on
the browser to keep polling. A render costs money and Veo deletes the result
after two days, so the download has to happen even if the tab was closed — the
watcher is what makes "saved automatically on completion" actually true.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from services.errors import ServiceError, TaskNotFoundError
from services.task_store import TaskStore, VideoTask
from services.video_provider import ProviderResult, VideoProvider

logger = logging.getLogger(__name__)


class VideoService:
    def __init__(
        self,
        provider: VideoProvider,
        store: TaskStore,
        *,
        output_dir: Path | None = None,
        poll_interval_seconds: float = 5.0,
        watch_timeout_seconds: float = 20 * 60,
    ) -> None:
        self._provider = provider
        self._store = store
        self._output_dir = output_dir
        self._poll_interval = poll_interval_seconds
        self._watch_timeout = watch_timeout_seconds
        self._watchers: set[asyncio.Task[None]] = set()
        # Task ids whose download is already running, so an in-flight save and a
        # concurrent poll don't both pull the same file.
        self._saving: set[str] = set()

        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def provider(self) -> VideoProvider:
        return self._provider

    def estimate_cost_usd(self, duration_seconds: int | None) -> float | None:
        estimate = getattr(self._provider, "estimate_cost_usd", None)
        return estimate(duration_seconds) if estimate else None

    # -- lifecycle -------------------------------------------------------- #

    async def create_task(
        self,
        prompt: str,
        *,
        duration_seconds: int | None = None,
        aspect_ratio: str | None = None,
    ) -> VideoTask:
        """Register a PENDING task. Submission happens in the background."""
        return await self._store.create(
            prompt=prompt,
            provider=self._provider.name,
            duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio,
            estimated_cost_usd=self.estimate_cost_usd(duration_seconds),
        )

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
        self.watch(task_id)

    def resume_interrupted(self) -> int:
        """Re-attach watchers to jobs that outlived the last process."""
        tasks = self._store.resumable()
        for task in tasks:
            logger.info(
                "Resuming task %s (%s) left behind by a restart",
                task.task_id,
                task.status,
            )
            self.watch(task.task_id)
        return len(tasks)

    async def shutdown(self) -> None:
        for watcher in list(self._watchers):
            watcher.cancel()
        if self._watchers:
            await asyncio.gather(*self._watchers, return_exceptions=True)

    # -- watching --------------------------------------------------------- #

    def watch(self, task_id: str) -> None:
        watcher = asyncio.create_task(self._watch(task_id))
        # Hold a reference; asyncio only keeps a weak one and would let the
        # task be collected mid-render.
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def _watch(self, task_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self._watch_timeout
        try:
            while True:
                await asyncio.sleep(self._poll_interval)

                task = await self._store.get(task_id)
                if task is None or task.is_terminal or task.provider_task_id is None:
                    return

                try:
                    result = await self._provider.fetch(task.provider_task_id)
                except ServiceError as exc:
                    # Transient upstream trouble: keep waiting rather than
                    # failing a job that is probably still rendering.
                    logger.warning("Watch poll failed for %s: %s", task_id, exc.message)
                else:
                    refreshed = await self._apply(task, result)
                    if refreshed.is_terminal:
                        return

                if asyncio.get_running_loop().time() > deadline:
                    logger.warning("Stopped watching %s after timeout", task_id)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dead watcher must not be silent
            logger.exception("Watcher for task %s crashed", task_id)

    # -- reads ------------------------------------------------------------ #

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

        return await self._apply(task, result)

    async def _apply(self, task: VideoTask, result: ProviderResult) -> VideoTask:
        """Fold a provider reading into the store, saving the video when done."""
        unchanged = (
            result.status == task.status
            and result.video_url is None
            and result.error == task.error
        )
        if unchanged:
            return task

        refreshed = (
            await self._store.update(
                task.task_id,
                status=result.status,
                video_url=result.video_url,
                error=result.error,
            )
            or task
        )

        if refreshed.status == "COMPLETED" and refreshed.local_path is None:
            saved = await self._save_video(refreshed)
            if saved is not None:
                refreshed = await self._store.update(
                    refreshed.task_id, local_path=saved
                ) or refreshed
        return refreshed

    # -- video bytes ------------------------------------------------------- #

    async def _save_video(self, task: VideoTask) -> str | None:
        """Pull the finished mp4 to disk. Never fails the task if it can't."""
        if self._output_dir is None or not task.video_url:
            return None
        if task.task_id in self._saving:
            return None

        self._saving.add(task.task_id)
        path = self._output_dir / f"{task.task_id}.mp4"
        partial = path.with_suffix(".mp4.part")
        try:
            with partial.open("wb") as handle:
                async for chunk in self._provider.stream(task.video_url):
                    handle.write(chunk)
            partial.replace(path)
            logger.info("Saved %s (%d bytes)", path, path.stat().st_size)
            return str(path)
        except Exception:  # noqa: BLE001 - a failed save is not a failed render
            logger.exception("Could not save video for task %s", task.task_id)
            partial.unlink(missing_ok=True)
            return None
        finally:
            self._saving.discard(task.task_id)

    def local_video_path(self, task: VideoTask) -> Path | None:
        """The saved copy, if we have one — it outlives the provider's URL."""
        if not task.local_path:
            return None
        path = Path(task.local_path)
        return path if path.is_file() else None

    def stream_video(self, video_url: str) -> AsyncIterator[bytes]:
        """Proxy the finished video, so provider credentials stay server-side."""
        return self._provider.stream(video_url)
