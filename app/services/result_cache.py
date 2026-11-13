"""Short-lived, shared result caching. Authorization always remains in the DB."""
import hashlib
import base64
import gzip
import json
import logging
import secrets
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from threading import BoundedSemaphore, Lock

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app.settings import settings
from app.services.errors import AppError

logger = logging.getLogger(__name__)
_locks = [Lock() for _ in range(64)]
_unavailable_until = 0.0
_encoded = OrderedDict()
_encoded_lock = Lock()
_encoded_bytes = 0
_ENCODED_MAX_BYTES = 64 * 1024 * 1024
_rebuild_slots = BoundedSemaphore(1)


@contextmanager
def _rebuild_budget(enabled, wait=1):
    if enabled and not _rebuild_slots.acquire(timeout=wait):
        raise AppError(503, "api_busy", "스코어보드를 갱신 중입니다. 잠시 후 다시 시도해 주세요.",
            {"retry_after_seconds": 1})
    try:
        yield
    finally:
        if enabled:
            _rebuild_slots.release()


def _local_read(key):
    global _encoded_bytes
    with _encoded_lock:
        entry = _encoded.get(key)
        if entry is None:
            return None
        value, expiry, size = entry
        if expiry <= time.monotonic():
            _encoded.pop(key)
            _encoded_bytes -= size
            return None
        _encoded.move_to_end(key)
        return value


def _local_write(key, value, ttl):
    global _encoded_bytes
    size = len(value.encode())
    if size > min(settings.result_cache_max_entry_bytes, _ENCODED_MAX_BYTES):
        return
    with _encoded_lock:
        old = _encoded.pop(key, None)
        if old:
            _encoded_bytes -= old[2]
        while _encoded and (len(_encoded) >= 64 or _encoded_bytes + size > _ENCODED_MAX_BYTES):
            _, (_, _, removed) = _encoded.popitem(last=False)
            _encoded_bytes -= removed
        _encoded[key] = (value, time.monotonic()+ttl, size)
        _encoded_bytes += size
_BOARD_TABLES = {"contests", "contest_divisions", "participant_teams", "problems", "submissions", "scoreboard_releases"}
_SESSION_TABLES = {"general_sessions", "team_sessions"}


def _encode(value):
    if isinstance(value, datetime):
        return {"__zoj_datetime__": value.isoformat()}
    raise TypeError(f"Unsupported cache value: {type(value).__name__}")


def _decode(value):
    if set(value) == {"__zoj_datetime__"}:
        return datetime.fromisoformat(value["__zoj_datetime__"])
    return value


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


@lru_cache(maxsize=1)
def _client(url):
    return redis.Redis.from_url(url, socket_connect_timeout=0.15, socket_timeout=0.15,
                               max_connections=64, retry=Retry(NoBackoff(), 0), decode_responses=True)


def _failed():
    global _unavailable_until
    now = time.monotonic()
    if now >= _unavailable_until:
        logger.warning("Result cache unavailable; using database results")
    _unavailable_until = now + 3


def invalidate(group: str) -> None:
    if not settings.redis_url:
        return
    try:
        # Do not skip invalidations during the read circuit breaker's cooldown.
        _client(settings.redis_url).incr(f"zoj:cache:v1:generation:{group}")
    except redis.RedisError:
        _failed()


