"""Reuse one connection inside a synchronous request, never across requests."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from app.database import SessionLocal
from sqlalchemy.orm import Session, sessionmaker

_current = ContextVar("request_database_session", default=None)


class _AtomicSession(Session):
    """Legacy store commits become flushes inside a single change request."""

    def commit(self):
        self.flush()


@contextmanager
def atomic_request_database():
    factory = sessionmaker(class_=_AtomicSession, **SessionLocal.kw)
    with factory() as session:
        token = _current.set(session)
        try:
            yield session
            Session.commit(session)
            for callback in session.info.get("after_commit", []):
                try:
                    callback()
                except Exception:
                    pass  # File cleanup is best effort, as in the store.
        except BaseException:
            session.rollback()
            raise
        finally:
            _current.reset(token)


def after_commit(callback):
    session = _current.get()
    if isinstance(session, _AtomicSession):
        session.info.setdefault("after_commit", []).append(callback)
    else:
        callback()


@contextmanager
def borrowed_session(session):
    try:
        yield session
    except BaseException:
        session.rollback()
        raise


def current_session(fallback=SessionLocal):
    session = _current.get()
    return borrowed_session(session) if session is not None else fallback()


def request_database(function=None, *, session_factory=SessionLocal):
    if function is None:
        return lambda target: request_database(target, session_factory=session_factory)
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _current.get() is not None:
            return function(*args, **kwargs)
        with session_factory() as session:
            token = _current.set(session)
            try:
                return function(*args, **kwargs)
            finally:
                _current.reset(token)
    return wrapped
