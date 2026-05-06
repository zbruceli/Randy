"""Consultation runner — channel-agnostic task management.

Owns the in-flight `asyncio.Task` for each running consultation, plus a
per-task progress log so pull-style channels (HTMX polling) and push-style
channels (Telegram callbacks) can both consume the same stream.

Lifecycle of a task:
  start()     -> task_id, spawns background task
  push subscribers fire on every progress line
  pull subscribers call get_progress(task_id) at any time
  await wait(task_id)       -> ConsultationResult (or raises)
  cancel(task_id)           -> True if it was running
  Tasks self-clean ~5 min after completion to bound memory.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from ..memory import MemoryStore
from .pipeline import ConsultationResult, run_consultation

logger = logging.getLogger("randy.runner")

ProgressCallback = Callable[[str], Awaitable[None]]
TaskStatus = Literal["running", "done", "failed", "cancelled"]

_RETENTION_SECONDS = 300.0


@dataclass
class ProgressSnapshot:
    task_id: str
    status: TaskStatus
    progress_lines: list[str]
    result: ConsultationResult | None = None
    error: str | None = None


@dataclass
class _RunningTask:
    task_id: str
    user_id: str
    question: str
    asyncio_task: asyncio.Task
    progress_lines: list[str] = field(default_factory=list)
    push_callbacks: list[ProgressCallback] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    status: TaskStatus = "running"
    result: ConsultationResult | None = None
    error: str | None = None


class ConsultationRunner:
    def __init__(self, store: MemoryStore):
        self._store = store
        self._tasks: dict[str, _RunningTask] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        user_id: str,
        question: str,
        *,
        round2: bool = False,
        use_profile: bool = True,
        on_progress: ProgressCallback | None = None,
    ) -> str:
        """Begin a consultation. Returns a task_id immediately."""
        self._gc()
        task_id = uuid.uuid4().hex[:12]

        async def _multicast(line: str) -> None:
            entry = self._tasks.get(task_id)
            if entry is None:
                return
            entry.progress_lines.append(line)
            for cb in list(entry.push_callbacks):
                try:
                    await cb(line)
                except Exception:
                    logger.exception("push callback failed (non-fatal)")

        async def _runner() -> None:
            try:
                result = await run_consultation(
                    user_id=user_id,
                    question=question,
                    store=self._store,
                    on_progress=_multicast,
                    round2=round2,
                    use_profile=use_profile,
                )
                entry = self._tasks.get(task_id)
                if entry is not None:
                    entry.result = result
                    entry.status = "done"
            except asyncio.CancelledError:
                entry = self._tasks.get(task_id)
                if entry is not None:
                    entry.status = "cancelled"
                raise
            except Exception as e:
                logger.exception("consultation failed task_id=%s", task_id)
                entry = self._tasks.get(task_id)
                if entry is not None:
                    entry.status = "failed"
                    entry.error = f"{type(e).__name__}: {e}"
            finally:
                entry = self._tasks.get(task_id)
                if entry is not None:
                    entry.finished_at = time.monotonic()

        loop_task = asyncio.create_task(_runner(), name=f"consultation:{task_id}")
        running = _RunningTask(
            task_id=task_id,
            user_id=user_id,
            question=question,
            asyncio_task=loop_task,
        )
        if on_progress is not None:
            running.push_callbacks.append(on_progress)
        self._tasks[task_id] = running
        return task_id

    def subscribe(self, task_id: str, callback: ProgressCallback) -> None:
        entry = self._tasks.get(task_id)
        if entry is None:
            return
        entry.push_callbacks.append(callback)

    def get_progress(self, task_id: str) -> ProgressSnapshot | None:
        entry = self._tasks.get(task_id)
        if entry is None:
            return None
        return ProgressSnapshot(
            task_id=task_id,
            status=entry.status,
            progress_lines=list(entry.progress_lines),
            result=entry.result,
            error=entry.error,
        )

    async def wait(self, task_id: str) -> ConsultationResult:
        """Await completion. Raises CancelledError or RuntimeError on failure."""
        entry = self._tasks.get(task_id)
        if entry is None:
            raise KeyError(f"unknown task_id: {task_id}")
        await entry.asyncio_task
        if entry.status == "cancelled":
            raise asyncio.CancelledError()
        if entry.status == "failed":
            raise RuntimeError(entry.error or "consultation failed")
        assert entry.result is not None
        return entry.result

    def cancel(self, task_id: str) -> bool:
        entry = self._tasks.get(task_id)
        if entry is None or entry.asyncio_task.done():
            return False
        entry.asyncio_task.cancel()
        return True

    def list_active(self) -> list[ProgressSnapshot]:
        return [s for s in (self.get_progress(tid) for tid in self._tasks) if s and s.status == "running"]

    def _gc(self) -> None:
        """Drop completed tasks older than _RETENTION_SECONDS to bound memory."""
        now = time.monotonic()
        stale = [
            tid for tid, t in self._tasks.items()
            if t.finished_at is not None and (now - t.finished_at) > _RETENTION_SECONDS
        ]
        for tid in stale:
            self._tasks.pop(tid, None)