def cached_result(group: str, identity, ttl: int, compute, *, serialized: bool = False, snapshot: bool = False, compressed: bool = False):
    if serialized:
        identity = [identity, "serialized-gzip-v1" if compressed else "serialized-v1"]
    def encoded_result(value):
        if value is None:
            return "null"
        raw = json.dumps(value, default=_json_default if serialized else _encode, separators=(",", ":"))
        return "gzip:"+base64.b64encode(gzip.compress(raw.encode(), compresslevel=1, mtime=0)).decode() if compressed else raw
    def decoded(raw):
        return raw if serialized else json.loads(raw, object_hook=_decode)
    def computed():
        with _rebuild_budget(serialized):
            value = compute()
            return encoded_result(value) if serialized else value
    previous_key = None
    if snapshot and ttl > 0:
        # Public live scores have bounded display delay. A burst of submissions
        # must not create hundreds of full-board recalculations per second.
        # Access/freeze/release policy is checked by the caller on every request
        # and forms part of identity; it is never delayed by this time window.
        previous_key = "zoj:cache:v1:previous:"+hashlib.sha256(json.dumps(
            [settings.release_version, group, identity], default=_encode, sort_keys=True).encode()).hexdigest()
        identity = [identity, "snapshot", int(time.time() // ttl)]
        if not settings.redis_url or time.monotonic() < _unavailable_until:
            key = hashlib.sha256(json.dumps([group, identity], default=_encode, sort_keys=True).encode()).hexdigest()
            with _locks[int(key[:8], 16) % len(_locks)]:
                value = _local_read(key)
                if value is None:
                    value = computed()
                    _local_write(key, value, ttl)
                    _local_write(previous_key, value, ttl+1)
                return value
    if not settings.redis_url or ttl <= 0 or time.monotonic() < _unavailable_until:
        return computed()
    client = _client(settings.redis_url)
    def previous_result():
        if not previous_key:
            return None
        previous = _local_read(previous_key)
        if previous is not None:
            return previous
        try:
            previous = client.get(previous_key)
            remaining = (client.pttl(previous_key) / 1000
                if previous is not None and hasattr(client, "pttl") else ttl+1)
            if previous is not None and remaining > 0:
                _local_write(previous_key, previous, remaining)
                return previous
        except redis.RedisError:
            _failed()
        return None
    try:
        generation = "snapshot" if snapshot else client.get(f"zoj:cache:v1:generation:{group}") or "0"
        if group.startswith("scoreboard:") and not snapshot:
            # Bulk writes whose scope cannot be determined retain a safe global
            # fallback, without ordinary writes flushing unrelated contests.
            generation = [client.get("zoj:cache:v1:generation:scoreboard") or "0", generation]
        material = json.dumps([settings.release_version, group, generation, identity],
                              sort_keys=True, default=_encode, separators=(",", ":"))
        digest = hashlib.sha256(material.encode()).hexdigest()
        key = f"zoj:cache:v1:result:{group}:{digest}"
        if serialized:
            local = _local_read(key)
            if local is not None:
                return local
        value = client.get(key)
        if value is not None:
            if serialized:
                # The generation is still fetched from Redis on every read.
                # Do not extend the shared entry's TTL inside this process.
                remaining = client.pttl(key) / 1000 if hasattr(client, "pttl") else ttl
                if remaining > 0:
                    _local_write(key, value, min(remaining, ttl))
            return decoded(value)
    except (redis.RedisError, ValueError):
        _failed()
        return computed()
    # Bound simultaneous recomputation in each API worker; Redis shares the
    # computed result across workers. Locks are fixed-size, not per arbitrary URL.
    lock = _locks[int(digest[:8], 16) % len(_locks)]
    if snapshot and lock.locked():
        previous = _local_read(previous_key)
        if previous is not None:
            return previous
    with lock:
        try:
            value = client.get(key)
            if value is not None:
                return decoded(value)
        except (redis.RedisError, ValueError):
            _failed()
            return computed()
        lock_key, owner = key + ":building", None
        if serialized and hasattr(client, "eval"):
            token = secrets.token_hex(16)
            deadline = time.monotonic() + 2
            try:
                while not client.set(lock_key, token, nx=True, px=10_000):
                    value = client.get(key)
                    if value is not None:
                        remaining = client.pttl(key) / 1000
                        if remaining > 0:
                            _local_write(key, value, min(remaining, ttl))
                        return decoded(value)
                    if snapshot:
                        previous = previous_result()
                        if previous is not None:
                            return previous
                    if time.monotonic() >= deadline:
                        raise AppError(503, "api_busy", "스코어보드를 갱신 중입니다. 잠시 후 다시 시도해 주세요.",
                            {"retry_after_seconds": 1})
                    time.sleep(0.025)
                owner = token
            except redis.RedisError:
                _failed()
        try:
            if owner:
                try:
                    value = client.get(key)
                except redis.RedisError:
                    _failed()
                    value = None
                if value is not None:
                    remaining = client.pttl(key) / 1000
                    if remaining > 0:
                        _local_write(key, value, min(remaining, ttl))
                    return decoded(value)
            previous = previous_result() if snapshot else None
            try:
                with _rebuild_budget(serialized, wait=0 if previous is not None else 1):
                    value = compute()
                    encoded = encoded_result(value)
            except AppError as exc:
                # Another contest can occupy this worker's rebuild budget.
                # Reuse only an unexpired same-policy snapshot in that case.
                if exc.code == "api_busy" and previous is not None:
                    previous = previous_result()
                    if previous is not None:
                        return previous
                raise
            try:
                # Do not let an unusually large contest monopolize Redis memory.
                if len(encoded.encode()) <= settings.result_cache_max_entry_bytes:
                    client.set(key, encoded, ex=ttl)
                    if serialized:
                        _local_write(key, encoded, ttl)
                    if snapshot:
                        client.set(previous_key, encoded, ex=ttl+1)
                        _local_write(previous_key, encoded, ttl+1)
            except redis.RedisError:
                _failed()
            return encoded if serialized else value
        finally:
            if owner:
                try:
                    client.eval("if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) end",
                        1, lock_key, owner)
                except redis.RedisError:
                    _failed()


@event.listens_for(Session, "before_flush")
def _collect_changes(session, flush_context, instances):
    groups = session.info.setdefault("result_cache_groups", set())
    for row in session.new | session.dirty | session.deleted:
        table = getattr(row, "__tablename__", "")
        if table in _BOARD_TABLES:
            if table == "submissions" and row in session.dirty:
                scoring = {"score", "subtask_results", "scoring_mode", "status", "submitted_at", "participant_team_id", "problem_id", "division_id", "submission_kind"}
                if not any(inspect(row).attrs[name].history.has_changes() for name in scoring):
                    continue
            groups.add(f"scoreboard:{row.contest_id}")
        if table == "access_logs" or (table in _SESSION_TABLES and (
            row in session.new or row in session.deleted or inspect(row).attrs.revoked_at.history.has_changes()
        )):
            groups.add("access-stats")


@event.listens_for(Session, "do_orm_execute")
def _collect_bulk_changes(state):
    if not (state.is_update or state.is_delete):
        return
    table = getattr(getattr(state.statement, "table", None), "name", "")
    groups = state.session.info.setdefault("result_cache_groups", set())
    if table in _BOARD_TABLES:
        values = getattr(state.statement, "_values", None)
        changed = {getattr(column, "key", str(column)) for column in values} if values else set()
        scoring = {"score", "subtask_results", "scoring_mode", "status", "submitted_at", "participant_team_id", "problem_id", "division_id", "submission_kind", "contest_id"}
        if table != "submissions" or state.is_delete or not changed or changed & scoring:
            target = state.statement.table
            if state.parameters:
                groups.add("scoreboard")
            else:
                ids = state.session.scalars(select(target.c.contest_id)
                    .where(*state.statement._where_criteria).distinct().limit(129)).all()
                if len(ids) > 128:
                    groups.add("scoreboard")
                else:
                    groups.update(f"scoreboard:{cid}" for cid in ids)
                    # Moving rows must invalidate both old and new contests.
                    if "contest_id" in changed:
                        groups.add("scoreboard")
    if table == "access_logs" or table in _SESSION_TABLES:
        groups.add("access-stats")


@event.listens_for(Session, "after_commit")
def _publish_changes(session):
    for group in session.info.pop("result_cache_groups", ()):
        invalidate(group)


@event.listens_for(Session, "after_rollback")
def _discard_changes(session):
    session.info.pop("result_cache_groups", None)
