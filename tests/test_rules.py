"""One test group per rule: fires, does not fire, boundary; then rating and recommendation."""

from __future__ import annotations

import pytest

from onboarding.llm.facts import template_explanation
from onboarding.models import (
    Applicant,
    CaseState,
    ExtractedField,
    Extraction,
    FieldAgreement,
    FiredRule,
    Hit,
    Screening,
)
from onboarding.rules.engine import (
    RULE_IDS,
    assess_case,
    evaluate_rules,
    rate,
    recommend_action,
)
from onboarding.rules.reference import Reference

REF = Reference.load()


def state(**kw) -> CaseState:
    applicant = Applicant(
        name="Test Person",
        dob="1990-01-01",
        nationality="Utopia",
        residence_country=kw.pop("residence", "United Arab Emirates"),
        occupation=kw.pop("occupation", "Engineer"),
    )
    return CaseState(case_id="c1", applicant=applicant, **kw)


def hit(cls: str, entry="X.001") -> Hit:
    return Hit(
        entry_id=entry,
        list_source="UN",
        matched_name="N",
        applicant_name_used="N",
        score=95.0,
        classification=cls,
        field_agreement=FieldAgreement(),
        reason="r",
    )


def screening(*hits: Hit) -> Screening:
    return Screening(
        list_source="UN",
        snapshot_date="2026-10-03",
        algorithm="a",
        raise_threshold=85,
        strong_threshold=92,
        hits=list(hits),
    )


def field(name="date_of_birth", review=False, reason=None, doc="id_document") -> ExtractedField:
    return ExtractedField(
        document=doc,
        name=name,
        value="v",
        confidence=0.5 if review else 0.97,
        needs_review=review,
        reason=reason,
    )


def ids(s: CaseState) -> list[str]:
    return [r.rule_id for r in evaluate_rules(s, REF)]


def test_clean_case_fires_nothing_and_is_low_approve():
    s = state(
        extraction=Extraction(attempted=True, available=True, fields=[field(), field("id_number")]),
        screening=screening(),
    )
    risk, action = assess_case(s, REF)
    assert (ids(s), risk.rating, action) == ([], "low", "approve")


# --- sanctions ---
def test_r_san_01_fires_only_on_strong_hits():
    assert ids(state(screening=screening(hit("strong")))) == ["R-SAN-01"]
    assert "R-SAN-01" not in ids(state(screening=screening(hit("possible"))))
    assert "R-SAN-01" not in ids(state(screening=None))


def test_r_san_02_fires_only_on_possible_hits_and_both_can_fire():
    assert ids(state(screening=screening(hit("possible")))) == ["R-SAN-02"]
    both = state(screening=screening(hit("strong", "A.1"), hit("possible", "B.2")))
    assert ids(both) == ["R-SAN-01", "R-SAN-02"]
    assert evaluate_rules(both, REF)[0].inputs["entry_ids"] == "A.1"


# --- jurisdiction ---
@pytest.mark.parametrize(
    ("country", "expected"),
    [
        ("Myanmar", ["R-JUR-01"]),
        ("myanmar", ["R-JUR-01"]),
        ("Iran", ["R-JUR-01"]),
        ("Lebanon", ["R-JUR-02"]),
        ("Côte d'Ivoire", ["R-JUR-02"]),
        ("Cote d Ivoire", ["R-JUR-02"]),
        ("United Arab Emirates", []),
        ("Uganda", []),
    ],
)
def test_jurisdiction_rules(country, expected):
    assert ids(state(residence=country)) == expected


def test_jurisdiction_rule_ignores_nationality():
    s = state()
    s.applicant.nationality = "Democratic Republic of the Congo"  # on the increased-monitoring list
    assert ids(s) == []


# --- occupation ---
@pytest.mark.parametrize(
    ("occupation", "fires"),
    [
        ("Gold dealer", True),
        ("Jewellery Trader", True),
        ("Money exchange operator", True),
        ("Crypto trader", True),
        ("Member of Parliament", True),
        ("Software engineer", False),
        ("Accountant", False),
        ("", False),
    ],
)
def test_occupation_rule(occupation, fires):
    assert (ids(state(occupation=occupation)) == ["R-OCC-01"]) is fires


