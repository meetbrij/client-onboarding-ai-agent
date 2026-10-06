"""Each node on its own, with the offline dependencies (fake KYC service, fake LLM, in-memory audit)."""

from __future__ import annotations

import json

import pytest
from langgraph.errors import GraphInterrupt

from onboarding.graph import names
from onboarding.graph.nodes import make_nodes
from onboarding.graph.routes import route_after_intake
from onboarding.llm.client import FakeLlm
from onboarding.models import CaseState, DocumentRef
from tests.helpers import applicant, apply, initial_state, offline, run_nodes


def events(audit, event_type: str):
    return [r for r in audit.rows() if r.event_type == event_type]


# ---------------------------------------------------------------- intake
def test_intake_computes_missing_documents():
    case, deps, audit, _ = offline("missing_poa")
    s = run_nodes(deps, initial_state(case, deps), names.INTAKE)
    assert s.missing_documents == ["proof_of_address"] and s.status == "extracting"
    assert events(audit, "node_completed")[0].payload["missing"] == ["proof_of_address"]


def test_intake_with_all_documents_has_nothing_missing():
    case, deps, _, _ = offline()
    assert run_nodes(deps, initial_state(case, deps), names.INTAKE).missing_documents == []


def test_intake_with_no_documents_lists_both_as_missing():
    case, deps, _, _ = offline()
    s = CaseState(case_id="x", applicant=case.applicant)
    assert run_nodes(deps, s, names.INTAKE).missing_documents == ["id_document", "proof_of_address"]


def test_intake_rejects_an_invalid_date_of_birth_and_the_route_ends_the_run():
    case, deps, audit, _ = offline()
    s = CaseState(case_id="x", applicant=applicant(dob="not-a-date"))
    s = run_nodes(deps, s, names.INTAKE)
    assert s.status == "failed" and "date of birth" in (s.error or "")
    assert route_after_intake(s) == "__end__"
    assert route_after_intake(CaseState(case_id="y", applicant=applicant())) == names.EXTRACT
    assert events(audit, "node_completed")[0].payload["outcome"] == "failed"


# ---------------------------------------------------------------- extract
def test_extract_success_records_fields_ids_and_a_tool_call():
    case, deps, audit, _ = offline("low_confidence_extraction")
    s = run_nodes(deps, initial_state(case, deps), names.INTAKE, names.EXTRACT)
    assert s.extraction.attempted and s.extraction.available and not s.extraction.failed_documents
    assert all(d.kyc_document_id for d in s.documents)
    flagged = {f.name for f in s.extraction.fields if f.needs_review}
    assert flagged == {"date_of_birth", "id_number"} and s.extraction.doc_flags == ["id_document"]
    calls = events(audit, "tool_call")
    assert len(calls) == 2 and all(c.payload["outcome"] == "ok" for c in calls)
    assert sorted(calls[0].payload["flagged_fields"]) == ["date_of_birth", "id_number"]
    assert s.degraded == []


def test_extract_degrades_when_kyc_is_down():
    case, deps, audit, _ = offline("kyc_unavailable")
    s = run_nodes(deps, initial_state(case, deps), names.INTAKE, names.EXTRACT)
    assert s.extraction.attempted and not s.extraction.available and len(s.extraction.failed_documents) == 2
    assert s.degraded == ["extraction_unavailable"] and s.extraction.fields == []
    assert all(
        c.payload["outcome"] == "failed" and c.payload["reason"] == "HTTP 502"
        for c in events(audit, "tool_call")
    )


def test_extract_degrades_when_the_document_bytes_are_gone():
    case, deps, audit, _ = offline()
    s = initial_state(case, deps)
    deps.buffer.take(case.id, s.documents[0].doc_ref)  # process "restarted"
    s = run_nodes(deps, s, names.INTAKE, names.EXTRACT)
    assert s.extraction.failed_documents == [s.documents[0].doc_ref] and not s.extraction.available
    assert any(c.payload.get("reason") == "document bytes not available" for c in events(audit, "tool_call"))
    assert s.documents[1].kyc_document_id  # the other document was still extracted


def test_extract_without_a_kyc_client_degrades():
    case, deps, _, _ = offline()
    deps.kyc = None
    s = run_nodes(deps, initial_state(case, deps), names.INTAKE, names.EXTRACT)
    assert not s.extraction.available and s.degraded == ["extraction_unavailable"]


