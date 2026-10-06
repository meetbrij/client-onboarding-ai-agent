"""The `cases` table: a small projection of where each case is, kept next to the LangGraph checkpoint.

It holds no personal data beyond the synthetic applicant name. Its job is to (1) list and filter cases
without loading checkpoints, (2) make a decision or a document upload claim a specific pause exactly once
(compare-and-set on `waiting_on` and `interrupt_id`), and (3) remember a claimed decision so a crash between
the claim and the resume can be finished at startup. Works on Postgres and SQLite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, DateTime, Engine, Integer, MetaData, String, Table, Text, and_, select, update

metadata = MetaData()
cases = Table(
    "cases",
    metadata,
    Column("case_id", String(64), primary_key=True),
    Column("submitted_by", String(128), nullable=False),
    Column("applicant_name", String(200), nullable=False),
    Column("status", String(32), nullable=False),
    Column("waiting_on", String(16)),  # "approve" | "await_docs" | NULL
    Column("interrupt_id", String(128)),
    Column("pending_json", Text),  # a claimed decision or document set not yet applied
    Column("initial_json", Text, nullable=False),  # the starting state (references only, no bytes)
    Column("risk_rating", String(8)),
    Column("recommendation", String(16)),
    Column("final_json", Text),  # kept when the checkpoint is purged: ids, decision, outcome; no values
    Column("last_error", Text),
    Column("version", Integer, nullable=False, default=0),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

TERMINAL = ("approved", "rejected", "failed")
RUNNING = ("running", "resuming")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class CaseRow:
    case_id: str
    submitted_by: str
    applicant_name: str
    status: str
    waiting_on: str | None
    interrupt_id: str | None
    pending: dict[str, Any] | None
    initial: dict[str, Any]
    risk_rating: str | None
    recommendation: str | None
    final: dict[str, Any] | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _row(m: Any) -> CaseRow:
    return CaseRow(
        case_id=m["case_id"],
        submitted_by=m["submitted_by"],
        applicant_name=m["applicant_name"],
        status=m["status"],
        waiting_on=m["waiting_on"],
        interrupt_id=m["interrupt_id"],
        pending=json.loads(m["pending_json"]) if m["pending_json"] else None,
        initial=json.loads(m["initial_json"]),
        risk_rating=m["risk_rating"],
        recommendation=m["recommendation"],
        final=json.loads(m["final_json"]) if m["final_json"] else None,
        last_error=m["last_error"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


class CaseStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create_schema(self) -> None:
        metadata.create_all(self.engine)

    def insert(self, case_id: str, submitted_by: str, applicant_name: str, initial: dict[str, Any]) -> None:
        now = utcnow()
        with self.engine.begin() as c:
            c.execute(
                cases.insert().values(
                    case_id=case_id,
                    submitted_by=submitted_by,
                    applicant_name=applicant_name,
                    status="running",
                    initial_json=json.dumps(initial),
                    version=0,
                    created_at=now,
                    updated_at=now,
                )
            )

    def get(self, case_id: str) -> CaseRow | None:
        with self.engine.connect() as c:
            m = c.execute(select(cases).where(cases.c.case_id == case_id)).mappings().first()
        return _row(m) if m else None

    def list_cases(self, submitted_by: str | None = None, limit: int = 200) -> list[CaseRow]:
        q = select(cases).order_by(cases.c.created_at.desc()).limit(limit)
        if submitted_by:
            q = q.where(cases.c.submitted_by == submitted_by)
        with self.engine.connect() as c:
            return [_row(m) for m in c.execute(q).mappings()]

    def in_flight(self) -> list[CaseRow]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(cases).where(cases.c.status.in_(RUNNING)).order_by(cases.c.created_at)
            ).mappings()
            return [_row(m) for m in rows]

    def claim(self, case_id: str, waiting_on: str, interrupt_id: str, pending: dict[str, Any]) -> bool:
        """Compare-and-set: take the pause `interrupt_id` for one caller. False if it is already taken or stale."""
        with self.engine.begin() as c:
            r = c.execute(
                update(cases)
                .where(
                    and_(
                        cases.c.case_id == case_id,
                        cases.c.waiting_on == waiting_on,
                        cases.c.interrupt_id == interrupt_id,
                    )
                )
                .values(
                    waiting_on=None,
                    status="resuming",
                    pending_json=json.dumps(pending),
                    version=cases.c.version + 1,
                    updated_at=utcnow(),
                )
            )
            return r.rowcount == 1

    def project(self, case_id: str, **values: Any) -> None:
        values.setdefault("updated_at", utcnow())
        for key in ("final_json", "pending_json"):
            if isinstance(values.get(key), dict):
                values[key] = json.dumps(values[key])
        with self.engine.begin() as c:
            c.execute(
                update(cases).where(cases.c.case_id == case_id).values(**values, version=cases.c.version + 1)
            )

    def terminal_older_than(self, cutoff: datetime) -> list[str]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(cases.c.case_id).where(and_(cases.c.status.in_(TERMINAL), cases.c.updated_at < cutoff))
            )
            return [r[0] for r in rows]
