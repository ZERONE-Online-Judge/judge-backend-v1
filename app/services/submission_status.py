"""Coalesce result waits into bounded, cross-worker-safe database batches."""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from weakref import WeakKeyDictionary

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal, status_session
from app.orm_models import SubmissionRow, RejudgingItemRow
from app.services.errors import AppError
from app.services.request_sessions import current_session

PENDING = {"waiting", "preparing", "judging"}
BATCH_SIZE = 500
CHECK_INTERVAL = 0.5
logger = logging.getLogger(__name__)
_snapshot_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="result-auth")
_snapshot_slots = BoundedSemaphore(16)


async def read_status_snapshot(function):
    if not _snapshot_slots.acquire(blocking=False):
        raise AppError(503, "api_busy", "요청이 많습니다. 잠시 후 다시 시도해 주세요.",
            {"retry_after_seconds":1})
    try:
        future = _snapshot_workers.submit(function)
    except BaseException:
        _snapshot_slots.release()
        raise
    # A disconnected caller must not free a slot while its SQL is still running.
    future.add_done_callback(lambda _: _snapshot_slots.release())
    return await asyncio.wrap_future(future)


def pending_submissions(ids):
    pending = set()
    with SessionLocal() as db:
        for offset in range(0, len(ids), BATCH_SIZE):
            pending.update(db.scalars(select(SubmissionRow.submission_id).where(
                SubmissionRow.submission_id.in_(ids[offset:offset+BATCH_SIZE]),
                SubmissionRow.status.in_(PENDING))))
    return pending


def submission_versions(ids):
    """Small, committed DB fingerprints work across API processes and colors."""
    versions = {}
    with current_session(status_session) as db:
        for offset in range(0, len(ids), BATCH_SIZE):
            candidate = RejudgingItemRow.candidate_result
            rows = db.execute(select(
                SubmissionRow.submission_id, SubmissionRow.status_updated_at,
                SubmissionRow.status, SubmissionRow.progress_current,
                SubmissionRow.progress_total, SubmissionRow.progress_phase,
                RejudgingItemRow.rejudging_id,
                *(candidate[name].as_string() for name in (
                    "status_updated_at", "status", "progress_current", "progress_total", "progress_phase")),
            ).outerjoin(RejudgingItemRow,
                RejudgingItemRow.active_submission_id == SubmissionRow.submission_id
            ).where(SubmissionRow.submission_id.in_(ids[offset:offset+BATCH_SIZE])))
            versions.update((row[0], tuple(row[1:])) for row in rows)
    return versions


class SubmissionMonitor:
    def __init__(self):
        self.watchers = {}
        self.change_watchers = {}
        self.task = None

    async def wait_changes(self, versions, seconds):
        if seconds <= 0 or not versions:
            return
        event = asyncio.Event()
        for sid, version in versions.items():
            self.change_watchers.setdefault(sid, {})[event] = version
        if self.task is None:
            self.task = asyncio.create_task(self._run())
        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=min(seconds, 2))
            except TimeoutError:
                pass
        finally:
            for sid in versions:
                listeners = self.change_watchers.get(sid)
                if listeners is not None:
                    listeners.pop(event, None)
                    if not listeners:
                        self.change_watchers.pop(sid, None)
            await self._stop_unused()

    async def _stop_unused(self):
        if not self.watchers and not self.change_watchers and self.task:
            task, self.task = self.task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def wait(self, submission_id, seconds):
        if seconds <= 0:
            return
        event = asyncio.Event()
        self.watchers.setdefault(submission_id, set()).add(event)
        if self.task is None:
            self.task = asyncio.create_task(self._run())
        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=min(seconds, 10))
            except TimeoutError:
                pass
        finally:
            listeners = self.watchers.get(submission_id)
            if listeners is not None:
                listeners.discard(event)
                if not listeners:
                    self.watchers.pop(submission_id, None)
            await self._stop_unused()

    async def _run(self):
        while self.watchers or self.change_watchers:
            snapshot = set(self.watchers)
            changes = set(self.change_watchers)
            try:
                pending = await run_in_threadpool(pending_submissions, list(snapshot)) if snapshot else set()
                versions = await run_in_threadpool(submission_versions, list(changes)) if changes else {}
            except SQLAlchemyError:
                # A failed read cannot be reported as a terminal judge result.
                logger.warning("Submission status check unavailable; retrying")
            else:
                for sid in snapshot-pending:
                    for event in self.watchers.get(sid, ()):
                        event.set()
                for sid in changes:
                    listeners = self.change_watchers.get(sid, {})
                    for event, baseline in listeners.items():
                        if versions.get(sid) != baseline:
                            event.set()
            await asyncio.sleep(CHECK_INTERVAL)


_monitors = WeakKeyDictionary()


async def wait_for_submission(submission_id, seconds):
    loop = asyncio.get_running_loop()
    monitor = _monitors.setdefault(loop, SubmissionMonitor())
    await monitor.wait(submission_id, max(0, seconds))


async def wait_for_submission_changes(versions, seconds):
    loop = asyncio.get_running_loop()
    monitor = _monitors.setdefault(loop, SubmissionMonitor())
    await monitor.wait_changes(versions, max(0, seconds))