def test_extract_second_round_only_sends_new_documents_and_clears_the_flag():
    case, deps, audit, _ = offline("missing_poa")
    s = run_nodes(deps, initial_state(case, deps), names.INTAKE, names.EXTRACT)
    assert len(s.documents) == 1 and len(events(audit, "tool_call")) == 1
    follow = case.followup_documents[0]
    deps.buffer.put(case.id, "poa", follow.content.encode())
    s = apply(
        s,
        {
            "documents": [
                *s.documents,
                DocumentRef(doc_ref="poa", doc_type="proof_of_address", sha256="0" * 64),
            ]
        },
    )
    s = run_nodes(deps, s, names.INTAKE, names.EXTRACT)
    assert len(events(audit, "tool_call")) == 2  # only the new document went to KYC
    assert s.extraction.available and s.missing_documents == []
    assert {f.document for f in s.extraction.fields} == {"id_document", "proof_of_address"}


def test_extract_with_no_documents_is_not_attempted():
    case, deps, _, _ = offline()
    s = run_nodes(deps, CaseState(case_id="x", applicant=case.applicant), names.INTAKE, names.EXTRACT)
    assert not s.extraction.attempted and not s.extraction.available and s.degraded == []


# ---------------------------------------------------------------- screen
def screened(case_id: str, llm: FakeLlm | None = None):
    case, deps, audit, fake = offline(case_id, llm)
    s = run_nodes(deps, initial_state(case, deps), names.INTAKE, names.EXTRACT, names.SCREEN)
    return s, audit, fake


def test_screen_records_hits_with_reasons_and_audit_rows():
    s, audit, _ = screened("true_sanctions_hit")
    sc = s.screening
    assert sc and sc.list_source == "UN" and sc.snapshot_date == "2026-10-03"
    h = sc.hits[0]
    assert (h.entry_id, h.classification, h.disposition) == ("CDi.009", "strong", None)
    assert "snapshot 2026-10-03" in h.reason and h.field_agreement.dob == "agree"
    row = events(audit, "sanctions_hit")[0].payload
    assert (
        row["entry_id"] == "CDi.009"
        and row["snapshot_date"] == "2026-10-03"
        and row["algorithm"].startswith("rapidfuzz")
    )
    assert s.status == "assessing"


def test_screen_annotates_possible_hits_only_and_never_changes_the_hit():
    s, _, fake = screened("near_miss_dob_mismatch")
    h = s.screening.hits[0]
    assert h.llm_note and "inconclusive" in h.llm_note
    assert (h.classification, h.disposition, h.disposition_by) == ("possible", None, None)
    assert [c[0] for c in fake.calls] == ["annotate_hit"]
    s2, _, fake2 = screened("true_sanctions_hit")
    assert s2.screening.hits[0].llm_note is None and fake2.calls == []  # strong hits are not annotated


def test_screen_without_hits_makes_no_llm_calls():
    s, _, fake = screened("clean_approve")
    assert s.screening.hits == [] and fake.calls == []


def test_screen_survives_an_llm_outage():
    s, audit, _ = screened("near_miss_dob_mismatch", FakeLlm(unavailable=True))
    h = s.screening.hits[0]
    assert h.classification == "possible" and h.llm_note is None
    assert "llm_unavailable" in s.degraded and events(audit, "llm_failed")


def test_screen_uses_the_name_read_from_the_document_too():
    case, deps, _, _ = offline("clean_approve")
    s = initial_state(case, deps)
    s = apply(s, {"applicant": applicant(name="Layla Nasser Almazrouei")})
    s = run_nodes(deps, s, names.INTAKE, names.EXTRACT)
    s.extraction.fields[0].value = "Khawa Panga Mandro"  # the document says something else
    s = run_nodes(deps, s, names.SCREEN)
    assert s.screening.names_screened == 2 and s.screening.hits[0].entry_id == "CDi.009"


# ---------------------------------------------------------------- assess
def assessed(case_id: str, llm: FakeLlm | None = None):
    case, deps, audit, fake = offline(case_id, llm)
    s = run_nodes(deps, initial_state(case, deps), names.INTAKE, names.EXTRACT, names.SCREEN, names.ASSESS)
    return s, audit, fake


