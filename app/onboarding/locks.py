"""One runner per case at a time. A graph run holds the case lock; a second caller (another replica, or the
startup recovery) gets `Busy` and leaves the case alone."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text


class Busy(RuntimeError):
    """Another process is already running this case."""


class LocalLocks:
    """In-process locks (SQLite, tests, single replica)."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held: set[str] = set()

    @contextmanager
    def hold(self, case_id: str) -> Iterator[None]:
        with self._guard:
            if case_id in self._held:
                raise Busy(case_id)
            self._held.add(case_id)
        try:
            yield
        finally:
            with self._guard:
                self._held.discard(case_id)


class PostgresAdvisoryLocks:
    """Session-level advisory locks on a dedicated connection held for the length of the run."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def hold(self, case_id: str) -> Iterator[None]:
        conn = self.engine.connect()
        try:
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": case_id}
            ).scalar()
            conn.commit()
            if not got:
                raise Busy(case_id)
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": case_id})
                conn.commit()
        finally:
            conn.close()
