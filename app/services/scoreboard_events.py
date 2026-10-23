"""Share lightweight reveal-version checks between authenticated displays.

Only a refresh signal crosses this stream. Score rows always use the existing
authorized presentation endpoint, including its freeze and reveal filtering.
"""

import asyncio
import hashlib
import json
import logging
from weakref import WeakKeyDictionary

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.database import status_session
from app.orm_models import ContestRow, ScoreboardReleaseRow
from app.services.errors import AppError

CHECK_INTERVAL = 0.25
AUTH_INTERVAL = 10
logger = logging.getLogger(__name__)


def reveal_versions(contest_ids):
    """Read small columns only; never materialize scores or resolver snapshots."""
    versions = {}
    ids = sorted(contest_ids)
    for offset in range(0, len(ids), 250):
        batch = ids[offset : offset + 250]
        with status_session() as db:
            contests = db.execute(
                select(
                    ContestRow.contest_id,
                    ContestRow.status,
                    ContestRow.start_at,
                    ContestRow.end_at,
                    ContestRow.freeze_at,
                    ContestRow.scoreboard_freeze_mode,
                    ContestRow.scoreboard_frozen_at,
                    ContestRow.scoreboard_release_mode,
                ).where(ContestRow.contest_id.in_(batch))
            ).all()
            releases = db.execute(
                select(
                    ScoreboardReleaseRow.contest_id,
                    ScoreboardReleaseRow.division_id,
                    ScoreboardReleaseRow.revision,
                    ScoreboardReleaseRow.mode,
                    ScoreboardReleaseRow.strategy,
                )
                .where(ScoreboardReleaseRow.contest_id.in_(batch))
                .order_by(ScoreboardReleaseRow.division_id)
            ).all()
        grouped = {cid: [] for cid in batch}
        for row in releases:
            grouped[row.contest_id].append(tuple(row))
        for row in contests:
            material = json.dumps([tuple(row), grouped[row.contest_id]], default=str)
            versions[row.contest_id] = hashlib.sha256(material.encode()).hexdigest()
    return versions


class ScoreboardMonitor:
    def __init__(self):
        self.listeners = {}
        self.versions = {}
        self.task = None

    def subscribe(self, contest_id):
        changed = asyncio.Event()
        self.listeners.setdefault(contest_id, set()).add(changed)
        if self.task is None:
            self.task = asyncio.create_task(self._run())
        return changed

    async def unsubscribe(self, contest_id, changed):
        listeners = self.listeners.get(contest_id)
        if listeners is not None:
            listeners.discard(changed)
            if not listeners:
                self.listeners.pop(contest_id, None)
                self.versions.pop(contest_id, None)
        if not self.listeners and self.task is not None:
            task, self.task = self.task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self):
        while self.listeners:
            ids = set(self.listeners)
            try:
                versions = await run_in_threadpool(reveal_versions, ids)
            except SQLAlchemyError:
                # A DB outage must not imply a release or revoke a display.
                logger.debug("Presentation version check unavailable", exc_info=True)
            else:
                for cid in ids:
                    if cid not in self.listeners:
                        continue
                    version = versions.get(cid)
                    if cid not in self.versions or self.versions[cid] != version:
                        self.versions[cid] = version
                        for changed in self.listeners[cid]:
                            changed.set()
            await asyncio.sleep(CHECK_INTERVAL)


_monitors = WeakKeyDictionary()


def scoreboard_monitor():
    loop = asyncio.get_running_loop()
    if loop not in _monitors:
        _monitors[loop] = ScoreboardMonitor()
    return _monitors[loop]


async def scoreboard_events(request, contest_id, authorize):
    monitor = scoreboard_monitor()
    changed = monitor.subscribe(contest_id)
    try:
        # Refresh after connecting, including updates missed during reconnect.
        yield "event: scoreboard_update\ndata: {}\n\n"
        while not await request.is_disconnected():
            try:
                await asyncio.wait_for(changed.wait(), timeout=AUTH_INTERVAL)
                changed.clear()
                event = "event: scoreboard_update\ndata: {}\n\n"
            except TimeoutError:
                event = ": keepalive\n\n"
            try:
                await run_in_threadpool(authorize)
            except AppError:
                # The client retries through its ordinary authenticated query.
                # Token rotation is not itself evidence of account revocation.
                yield "event: reauthenticate\ndata: {}\n\n"
                return
            yield event
    finally:
        await monitor.unsubscribe(contest_id, changed)
