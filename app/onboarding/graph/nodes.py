"""Workflow nodes: intake, extract, screen, assess, approve.

Conventions
- A node returns a dict of changed state fields and a small, PII-free audit payload; `audited` writes the
  `node_completed` audit row and stores the row hash in `audit_head`. If the audit write fails, the node
  fails: a step without its audit record must not count as done.
- Nodes before `approve` may call tools and the LLM. `approve` itself must stay free of side effects,
  because LangGraph re-runs an interrupted node from the top when it resumes.
- Failures of the KYC service or the LLM degrade the case (a flag in `degraded`); they do not fail it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from langgraph.types import interrupt

from onboarding.audit import AuditEvent, AuditSink
from onboarding.decision import (
    DocumentsResume,
    ResumeDecision,
    apply_dispositions,
    approval_interrupt_id,
    check_decision,
    documents_interrupt_id,
    to_decision,
)
from onboarding.graph import names
from onboarding.llm.service import LlmService
from onboarding.models import (
    REQUIRED_DOCUMENTS,
    CaseState,
    DocumentRef,
    Execution,
    ExtractedField,
    Extraction,
)
from onboarding.observability import NoopTracer, Tracer
from onboarding.rules.engine import assess_case
from onboarding.rules.reference import Reference
from onboarding.screening.scorer import ScreeningConfig, screen_applicant
from onboarding.screening.unlist import SanctionsIndex
from onboarding.tools.bank import BankClient
from onboarding.tools.buffer import DocumentBuffer
from onboarding.tools.kyc import KycClient, KycUnavailable

Update = dict[str, Any]
NodeFn = Callable[[CaseState], Update]


@dataclass
class Deps:
    audit: AuditSink
    kyc: KycClient | None
    index: SanctionsIndex
    screening_cfg: ScreeningConfig
    reference: Reference
    llm: LlmService
    buffer: DocumentBuffer
    bank: BankClient | None = None
    max_info_rounds: int = 2
    tracer: Tracer = field(default_factory=NoopTracer)


def _flags(current: list[str], flag: str, on: bool) -> list[str]:
    rest = [f for f in current if f != flag]
    return [*rest, flag] if on else rest


def audited(deps: Deps, node: str, body: Callable[[CaseState], tuple[Update, dict[str, Any]]]) -> NodeFn:
    def run(state: CaseState) -> Update:
        with deps.tracer.span(node, case_id=state.case_id) as span:
            update, payload = body(state)
            span.update(**{k: v for k, v in payload.items() if isinstance(v, str | int | float | bool)})
        row = deps.audit.append(AuditEvent(state.case_id, names.NODE_COMPLETED, node=node, payload=payload))
        return {**update, "audit_head": row.row_hash}

    run.__name__ = node
    return run


def make_nodes(deps: Deps) -> dict[str, NodeFn]:
    def audit(state: CaseState, event_type: str, node: str, payload: dict[str, Any]) -> None:
        deps.audit.append(AuditEvent(state.case_id, event_type, node=node, payload=payload))

    # ---------------------------------------------------------------- intake
    def intake(state: CaseState) -> tuple[Update, dict[str, Any]]:
        try:
            date.fromisoformat(state.applicant.dob)
        except ValueError:
            return (
                {"status": "failed", "error": "applicant date of birth is not an ISO date"},
                {"outcome": "failed", "reason": "invalid_dob"},
            )
        present = {d.doc_type for d in state.documents}
        missing = [d for d in REQUIRED_DOCUMENTS if d not in present]
        return (
            {"status": "extracting", "missing_documents": missing},
            {
                "outcome": "ok",
                "documents": len(state.documents),
                "missing": missing,
                "info_round": state.info_rounds,
            },
        )

    # --------------------------------------------------------------- extract
    def extract(state: CaseState) -> tuple[Update, dict[str, Any]]:
        fields = list(state.extraction.fields)
        flags = list(state.extraction.doc_flags)
        documents = [d.model_copy() for d in state.documents]
        failed: list[str] = []
        pending = [d for d in documents if d.kyc_document_id is None]
        for doc in pending:
            content = deps.buffer.take(state.case_id, doc.doc_ref)
            if content is None or deps.kyc is None:
                reason = "document bytes not available" if content is None else "KYC client not configured"
                failed.append(doc.doc_ref)
                audit(
                    state,
                    "tool_call",
                    names.EXTRACT,
                    {"tool": "kyc.extract", "doc_type": doc.doc_type, "outcome": "failed", "reason": reason},
                )
                continue
            try:
                with deps.tracer.span("kyc.extract", case_id=state.case_id, doc_type=doc.doc_type):
                    result = deps.kyc.extract(content, doc.doc_type, filename=doc.doc_ref)
            except KycUnavailable as exc:
                failed.append(doc.doc_ref)
                audit(
                    state,
                    "tool_call",
                    names.EXTRACT,
                    {
                        "tool": "kyc.extract",
                        "doc_type": doc.doc_type,
                        "outcome": "failed",
                        "reason": exc.reason,
                        "attempts": exc.attempts,
                    },
                )
                continue
            doc.kyc_document_id = result.document_id
            fields = [f for f in fields if f.document != doc.doc_type] + result.fields
            if result.needs_review and doc.doc_type not in flags:
                flags.append(doc.doc_type)
            audit(
                state,
                "tool_call",
                names.EXTRACT,
                {
                    "tool": "kyc.extract",
                    "doc_type": doc.doc_type,
                    "outcome": "ok",
                    "status": result.status,
                    "attempts": result.attempts,
                    "latency_ms": result.latency_ms,
                    "kyc_document_id": result.document_id,
                    "model_id": result.model_id,
                    "flagged_fields": [f.name for f in result.fields if f.needs_review],
                },
            )
        attempted = bool(documents)
        extraction = Extraction(
            attempted=attempted,
            available=attempted and not failed,
            fields=fields,
            failed_documents=failed,
            doc_flags=flags,
        )
        update: Update = {
            "documents": documents,
            "extraction": extraction,
            "status": "screening",
            "degraded": _flags(state.degraded, "extraction_unavailable", bool(failed)),
        }
        payload = {
            "documents_attempted": len(pending),
            "failed": len(failed),
            "flagged_fields": sum(f.needs_review for f in fields),
        }
        return update, payload

    # ---------------------------------------------------------------- screen
    def screen(state: CaseState) -> tuple[Update, dict[str, Any]]:
        deps.llm.reset()
        extracted_names = [
            f.value
            for f in state.extraction.fields
            if f.name == "full_name" and f.value and not f.needs_review
        ]
        result = screen_applicant(state.applicant, extracted_names, deps.index, deps.screening_cfg)
        previous = {h.entry_id: h for h in (state.screening.hits if state.screening else [])}
        for hit in result.hits:
            earlier = previous.get(hit.entry_id)
            if (
                earlier and earlier.disposition
            ):  # same list snapshot and same applicant: keep the officer's call
                hit.disposition, hit.disposition_by = earlier.disposition, earlier.disposition_by
            note = deps.llm.annotate_hit(state, hit)
            if note:
                hit.llm_note = note  # advisory only: classification and disposition are untouched
            audit(
                state,
                "sanctions_hit",
                names.SCREEN,
                {
                    "entry_id": hit.entry_id,
                    "list_source": hit.list_source,
                    "snapshot_date": result.snapshot_date,
                    "algorithm": result.algorithm,
                    "score": hit.score,
                    "classification": hit.classification,
                    "dob_agreement": hit.field_agreement.dob,
                    "nationality_agreement": hit.field_agreement.nationality,
                    "llm_note_recorded": note is not None,
                },
            )
        degraded = state.degraded
        for flag in ("llm_unavailable", "llm_disabled"):
            if flag in deps.llm.degraded and flag not in degraded:
                degraded = [*degraded, flag]
        payload = {
            "list_source": result.list_source,
            "snapshot_date": result.snapshot_date,
            "algorithm": result.algorithm,
            "names_screened": result.names_screened,
            "hits": len(result.hits),
            "strong": sum(h.classification == "strong" for h in result.hits),
        }
        return {"screening": result, "status": "assessing", "degraded": degraded}, payload

    # ---------------------------------------------------------------- assess
    def assess(state: CaseState) -> tuple[Update, dict[str, Any]]:
        deps.llm.reset()
        risk, action = assess_case(state, deps.reference)
        for rule in risk.fired_rules:
            audit(
                state,
                "rule_result",
                names.ASSESS,
                {"rule_id": rule.rule_id, "severity": rule.severity, "inputs": rule.inputs},
            )
        recommendation = deps.llm.explain(state, risk, action)
        summary = deps.llm.summarise(state, risk, action)
        draft = deps.llm.draft_missing_docs(state)
        degraded = state.degraded
        for flag in sorted(deps.llm.degraded):
            if flag not in degraded:
                degraded = [*degraded, flag]
        audit(
            state,
            "recommendation_made",
            names.ASSESS,
            {
                "action": action,
                "rating": risk.rating,
                "escalated": risk.escalated,
                "rules": [r.rule_id for r in risk.fired_rules],
                "explained_by": recommendation.drafted_by,
                "summary_by": summary.drafted_by,
                "draft_by": draft.drafted_by if draft else None,
            },
        )
        update: Update = {
            "risk": risk,
            "recommendation": recommendation,
            "summary": summary.text,
            "summary_by": summary.drafted_by,
            "missing_doc_draft": draft.text if draft else None,
            "status": "awaiting_officer",
            "degraded": degraded,
        }
        return update, {"rating": risk.rating, "action": action, "rules": len(risk.fired_rules)}

    # --------------------------------------------------------------- approve
    def approve(state: CaseState) -> Update:
        """Pause for the officer, then apply the decision that was checked and recorded before the resume.

        Everything before `interrupt()` is repeated when the graph resumes, so it must have no side effects.
        """
        expected = approval_interrupt_id(state.case_id, state.info_rounds)
        resume = ResumeDecision.model_validate(interrupt(approval_payload(state)))
        if resume.interrupt_id != expected:
            raise ValueError(f"decision answers {resume.interrupt_id}, but the case is waiting on {expected}")
        problems = check_decision(state, resume, deps.max_info_rounds)
        if problems:  # the API checks first; this is the second line of defence
            raise ValueError("decision not allowed: " + "; ".join(problems))
        assert state.screening is not None
        screening = apply_dispositions(state.screening, dict(resume.dispositions), resume.officer)
        status = {"approve": "executing", "reject": "rejected", "request_more_info": "awaiting_documents"}[
            resume.action
        ]
        return {"decision": to_decision(resume), "screening": screening, "status": status}

    # ------------------------------------------------------------ await_docs
    def await_docs(state: CaseState) -> Update:
        """Pause until the client's documents arrive. Only references travel in the resume value."""
        expected = documents_interrupt_id(state.case_id, state.info_rounds)
        payload = {
            "kind": "await_docs",
            "interrupt_id": expected,
            "case_id": state.case_id,
            "needed": list(state.missing_documents),
            "extraction_available": state.extraction.available,
            "info_round": state.info_rounds,
        }
        resume = DocumentsResume.model_validate(interrupt(payload))
        if resume.interrupt_id != expected:
            raise ValueError(f"documents answer {resume.interrupt_id}, but the case is waiting on {expected}")
        arrived = [DocumentRef.model_validate(d) for d in resume.documents]
        # a re-sent document replaces the earlier one of the same type that was not extracted
        kept = [
            d for d in state.documents if d.kyc_document_id or d.doc_type not in {a.doc_type for a in arrived}
        ]
        return {
            "documents": [*kept, *arrived],
            "info_rounds": state.info_rounds + 1,
            "decision": None,
            "status": "intake",
        }

    # --------------------------------------------------------------- execute
    def execute(state: CaseState) -> tuple[Update, dict[str, Any]]:
        """Create the customer in core banking. Reachable only from `approve` with an officer's approval."""
        d = state.decision
        if d is None or d.action != "approve" or not d.officer:
            raise PermissionError("execute requires an officer's approval on the case")
        if deps.bank is None:
            raise RuntimeError("no core-banking client configured")
        key = state.case_id
        payload = {
            "case_id": state.case_id,
            "full_name": state.applicant.name,
            "date_of_birth": state.applicant.dob,
            "nationality": state.applicant.nationality,
            "residence_country": state.applicant.residence_country,
            "occupation": state.applicant.occupation,
            "risk_rating": state.risk.rating if state.risk else "high",
        }
        audit(state, "execute_requested", names.EXECUTE, {"idempotency_key": key, "approved_by": d.officer})
        with deps.tracer.span("bank.create_customer", case_id=state.case_id, idempotency_key=key):
            result = deps.bank.create_customer(key, payload)
        audit(
            state,
            "execute_completed",
            names.EXECUTE,
            {
                "idempotency_key": key,
                "customer_id": result.customer_id,
                "replayed": result.replayed,
                "attempts": result.attempts,
            },
        )
        return (
            {
                "execution": Execution(customer_id=result.customer_id, idempotency_key=key),
                "status": "approved",
            },
            {"customer_id": result.customer_id, "replayed": result.replayed},
        )

    return {
        names.INTAKE: audited(deps, names.INTAKE, intake),
        names.EXTRACT: audited(deps, names.EXTRACT, extract),
        names.SCREEN: audited(deps, names.SCREEN, screen),
        names.ASSESS: audited(deps, names.ASSESS, assess),
        names.APPROVE: approve,
        names.AWAIT_DOCS: await_docs,
        names.EXECUTE: audited(deps, names.EXECUTE, execute),
    }


