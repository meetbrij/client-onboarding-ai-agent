"""CaseService: creates cases, runs the graph, and applies officer decisions and document uploads.

Guarantees (each has tests):
- A decision or an upload answers one specific pause. The request carries the `interrupt_id` it saw; a stale,
  duplicate or concurrent answer loses the compare-and-set on the `cases` row and is refused and audited.
- A person's action is audited *before* it takes effect. If the audit write fails the action is not applied.
- Nobody decides a case they submitted.
- A claimed decision is stored with the claim, so a crash between the claim and the graph resume is finished at
  startup (`recover`), and a case never repeats completed nodes: the graph continues from its checkpoint.
- One runner per case at a time (`locks`).
- Document bytes live only in the in-process buffer and are never written to the case, the checkpoint or the audit log.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from onboarding.audit import AuditEvent, AuditSink
from onboarding.decision import (
    DecisionRequest,
    DocumentsResume,
    ResumeDecision,
    check_decision,
    now_iso,
)
from onboarding.graph import names
from onboarding.graph.build import build_graph
from onboarding.graph.nodes import Deps
from onboarding.locks import Busy
from onboarding.models import Applicant, CaseState, DocType, DocumentRef
from onboarding.store import TERMINAL, CaseRow, CaseStore, utcnow

log = logging.getLogger("onboarding.service")


class CaseError(Exception):
    """Base class for errors the API turns into HTTP responses."""


class CaseNotFound(CaseError):
    pass


class Conflict(CaseError):
    """Stale, duplicate or concurrent answer, or the case is being run elsewhere (HTTP 409)."""


class NotAllowed(CaseError):
    """The deterministic guard refused the decision (HTTP 422)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class Forbidden(CaseError):
    """Separation of duties (HTTP 403)."""


class Unavailable(CaseError):
    """The audit log could not be written, so the action was not applied (HTTP 503)."""


@dataclass(frozen=True)
class UploadedDoc:
    doc_type: DocType
    content: bytes
    filename: str = "document"


@dataclass
class CaseView:
    row: CaseRow
    state: CaseState | None  # None once the checkpoint has been purged
    pending: dict[str, Any] | None  # the interrupt payload while the case is waiting
    audit_events: int = 0
    notes: list[str] = field(default_factory=list)


