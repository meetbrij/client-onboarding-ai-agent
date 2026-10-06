"""Hash chain for the audit log (shared by the in-memory and Postgres implementations).

Each row stores `prev_hash` (the previous row's `row_hash`, or 64 zeros for the first row) and
`row_hash = SHA-256(prev_hash + canonical JSON of the row's content)`. Editing, deleting or reordering any
row breaks every later hash, and `verify_rows` names the first row that does not fit.

Payload rules: the audit log carries identifiers, decisions, rule ids, scores and counts, never document
values. `AuditEvent` rejects payload keys that name personal data. Numbers in payloads should be ints or
decimals with few digits so they survive a round trip through JSONB unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

GENESIS_HASH = "0" * 64
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "dob",
        "date_of_birth",
        "id_number",
        "address",
        "address_line",
        "value",
        "values",
        "passport",
        "document",
    }
)
MAX_STRING = 1000


class AuditPayloadError(ValueError):
    """The payload would put personal data or oversized text into the audit log."""


def format_ts(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _check_payload(obj: Any, path: str = "payload") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise AuditPayloadError(f"{path}.{k}: key not allowed in the audit log")
            _check_payload(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_payload(v, f"{path}[{i}]")
    elif isinstance(obj, str) and len(obj) > MAX_STRING:
        raise AuditPayloadError(f"{path}: string longer than {MAX_STRING} characters")


@dataclass(frozen=True)
class AuditEvent:
    case_id: str
    event_type: str
    actor: str = "system"
    node: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    prompt_name: str | None = None
    prompt_version: str | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        _check_payload(self.payload)


@dataclass(frozen=True)
class AuditRow:
    seq: int
    ts: datetime
    case_id: str
    event_type: str
    actor: str
    node: str | None
    payload: dict[str, Any]
    prompt_name: str | None
    prompt_version: str | None
    model_id: str | None
    prev_hash: str
    row_hash: str


def canonical_content(row: AuditRow | dict[str, Any]) -> bytes:
    """The bytes that are hashed, apart from prev_hash: stable key order, no whitespace."""
    g = (lambda k: getattr(row, k)) if isinstance(row, AuditRow) else row.__getitem__
    content = {
        "seq": g("seq"),
        "ts": format_ts(g("ts")),
        "case_id": g("case_id"),
        "event_type": g("event_type"),
        "actor": g("actor"),
        "node": g("node"),
        "payload": g("payload"),
        "prompt_name": g("prompt_name"),
        "prompt_version": g("prompt_version"),
        "model_id": g("model_id"),
    }
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def compute_row_hash(prev_hash: str, row: AuditRow | dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode() + b"|" + canonical_content(row)).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    rows_checked: int
    first_broken_seq: int | None = None
    reason: str = ""
    head_seq: int = 0
    head_hash: str = GENESIS_HASH


def verify_rows(rows: list[AuditRow]) -> VerifyResult:
    """Check sequence numbers, links and hashes in order; report the first row that fails."""
    prev_hash, prev_seq = GENESIS_HASH, 0
    for r in rows:
        if r.seq != prev_seq + 1:
            return VerifyResult(
                False,
                prev_seq,
                r.seq,
                f"expected seq {prev_seq + 1}, found {r.seq} (row missing or reordered)",
            )
        if r.prev_hash != prev_hash:
            return VerifyResult(False, prev_seq, r.seq, "prev_hash does not match the previous row's hash")
        if compute_row_hash(r.prev_hash, r) != r.row_hash:
            return VerifyResult(
                False, prev_seq, r.seq, "row_hash does not match the row content (row was altered)"
            )
        prev_hash, prev_seq = r.row_hash, r.seq
    return VerifyResult(True, len(rows), None, "", prev_seq, prev_hash)


class AuditSink(Protocol):
    def append(self, event: AuditEvent) -> AuditRow: ...

    def rows(self, case_id: str | None = None) -> list[AuditRow]: ...

    def verify(self) -> VerifyResult: ...
