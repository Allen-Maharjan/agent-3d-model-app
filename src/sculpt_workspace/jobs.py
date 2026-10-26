"""Bounded in-process job queue with a single worker.

Jobs are persisted in SQLite before submission is acknowledged; the queue
only carries job ids. One Blender subprocess runs at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from .errors import QueueFull

log = logging.getLogger("sculpt.jobs")


class JobQueue:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._outstanding = 0
        self._task: asyncio.Task[None] | None = None
        self.current_job: str | None = None

    @property
    def outstanding(self) -> int:
        return self._outstanding

    def reserve(self, slots: int = 1) -> None:
        """Reserve capacity before persisting jobs, so submissions are refused
        up front instead of being acknowledged and then dropped."""
        if self._outstanding + slots > self.capacity:
            raise QueueFull(
                "The job queue is full. Wait for running jobs to finish and try again.",
                details={"capacity": self.capacity, "outstanding": self._outstanding},
            )
        self._outstanding += slots

    def release(self, slots: int = 1) -> None:
        self._outstanding = max(0, self._outstanding - slots)

    def put(self, job_id: str) -> None:
        self._queue.put_nowait(job_id)

    def start(self, execute: Callable[[str], Awaitable[None]]) -> None:
        self._task = asyncio.create_task(self._loop(execute), name="sculpt-job-worker")

    async def _loop(self, execute: Callable[[str], Awaitable[None]]) -> None:
        while True:
            job_id = await self._queue.get()
            self.current_job = job_id
            try:
                await execute(job_id)
            except Exception:
                log.exception("job worker error job_id=%s", job_id)
            finally:
                self.current_job = None
                self.release(1)
                self._queue.task_done()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