class CaseService:
    def __init__(
        self,
        deps: Deps,
        store: CaseStore,
        checkpointer: BaseCheckpointSaver[Any],
        locks: Any,
        max_info_rounds: int = 2,
    ) -> None:
        self.deps = deps
        self.audit: AuditSink = deps.audit
        self.store = store
        self.locks = locks
        self.max_info_rounds = max_info_rounds
        self.checkpointer = checkpointer
        self.graph = build_graph(deps, checkpointer)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _config(case_id: str) -> RunnableConfig:
        return RunnableConfig(configurable={"thread_id": case_id})

    def _snapshot(self, case_id: str) -> Any:
        return self.graph.get_state(self._config(case_id))

    def _stage(self, case_id: str, docs: list[UploadedDoc]) -> list[DocumentRef]:
        refs = []
        for i, d in enumerate(docs, start=1):
            ref = f"{case_id}-{uuid.uuid4().hex[:8]}-{i}"
            self.deps.buffer.put(case_id, ref, d.content)
            refs.append(
                DocumentRef(doc_ref=ref, doc_type=d.doc_type, sha256=hashlib.sha256(d.content).hexdigest())
            )
        return refs

    def _run(self, case_id: str, graph_input: Any, segment: str) -> None:
        try:
            with self.locks.hold(case_id), self.deps.tracer.run(case_id, segment):
                self.graph.invoke(graph_input, self._config(case_id))
        except Busy as exc:
            raise Conflict("the case is being processed by another runner; try again shortly") from exc
        self._after_run(case_id)

    def _run_safely(self, case_id: str, graph_input: Any, segment: str) -> None:
        """Run, and turn an unexpected failure into an audited, recoverable state instead of an exception."""
        try:
            self._run(case_id, graph_input, segment)
        except Conflict:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, and the case stays recoverable
            log.exception("run failed", extra={"case_id": case_id, "segment": segment})
            try:
                self.audit.append(
                    AuditEvent(case_id, "run_failed", node=segment, payload={"error": type(exc).__name__})
                )
            except Exception:  # noqa: BLE001
                log.exception("could not audit the failure", extra={"case_id": case_id})
            self.store.project(
                case_id, last_error=type(exc).__name__
            )  # the class only: messages can echo applicant data

    def _after_run(self, case_id: str) -> None:
        """Write the pause to the audit log (once per pause) and refresh the projection from the checkpoint."""
        snap = self._snapshot(case_id)
        if not snap.values:
            return
        state = CaseState.model_validate(snap.values)
        row = self.store.get(case_id)
        assert row is not None
        waiting: str | None = None
        interrupt_id: str | None = None
        if snap.interrupts:
            payload = snap.interrupts[0].value
            kind = payload["kind"]
            waiting = "approve" if kind == "approve" else "await_docs"
            interrupt_id = payload["interrupt_id"]
            if row.interrupt_id != interrupt_id:
                if waiting == "approve":
                    self.audit.append(
                        AuditEvent(
                            case_id,
                            names.APPROVAL_REQUESTED,
                            node=names.APPROVE,
                            payload={
                                "interrupt_id": interrupt_id,
                                "recommendation": payload["recommendation"]["action"],
                                "rating": payload["risk_rating"],
                                "info_round": payload["info_round"],
                            },
                        )
                    )
                else:
                    self.audit.append(
                        AuditEvent(
                            case_id,
                            names.DOCUMENTS_REQUESTED,
                            node=names.AWAIT_DOCS,
                            payload={
                                "interrupt_id": interrupt_id,
                                "needed": payload["needed"],
                                "info_round": payload["info_round"],
                            },
                        )
                    )
            status = "awaiting_officer" if waiting == "approve" else "awaiting_documents"
        elif snap.next:
            status = "running"
        else:
            status = state.status
        values: dict[str, Any] = {
            "status": status,
            "waiting_on": waiting,
            "interrupt_id": interrupt_id,
            "risk_rating": state.risk.rating if state.risk else None,
            "recommendation": state.recommendation.action if state.recommendation else None,
            "pending_json": None,
            "last_error": None,
        }
        if status in TERMINAL:
            values["final_json"] = _final_json(state)
        self.store.project(case_id, **values)

    # ------------------------------------------------------------------ commands
    def create_case(
        self, applicant: Applicant, docs: list[UploadedDoc], submitted_by: str, case_id: str | None = None
    ) -> CaseView:
        case_id = case_id or str(uuid.uuid4())
        refs = self._stage(case_id, docs)
        state = CaseState(case_id=case_id, applicant=applicant, submitted_by=submitted_by, documents=refs)
        self.audit.append(
            AuditEvent(
                case_id,
                "case_created",
                actor=submitted_by,
                payload={"submitted_by": submitted_by, "documents": [d.doc_type for d in docs]},
            )
        )
        self.store.insert(case_id, submitted_by, applicant.name, state.model_dump(mode="json"))
        self._run_safely(case_id, state, "start")
        return self.get(case_id)

    def decide(self, case_id: str, request: DecisionRequest, officer: str) -> CaseView:
        row = self.store.get(case_id)
        if row is None:
            raise CaseNotFound(case_id)
        if row.submitted_by == officer:
            self._refuse(case_id, officer, request.interrupt_id, "separation of duties")
            raise Forbidden("nobody may decide a case they submitted")
        if row.waiting_on != "approve" or row.interrupt_id != request.interrupt_id:
            self._refuse(case_id, officer, request.interrupt_id, "stale or duplicate decision")
            raise Conflict("this decision answers an approval request that is no longer open")
        state = CaseState.model_validate(self._snapshot(case_id).values)
        problems = check_decision(state, request, self.max_info_rounds)
        if problems:
            self._refuse(case_id, officer, request.interrupt_id, "guard: " + "; ".join(problems)[:300])
            raise NotAllowed(problems)
        resume = ResumeDecision(**request.model_dump(), officer=officer, at=now_iso())
        # 1. the action is audited first; if this fails, nothing is applied
        try:
            self.audit.append(
                AuditEvent(
                    case_id,
                    "decision_received",
                    actor=officer,
                    node=names.APPROVE,
                    payload={
                        "interrupt_id": request.interrupt_id,
                        "action": request.action,
                        "dispositions": dict(request.dispositions),
                        "note": request.note or "",
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("audit write failed; decision not applied", extra={"case_id": case_id})
            raise Unavailable("the audit log is unavailable; the decision was not applied") from exc
        # 2. claim the pause exactly once
        if not self.store.claim(
            case_id, "approve", request.interrupt_id, {"kind": "decision", "resume": resume.model_dump()}
        ):
            self._refuse(case_id, officer, request.interrupt_id, "lost the race to another decision")
            raise Conflict("another decision was recorded first")
        # 3. resume the graph from its checkpoint
        self._run_safely(case_id, Command(resume=resume.model_dump()), "resume")
        return self.get(case_id)

    def add_documents(self, case_id: str, interrupt_id: str, docs: list[UploadedDoc], actor: str) -> CaseView:
        row = self.store.get(case_id)
        if row is None:
            raise CaseNotFound(case_id)
        if row.waiting_on != "await_docs" or row.interrupt_id != interrupt_id:
            self._refuse(case_id, actor, interrupt_id, "stale or duplicate document upload")
            raise Conflict("the case is not waiting for these documents")
        if not docs:
            raise NotAllowed(["no documents were provided"])
        refs = self._stage(case_id, docs)
        resume = DocumentsResume(
            interrupt_id=interrupt_id,
            documents=[r.model_dump(include={"doc_ref", "doc_type", "sha256"}) for r in refs],
        )
        try:
            self.audit.append(
                AuditEvent(
                    case_id,
                    "documents_received",
                    actor=actor,
                    node=names.AWAIT_DOCS,
                    payload={
                        "interrupt_id": interrupt_id,
                        "documents": [{"doc_type": r.doc_type, "sha256": r.sha256} for r in refs],
                    },
                )
            )
            claimed = self.store.claim(
                case_id, "await_docs", interrupt_id, {"kind": "documents", "resume": resume.model_dump()}
            )
        except Exception as exc:
            self._discard(case_id, refs)
            raise Unavailable(
                "the audit log or case store is unavailable; the documents were not applied"
            ) from exc
        if not claimed:
            self._discard(case_id, refs)
            self._refuse(case_id, actor, interrupt_id, "lost the race to another upload")
            raise Conflict("another upload was recorded first")
        self._run_safely(case_id, Command(resume=resume.model_dump()), "resume")
        return self.get(case_id)

    def _discard(self, case_id: str, refs: list[DocumentRef]) -> None:
        for r in refs:
            self.deps.buffer.take(case_id, r.doc_ref)

    def _refuse(self, case_id: str, actor: str, interrupt_id: str, reason: str) -> None:
        try:
            self.audit.append(
                AuditEvent(
                    case_id,
                    "action_refused",
                    actor=actor,
                    payload={"interrupt_id": interrupt_id, "reason": reason},
                )
            )
        except Exception:  # noqa: BLE001 - the refusal itself still goes back to the caller
            log.exception("could not audit a refusal", extra={"case_id": case_id})

    # ------------------------------------------------------------------ queries
    def get(self, case_id: str) -> CaseView:
        row = self.store.get(case_id)
        if row is None:
            raise CaseNotFound(case_id)
        snap = self._snapshot(case_id)
        state = CaseState.model_validate(snap.values) if snap.values else None
        pending = snap.interrupts[0].value if snap.interrupts else None
        return CaseView(row=row, state=state, pending=pending)

    def list_cases(self, submitted_by: str | None = None) -> list[CaseRow]:
        return self.store.list_cases(submitted_by)

    def audit_trail(self, case_id: str) -> list[Any]:
        return self.audit.rows(case_id)

    # ------------------------------------------------------------------ recovery and retention
    def recover(self) -> list[str]:
        """Finish cases a crashed process left mid-run. Safe to call on every start and from several replicas."""
        recovered = []
        for row in self.store.in_flight():
            case_id = row.case_id
            snap = self._snapshot(case_id)
            try:
                if row.pending:
                    resume = row.pending["resume"]
                    still_waiting = (
                        bool(snap.interrupts)
                        and snap.interrupts[0].value["interrupt_id"] == resume["interrupt_id"]
                    )
                    graph_input: Any = Command(resume=resume) if still_waiting else None
                    if not still_waiting and not snap.next:
                        self._after_run(case_id)
                        recovered.append(case_id)
                        continue
                elif snap.values:
                    if not snap.next:
                        self._after_run(case_id)
                        recovered.append(case_id)
                        continue
                    graph_input = None
                else:
                    graph_input = CaseState.model_validate(
                        row.initial
                    )  # bytes are gone: extraction will degrade
                self._run_safely(case_id, graph_input, "recover")
                self.audit.append(AuditEvent(case_id, "case_recovered", node="recover", payload={}))
                recovered.append(case_id)
            except Conflict:
                continue  # another replica has it
        return recovered

    def purge_checkpoints(self, older_than_days: int) -> int:
        """Delete the checkpoints (and the extracted values in them) of finished cases. The audit log and the
        `final_json` summary stay."""
        cutoff = utcnow() - timedelta(days=older_than_days)
        purged = 0
        for case_id in self.store.terminal_older_than(cutoff):
            snap = self._snapshot(case_id)
            if not snap.values:
                continue
            if not hasattr(self.checkpointer, "delete_thread"):
                break
            self.checkpointer.delete_thread(case_id)
            self.audit.append(
                AuditEvent(case_id, "checkpoint_purged", payload={"older_than_days": older_than_days})
            )
            purged += 1
        return purged


def _final_json(state: CaseState) -> dict[str, Any]:
    """What survives after the checkpoint is purged: outcome and identifiers, no extracted values."""
    return {
        "status": state.status,
        "decision": state.decision.model_dump() if state.decision else None,
        "risk_rating": state.risk.rating if state.risk else None,
        "recommendation": state.recommendation.action if state.recommendation else None,
        "rules": [r.rule_id for r in state.risk.fired_rules] if state.risk else [],
        "hits": [
            {"entry_id": h.entry_id, "classification": h.classification, "disposition": h.disposition}
            for h in (state.screening.hits if state.screening else [])
        ],
        "customer_id": state.execution.customer_id if state.execution else None,
        "degraded": list(state.degraded),
        "info_rounds": state.info_rounds,
    }