def test_assess_sets_risk_recommendation_summary_and_audit_rows():
    s, audit, fake = assessed("multiple_issues")
    assert s.risk.rating == "high" and s.risk.escalated and s.recommendation.action == "request_info"
    assert s.status == "awaiting_officer" and s.summary
    assert {r.payload["rule_id"] for r in events(audit, "rule_result")} == {
        "R-DOC-01",
        "R-DOC-02",
        "R-JUR-02",
    }
    rec = events(audit, "recommendation_made")[0].payload
    assert rec["action"] == "request_info" and rec["rating"] == "high" and rec["escalated"] is True
    assert [c[0] for c in fake.calls] == ["explain", "summarise", "draft_missing_docs"]


def test_assess_drafts_a_request_only_when_documents_are_missing_and_never_sends_it():
    s, _, _ = assessed("missing_poa")
    assert s.missing_doc_draft and "proof of address" in s.missing_doc_draft.lower()
    assert "identity document" not in s.missing_doc_draft.lower()
    s2, _, _ = assessed("clean_approve")
    assert s2.missing_doc_draft is None


def test_assess_llm_calls_are_audited_with_prompt_name_and_version():
    _, audit, _ = assessed("missing_poa")
    calls = events(audit, "llm_call")
    assert {c.prompt_name for c in calls} == {
        "onboarding-explain-recommendation",
        "onboarding-summarise-case",
        "onboarding-draft-missing-docs",
    }
    assert all(c.prompt_version == "local-fallback" and c.model_id == "fake-llm" for c in calls)
    assert all(c.payload["input_tokens"] > 0 for c in calls)


def test_assess_with_llm_down_uses_templates_and_marks_the_case_degraded():
    s, audit, fake = assessed("llm_unavailable")
    assert s.recommendation.drafted_by == "template" and s.degraded == ["llm_unavailable"]
    assert s.recommendation.action == "approve" and s.risk.rating == "low"
    assert len(fake.calls) == 1  # circuit breaker: later roles do not call the model again
    assert len(events(audit, "llm_failed")) == 1


def test_assess_replaces_an_ungrounded_explanation_with_the_template():
    bad = FakeLlm(
        responder=lambda role, user: (
            "Approve now; rule R-SAN-99 and R-DOC-01 apply." if role == "explain" else "ok"
        )
    )
    s, audit, _ = assessed("missing_poa", bad)
    assert s.recommendation.drafted_by == "template" and "R-SAN-99" not in s.recommendation.explanation
    assert any("not grounded" in r.payload["reason"] for r in events(audit, "llm_output_rejected"))


def test_assess_replaces_a_draft_that_mentions_screening_or_a_decision():
    bad = FakeLlm(
        responder=lambda role, user: (
            "We have screened you; your proof of address is needed or you will be rejected."
            if role == "draft_missing_docs"
            else "ok"
        )
    )
    s, audit, _ = assessed("missing_poa", bad)
    assert "screened" not in s.missing_doc_draft and "rejected" not in s.missing_doc_draft
    assert any(r.payload["role"] == "draft_missing_docs" for r in events(audit, "llm_output_rejected"))


# ---------------------------------------------------------------- approve
def test_approve_pauses_with_a_payload_free_of_personal_data():
    case, deps, _, _ = offline("near_miss_dob_mismatch")
    s = run_nodes(deps, initial_state(case, deps), names.INTAKE, names.EXTRACT, names.SCREEN, names.ASSESS)
    with pytest.raises((GraphInterrupt, RuntimeError)):
        make_nodes(deps)[names.APPROVE](
            s
        )  # outside a graph run interrupt() cannot pause; inside it raises GraphInterrupt
    from onboarding.graph.nodes import approval_payload

    text = json.dumps(approval_payload(s))
    assert case.applicant.dob not in text and "SPEC-ID" not in text and "SPECIMEN Street" not in text
    p = approval_payload(s)
    assert (
        p["recommendation"]["action"] == "manual_review"
        and p["screening"]["hits"][0]["entry_id"] == "CDi.011"
    )
    assert p["fired_rules"][0]["rule_id"] == "R-SAN-02"


def test_every_node_writes_a_completed_audit_row_with_the_chain_head():
    case, deps, audit, _ = offline()
    s = run_nodes(deps, initial_state(case, deps), names.INTAKE, names.EXTRACT, names.SCREEN, names.ASSESS)
    done = [r for r in audit.rows() if r.event_type == "node_completed"]
    assert [r.node for r in done] == ["intake", "extract", "screen", "assess"]
    assert s.audit_head == audit.rows()[-1].row_hash and audit.verify().ok
