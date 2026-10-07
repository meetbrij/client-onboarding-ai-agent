"""Postgres wiring: migrations, the LangGraph checkpointer, and the retention purge command.

    python -m onboarding.db migrate   # as the schema owner: tables, triggers, grants for the application role
    python -m onboarding.db purge     # delete checkpoints of finished cases older than CHECKPOINT_RETENTION_DAYS

Database roles (DECISIONS D-02 and D-13)
  owner  creates and migrates everything; used only by `migrate`.
  app    the running service. SELECT/INSERT on audit_log (nothing else: no UPDATE, DELETE or TRUNCATE),
         SELECT/INSERT/UPDATE on `cases`, and SELECT/INSERT/UPDATE/DELETE on the LangGraph tables (DELETE is
         what the retention purge needs).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from onboarding.audit.postgres import install_schema
from onboarding.graph.serde import checkpoint_serde
from onboarding.store import CaseStore

CHECKPOINT_SCHEMA = "langgraph"
MIGRATE_LOCK_KEY = 7_315_492_002  # advisory lock id: serialises concurrent `migrate` runs
MIGRATE_LOCK_WAIT_S = 300.0


def libpq_conninfo(database_url: str) -> str:
    """A SQLAlchemy URL (postgresql+psycopg://...) as a plain libpq connection string."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("the checkpointer needs a PostgreSQL URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def postgres_checkpointer(database_url: str, setup: bool = False) -> Iterator[PostgresSaver]:
    """A PostgresSaver on its own schema, with LangGraph's strict serializer for our state models.
    `setup=True` creates LangGraph's tables and needs owner rights; the running service passes False."""
    pool: ConnectionPool[Connection[Any]] = ConnectionPool(
        libpq_conninfo(database_url),
        max_size=5,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={CHECKPOINT_SCHEMA}",
        },
    )
    pool.open()
    try:
        saver = PostgresSaver(pool, serde=checkpoint_serde())  # type: ignore[arg-type]
        if setup:
            saver.setup()
        yield saver
    finally:
        pool.close()


def migrate(owner_url: str, app_role: str) -> None:
    """Create or update everything the service needs. Idempotent."""
    if not app_role.replace("_", "").isalnum():
        raise ValueError("app_role must be a plain identifier")
    engine: Engine = create_engine(owner_url)
    # One migration at a time (several replicas start together): a session-level advisory lock around everything.
    # Two traps, both found by running it: the lock connection must be AUTOCOMMIT (an open transaction blocks
    # LangGraph's CREATE INDEX CONCURRENTLY), and waiters must poll with pg_try_advisory_lock (a statement that
    # is blocked inside pg_advisory_lock is itself a running transaction that CREATE INDEX CONCURRENTLY waits for).
    lock = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    deadline = time.monotonic() + MIGRATE_LOCK_WAIT_S
    while not lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": MIGRATE_LOCK_KEY}).scalar():
        if time.monotonic() > deadline:
            lock.close()
            raise TimeoutError("another migration has held the lock for too long")
        time.sleep(1.0)
    try:
        _migrate_locked(engine, owner_url, app_role)
    finally:
        lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATE_LOCK_KEY})
        lock.close()
    engine.dispose()


def _migrate_locked(engine: Engine, owner_url: str, app_role: str) -> None:
    with engine.begin() as c:
        c.execute(text(f"CREATE SCHEMA IF NOT EXISTS {CHECKPOINT_SCHEMA}"))
    with postgres_checkpointer(owner_url, setup=True):
        pass
    CaseStore(engine).create_schema()
    install_schema(engine, app_role=app_role)
    with engine.begin() as c:
        c.execute(text(f'GRANT USAGE ON SCHEMA {CHECKPOINT_SCHEMA} TO "{app_role}"'))
        c.execute(
            text(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {CHECKPOINT_SCHEMA} TO "{app_role}"'
            )
        )
        c.execute(text(f'GRANT SELECT, INSERT, UPDATE ON cases TO "{app_role}"'))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m onboarding.db",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["migrate", "purge"])
    args = parser.parse_args(argv)
    if args.command == "migrate":
        owner_url, role = os.environ.get("OWNER_DATABASE_URL"), os.environ.get("APP_DB_ROLE")
        if not owner_url or not role:
            print("set OWNER_DATABASE_URL and APP_DB_ROLE", file=sys.stderr)
            return 2
        migrate(owner_url, role)
        print(f"migrated; granted to role {role}")
        return 0
    from onboarding.bootstrap import build_service_from_env

    with build_service_from_env() as service:
        days = int(os.environ.get("CHECKPOINT_RETENTION_DAYS", "30"))
        print(
            f"purged checkpoints of {service.purge_checkpoints(days)} finished case(s) older than {days} days"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
