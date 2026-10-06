"""The officer's decision: its shape, and the deterministic guard that decides whether it is allowed.

`check_decision` is the single place the rules live. The API calls it before resuming the graph (to answer
the officer with reasons), and the `approve` node calls it again on resume as a second line of defence.
No LLM output is read here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from onboarding.models import CaseState, Decision, Hit, Screening

Disposition = Literal["cleared", "confirmed"]
MIN_NOTE_CHARS = 10


class DecisionRequest(BaseModel):
    """What an officer submits. `interrupt_id` binds the decision to the approval request they saw."""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str
    action: Literal["approve", "reject", "request_more_info"]
    note: str | None = Field(default=None, max_length=1000)
    dispositions: dict[str, Disposition] = Field(default_factory=dict)


class ResumeDecision(DecisionRequest):
    """The resume value stored in the checkpoint: the request plus who decided and when."""

    officer: str
    at: str


class DocumentsResume(BaseModel):
    """Resume value for `await_docs`: references only. The bytes are in the in-process buffer."""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str
    documents: list[dict[str, str]]  # {doc_ref, doc_type, sha256}


def approval_interrupt_id(case_id: str, info_rounds: int) -> str:
    return f"{case_id}:a{info_rounds}"


def documents_interrupt_id(case_id: str, info_rounds: int) -> str:
    return f"{case_id}:d{info_rounds}"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def merged_dispositions(hits: list[Hit], request: DecisionRequest) -> dict[str, Disposition | None]:
    out: dict[str, Disposition | None] = {h.entry_id: h.disposition for h in hits}
    out.update(request.dispositions)
    return out


def check_decision(state: CaseState, request: DecisionRequest, max_info_rounds: int) -> list[str]:
    """Reasons the decision is not allowed; an empty list means it is."""
    problems: list[str] = []
    hits = state.screening.hits if state.screening else []
    known = {h.entry_id for h in hits}
    unknown = sorted(set(request.dispositions) - known)
    if unknown:
        problems.append(f"disposition given for entries that are not hits on this case: {', '.join(unknown)}")
    note = (request.note or "").strip()

    if request.action == "approve":
        if not state.extraction.available:
            problems.append(
                "cannot approve while document extraction was unavailable; reject or request more information"
            )
        if state.missing_documents:
            problems.append("cannot approve while required documents are missing; request more information")
        disp = merged_dispositions(hits, request)
        undisposed = sorted(e for e, d in disp.items() if d is None)
        confirmed = sorted(e for e, d in disp.items() if d == "confirmed")
        if undisposed:
            problems.append(
                f"every sanctions hit needs a disposition before approval; missing: {', '.join(undisposed)}"
            )
        if confirmed:
            problems.append(f"a confirmed sanctions match cannot be approved: {', '.join(confirmed)}")
        overrides = (
            (state.recommendation is not None and state.recommendation.action != "approve")
            or (state.risk is not None and state.risk.rating != "low")
            or any(d == "cleared" for d in disp.values())
        )
        if overrides and len(note) < MIN_NOTE_CHARS:
            problems.append(
                f"a note of at least {MIN_NOTE_CHARS} characters is required when approving against the "
                "recommendation, above low risk, or after clearing a hit"
            )
    elif request.action == "request_more_info" and state.info_rounds >= max_info_rounds:
        problems.append(
            f"the limit of {max_info_rounds} information requests has been reached; approve or reject"
        )
    elif request.action == "reject" and any(h.classification == "strong" for h in hits):
        undisposed = [h.entry_id for h in hits if merged_dispositions(hits, request)[h.entry_id] is None]
        if undisposed:
            problems.append(f"record a disposition for each hit before rejecting: {', '.join(undisposed)}")
    return problems


def apply_dispositions(screening: Screening, dispositions: dict[str, Disposition], officer: str) -> Screening:
    hits = [
        h.model_copy(update={"disposition": dispositions[h.entry_id], "disposition_by": officer})
        if h.entry_id in dispositions
        else h
        for h in screening.hits
    ]
    return screening.model_copy(update={"hits": hits})


def to_decision(resume: ResumeDecision) -> Decision:
    return Decision(action=resume.action, officer=resume.officer, note=resume.note, at=resume.at)
