"""In-memory audit log with the same chain as the Postgres one. For unit tests and the offline CLI.
It has no trigger or role protection: the append-only guarantee is a Postgres property."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from onboarding.audit.chain import (
    GENESIS_HASH,
    AuditEvent,
    AuditRow,
    VerifyResult,
    compute_row_hash,
    verify_rows,
)


class MemoryAuditLog:
    def __init__(self) -> None:
        self._rows: list[AuditRow] = []
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> AuditRow:
        with self._lock:
            prev = self._rows[-1] if self._rows else None
            base = AuditRow(
                seq=(prev.seq + 1) if prev else 1,
                ts=datetime.now(UTC),
                case_id=event.case_id,
                event_type=event.event_type,
                actor=event.actor,
                node=event.node,
                payload=event.payload,
                prompt_name=event.prompt_name,
                prompt_version=event.prompt_version,
                model_id=event.model_id,
                prev_hash=prev.row_hash if prev else GENESIS_HASH,
                row_hash="",
            )
            row = AuditRow(**{**base.__dict__, "row_hash": compute_row_hash(base.prev_hash, base)})
            self._rows.append(row)
            return row

    def rows(self, case_id: str | None = None) -> list[AuditRow]:
        with self._lock:
            return [r for r in self._rows if case_id is None or r.case_id == case_id]

    def verify(self) -> VerifyResult:
        with self._lock:
            return verify_rows(list(self._rows))