def approval_payload(state: CaseState) -> dict[str, Any]:
    """What the officer sees at the gate. JSON-safe, and free of DOB, ID numbers and addresses."""
    assert state.risk is not None and state.recommendation is not None and state.screening is not None
    return {
        "kind": "approve",
        "interrupt_id": approval_interrupt_id(state.case_id, state.info_rounds),
        "case_id": state.case_id,
        "applicant_name": state.applicant.name,
        "summary": state.summary,
        "recommendation": {
            "action": state.recommendation.action,
            "explanation": state.recommendation.explanation,
            "drafted_by": state.recommendation.drafted_by,
        },
        "risk_rating": state.risk.rating,
        "escalated": state.risk.escalated,
        "fired_rules": [r.model_dump() for r in state.risk.fired_rules],
        "screening": {
            "list_source": state.screening.list_source,
            "snapshot_date": state.screening.snapshot_date,
            "algorithm": state.screening.algorithm,
            "hits": [h.model_dump() for h in state.screening.hits],
        },
        "missing_documents": list(state.missing_documents),
        "missing_doc_draft": state.missing_doc_draft,
        "extraction_available": state.extraction.available,
        "flagged_fields": [f"{f.document}.{f.name}" for f in state.extraction.fields if f.needs_review],
        "degraded": list(state.degraded),
        "info_round": state.info_rounds,
    }


__all__ = ["Deps", "ExtractedField", "approval_payload", "make_nodes"]
