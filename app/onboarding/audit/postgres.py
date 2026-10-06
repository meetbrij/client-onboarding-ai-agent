"""Postgres audit log: append-only in the database, hash-chained, verified by `verify`.

Enforcement lives in Postgres, not in application code:
  - a trigger rejects UPDATE and DELETE on every row and TRUNCATE on the table;
  - the application role gets INSERT and SELECT only (`install_schema(app_role=...)`);
  - appends take a transaction-scoped advisory lock, so concurrent writers produce one linear chain.
A database superuser can still drop the trigger; the chain then shows later edits, and truncating the
tail is detectable only if the head hash was recorded elsewhere (DECISIONS D-02).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, text

from onboarding.audit.chain import (
    GENESIS_HASH,
    AuditEvent,
    AuditRow,
    VerifyResult,
    compute_row_hash,
    verify_rows,
)

LOCK_KEY = 7_315_492_001  # arbitrary constant: the advisory lock id for appends to audit_log

SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        seq BIGINT PRIMARY KEY,
        ts TIMESTAMPTZ NOT NULL,
        case_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL,
        node TEXT,
        payload JSONB NOT NULL,
        prompt_name TEXT,
        prompt_version TEXT,
        model_id TEXT,
        prev_hash CHAR(64) NOT NULL,
        row_hash CHAR(64) NOT NULL UNIQUE
    )
    """,
    "CREATE INDEX IF NOT EXISTS audit_log_case_idx ON audit_log (case_id, seq)",
    """
    CREATE OR REPLACE FUNCTION audit_log_reject() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log",
    """
    CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject()
    """,
    "DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log",
    """
    CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject()
    """,
]


def install_schema(engine: Engine, app_role: str | None = None) -> None:
    """Create the table, the function and the triggers; with `app_role`, grant it INSERT and SELECT only.
    Run as the schema owner (migrations), not as the application role."""
    with engine.begin() as conn:
        for stmt in SCHEMA_SQL:
            conn.execute(text(stmt))
        conn.execute(text("REVOKE ALL ON audit_log FROM PUBLIC"))
        if app_role:
            if not app_role.replace("_", "").isalnum():
                raise ValueError("app_role must be a plain identifier")
            conn.execute(text(f'REVOKE ALL ON audit_log FROM "{app_role}"'))
            conn.execute(text(f'GRANT SELECT, INSERT ON audit_log TO "{app_role}"'))


def _row(m: Any) -> AuditRow:
    payload = m["payload"] if isinstance(m["payload"], dict) else json.loads(m["payload"])
    return AuditRow(
        seq=m["seq"],
        ts=m["ts"],
        case_id=m["case_id"],
        event_type=m["event_type"],
        actor=m["actor"],
        node=m["node"],
        payload=payload,
        prompt_name=m["prompt_name"],
        prompt_version=m["prompt_version"],
        model_id=m["model_id"],
        prev_hash=m["prev_hash"],
        row_hash=m["row_hash"],
    )


class PostgresAuditLog:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def append(self, event: AuditEvent) -> AuditRow:
        with self.engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
            last = conn.execute(text("SELECT seq, row_hash FROM audit_log ORDER BY seq DESC LIMIT 1")).first()
            seq = (last[0] + 1) if last else 1
            prev_hash = last[1] if last else GENESIS_HASH
            ts = datetime.now(UTC)  # microsecond precision, which timestamptz stores exactly
            base = AuditRow(
                seq=seq,
                ts=ts,
                case_id=event.case_id,
                event_type=event.event_type,
                actor=event.actor,
                node=event.node,
                payload=json.loads(json.dumps(event.payload)),
                prompt_name=event.prompt_name,
                prompt_version=event.prompt_version,
                model_id=event.model_id,
                prev_hash=prev_hash,
                row_hash="",
            )
            row_hash = compute_row_hash(prev_hash, base)
            conn.execute(
                text(
                    "INSERT INTO audit_log (seq, ts, case_id, event_type, actor, node, payload, prompt_name, "
                    "prompt_version, model_id, prev_hash, row_hash) VALUES (:seq, :ts, :case_id, :event_type, "
                    ":actor, :node, CAST(:payload AS JSONB), :prompt_name, :prompt_version, :model_id, "
                    ":prev_hash, :row_hash)"
                ),
                {
                    "seq": seq,
                    "ts": ts,
                    "case_id": base.case_id,
                    "event_type": base.event_type,
                    "actor": base.actor,
                    "node": base.node,
                    "payload": json.dumps(base.payload),
                    "prompt_name": base.prompt_name,
                    "prompt_version": base.prompt_version,
                    "model_id": base.model_id,
                    "prev_hash": prev_hash,
                    "row_hash": row_hash,
                },
            )
        return AuditRow(**{**base.__dict__, "row_hash": row_hash})

    def rows(self, case_id: str | None = None) -> list[AuditRow]:
        query = "SELECT * FROM audit_log"
        params: dict[str, Any] = {}
        if case_id is not None:
            query += " WHERE case_id = :c"
            params["c"] = case_id
        with self.engine.connect() as conn:
            return [_row(m) for m in conn.execute(text(query + " ORDER BY seq"), params).mappings()]

    def verify(self) -> VerifyResult:
        """Verify the whole chain. (Per-case slices cannot be verified on their own: links run across cases.)"""
        return verify_rows(self.rows())
