"""The deterministic guard on officer decisions."""

from __future__ import annotations

import pytest

from onboarding.decision import (
    DecisionRequest,
    DocumentsResume,
    ResumeDecision,
    apply_dispositions,
    approval_interrupt_id,
    check_decision,
    documents_interrupt_id,
)
from onboarding.models import (
    CaseState,
    Extraction,
    FieldAgreement,
    Hit,
    Recommendation,
    Risk,
    Screening,
)
from tests.helpers import applicant


def hit(entry="X.1", cls="possible", disposition=None) -> Hit:
    return Hit(
        entry_id=entry,
        list_source="UN",
        matched_name="N",
        applicant_name_used="N",
        score=90.0,
        classification=cls,
        field_agreement=FieldAgreement(),
        reason="r",
        disposition=disposition,
    )


def state(hits=(), rating="low", rec="approve", available=True, missing=(), rounds=0) -> CaseState:
    return CaseState(
        case_id="c1",
        applicant=applicant(),
        extraction=Extraction(attempted=True, available=available),
        missing_documents=list(missing),
        info_rounds=rounds,
        screening=Screening(
            list_source="UN",
            snapshot_date="d",
            algorithm="a",
            raise_threshold=85,
            strong_threshold=92,
            hits=list(hits),
        ),
        risk=Risk(rating=rating),
        recommendation=Recommendation(action=rec, explanation="e"),
    )


def req(action="approve", note=None, **disp) -> DecisionRequest:
    return DecisionRequest(interrupt_id="c1:a0", action=action, note=note, dispositions=disp)


def problems(s: CaseState, r: DecisionRequest, max_rounds=2) -> list[str]:
    return check_decision(s, r, max_rounds)


def test_clean_case_can_be_approved_without_a_note():
    assert problems(state(), req()) == []


def test_approval_needs_available_extraction_and_complete_documents():
    assert any(
        "extraction was unavailable" in p for p in problems(state(available=False), req(note="x" * 12))
    )
    assert any(
        "documents are missing" in p
        for p in problems(state(missing=["proof_of_address"]), req(note="x" * 12))
    )


def test_every_hit_needs_a_disposition_before_approval():
    s = state(hits=[hit("A.1"), hit("B.2")], rating="medium", rec="manual_review")
    assert any("A.1, B.2" in p for p in problems(s, req(note="x" * 12)))
    assert any("B.2" in p for p in problems(s, req(note="x" * 12, **{"A.1": "cleared"})))
    assert problems(s, req(note="Both are different people", **{"A.1": "cleared", "B.2": "cleared"})) == []


def test_a_confirmed_match_cannot_be_approved():
    s = state(hits=[hit("A.1", "strong")], rating="high", rec="reject")
    out = problems(s, req(note="x" * 12, **{"A.1": "confirmed"}))
    assert any("confirmed sanctions match cannot be approved" in p for p in out)


def test_disposition_recorded_earlier_counts():
    s = state(hits=[hit("A.1", disposition="cleared")], rating="medium", rec="manual_review")
    assert problems(s, req(note="Checked again against the file")) == []


@pytest.mark.parametrize(
    ("rating", "rec", "needs_note"),
    [
        ("low", "approve", False),
        ("medium", "approve", True),
        ("low", "manual_review", True),
        ("low", "request_info", True),
        ("high", "reject", True),
    ],
)
def test_a_note_is_required_when_overriding_or_above_low_risk(rating, rec, needs_note):
    s = state(rating=rating, rec=rec)
    assert any("note of at least" in p for p in problems(s, req())) is needs_note
    assert not any("note of at least" in p for p in problems(s, req(note="Reviewed the full file")))


def test_a_short_note_does_not_count():
    s = state(rating="medium", rec="manual_review")
    assert any("note of at least" in p for p in problems(s, req(note="ok")))


def test_clearing_a_hit_needs_a_note_even_at_low_rating():
    s = state(hits=[hit("A.1")], rating="low", rec="approve")
    assert any("note of at least" in p for p in problems(s, req(**{"A.1": "cleared"})))


def test_dispositions_for_unknown_entries_are_refused():
    assert any(
        "not hits on this case" in p for p in problems(state(), req(note="x" * 12, **{"Z.9": "cleared"}))
    )


def test_request_more_info_is_limited_by_the_round_cap():
    assert problems(state(rounds=1), req("request_more_info")) == []
    assert any("limit of 2" in p for p in problems(state(rounds=2), req("request_more_info")))
    assert any("limit of 1" in p for p in problems(state(rounds=1), req("request_more_info"), max_rounds=1))


def test_reject_is_always_possible_but_strong_hits_need_a_disposition():
    assert problems(state(), req("reject")) == []
    s = state(hits=[hit("A.1", "strong")], rating="high", rec="reject")
    assert any("A.1" in p for p in problems(s, req("reject")))
    assert problems(s, req("reject", **{"A.1": "confirmed"})) == []


def test_apply_dispositions_records_who_and_leaves_other_fields_alone():
    s = state(hits=[hit("A.1"), hit("B.2")])
    out = apply_dispositions(s.screening, {"A.1": "cleared"}, "officer-1")
    a, b = out.hits
    assert (a.disposition, a.disposition_by, a.classification) == ("cleared", "officer-1", "possible")
    assert (b.disposition, b.disposition_by) == (None, None)
    assert s.screening.hits[0].disposition is None  # the original is untouched


def test_request_models_reject_unknown_fields_and_bad_actions():
    with pytest.raises(ValueError):
        DecisionRequest(interrupt_id="x", action="maybe")
    with pytest.raises(ValueError):
        DecisionRequest(interrupt_id="x", action="approve", surprise=1)
    with pytest.raises(ValueError):
        DocumentsResume(interrupt_id="x", documents=[], extra=1)
    assert ResumeDecision(interrupt_id="x", action="reject", officer="o", at="t").officer == "o"


def test_interrupt_ids_are_stable_per_round():
    assert approval_interrupt_id("c", 0) == "c:a0" and approval_interrupt_id("c", 1) == "c:a1"
    assert documents_interrupt_id("c", 1) == "c:d1"


@pytest.mark.parametrize("dob", ["not-a-date", "1990-13-01", "1899-12-31", "2999-01-01", "", "90-01-01"])
def test_the_applicant_model_rejects_implausible_birth_dates(dob):
    with pytest.raises(ValueError):
        applicant(dob=dob)


def test_the_applicant_model_accepts_a_normal_date():
    assert applicant(dob="1990-06-12").dob == "1990-06-12"