def test_occupation_rule_names_the_category():
    r = evaluate_rules(state(occupation="Gold dealer"), REF)[0]
    assert r.inputs["category"] == "precious_metals_and_stones_dealer"


# --- documents ---
def test_r_doc_01_missing_documents():
    assert ids(state(missing_documents=["proof_of_address"])) == ["R-DOC-01"]
    assert ids(state(missing_documents=[])) == []
    r = evaluate_rules(state(missing_documents=["id_document", "proof_of_address"]), REF)[0]
    assert r.inputs["missing_count"] == 2


def test_r_doc_02_flagged_fields_only_when_flagged():
    ok = Extraction(attempted=True, available=True, fields=[field()])
    bad = Extraction(
        attempted=True, available=True, fields=[field(), field("id_number", True, "low_confidence")]
    )
    assert ids(state(extraction=ok)) == []
    s = state(extraction=bad)
    assert ids(s) == ["R-DOC-02"]
    assert evaluate_rules(s, REF)[0].inputs["fields"] == "id_document.id_number"


def test_r_doc_03_only_when_extraction_was_attempted_and_failed():
    assert ids(state(extraction=Extraction(attempted=True, available=False, failed_documents=["d1"]))) == [
        "R-DOC-03"
    ]
    assert ids(state(extraction=Extraction(attempted=True, available=True))) == []
    assert ids(state(extraction=Extraction(attempted=False))) == []  # nothing provided: R-DOC-01 covers it


def test_unavailable_extraction_does_not_also_fire_doc_02():
    ex = Extraction(attempted=True, available=False, failed_documents=["d1", "d2"])
    assert ids(state(extraction=ex)) == ["R-DOC-03"]


# --- rating (D-19) ---
def fired(*pairs: tuple[str, str]) -> list[FiredRule]:
    return [FiredRule(rule_id=i, severity=s, inputs={}, explanation="e") for i, s in pairs]


def test_rating_is_highest_severity():
    assert rate([]).rating == "low"
    assert rate(fired(("R-OCC-01", "medium"))).rating == "medium"
    assert rate(fired(("R-OCC-01", "medium"), ("R-JUR-01", "high"))).rating == "high"


def test_three_medium_rules_escalate_to_high_but_two_do_not():
    two = rate(fired(("R-DOC-01", "medium"), ("R-DOC-02", "medium")))
    three = rate(fired(("R-DOC-01", "medium"), ("R-DOC-02", "medium"), ("R-JUR-02", "medium")))
    assert (two.rating, two.escalated) == ("medium", False)
    assert (three.rating, three.escalated) == ("high", True)


def test_high_is_the_ceiling():
    r = rate(fired(("R-SAN-01", "high"), ("R-DOC-01", "medium"), ("R-OCC-01", "medium")))
    assert (r.rating, r.escalated) == ("high", False)


# --- recommendation (D-14) ---
def action_for(*pairs: tuple[str, str]) -> str:
    return recommend_action(rate(fired(*pairs)))


def test_recommendation_precedence():
    assert action_for() == "approve"
    assert action_for(("R-SAN-01", "high"), ("R-DOC-01", "medium")) == "reject"
    assert action_for(("R-DOC-01", "medium"), ("R-JUR-01", "high")) == "request_info"
    assert action_for(("R-SAN-02", "medium")) == "manual_review"
    assert action_for(("R-DOC-02", "medium")) == "manual_review"
    assert action_for(("R-DOC-03", "high")) == "manual_review"
    assert action_for(("R-JUR-01", "high")) == "manual_review"


def test_template_explanation_restates_exactly_the_fired_rules():
    risk = rate(fired(("R-DOC-01", "medium"), ("R-JUR-02", "medium")))
    rec = template_explanation("request_info", risk)
    assert rec.drafted_by == "template"
    assert "[R-DOC-01]" in rec.explanation and "[R-JUR-02]" in rec.explanation
    assert "[R-SAN-01]" not in rec.explanation
    assert "No risk rules fired" in template_explanation("approve", rate([])).explanation


def test_rule_ids_are_complete_and_unique():
    assert len(set(RULE_IDS)) == len(RULE_IDS) == 8
