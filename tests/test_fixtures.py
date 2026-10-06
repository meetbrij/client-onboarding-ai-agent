"""The 12 synthetic cases must be well formed, marked SPECIMEN, and consistent with the vendored
sanctions snapshot. The matching below is only a consistency check on the fixtures (plain
token_sort_ratio); the product scorer is built and tested in Phase 2."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from rapidfuzz import fuzz

from onboarding.fixtures import Case, load_cases
from onboarding.screening.normalize import normalize_name
from onboarding.screening.unlist import SanctionsIndex

CASES_DIR = Path("evals/cases")
HIT_THRESHOLD = 85.0
NO_HIT_MARGIN = 75.0  # non-hit applicants must stay clearly below the threshold

EXPECTED_IDS = {
    "clean_approve",
    "true_sanctions_hit",
    "near_miss_dob_mismatch",
    "missing_poa",
    "low_confidence_extraction",
    "high_risk_jurisdiction",
    "high_risk_occupation",
    "multiple_issues",
    "kyc_unavailable",
    "llm_unavailable",
    "alias_hit",
    "shared_given_name_no_hit",
}


@pytest.fixture(scope="module")
def cases() -> dict[str, Case]:
    return load_cases(CASES_DIR)


@pytest.fixture(scope="module")
def index() -> SanctionsIndex:
    return SanctionsIndex.load(Path("data/sanctions"))


def best_scores(case: Case, index: SanctionsIndex) -> dict[str, float]:
    queries = [normalize_name(n) for n in [case.applicant.name, *case.applicant.aliases]]
    best: dict[str, float] = {}
    for e in index.entries:
        for n in [e.name, *(a.name for a in e.aliases)]:
            cand = normalize_name(n)
            if not cand:
                continue
            s = max(fuzz.token_sort_ratio(q, cand) for q in queries)
            if s > best.get(e.entry_id, 0.0):
                best[e.entry_id] = s
    return best


def test_all_twelve_cases_present(cases):
    assert set(cases) == EXPECTED_IDS


def test_every_fixture_file_says_specimen():
    for path in CASES_DIR.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "SPECIMEN" in text.splitlines()[0], path
        assert path.stem in text


def test_file_name_matches_id_and_documents_are_specimen(cases):
    for path in CASES_DIR.glob("*.yaml"):
        assert path.stem in cases
    for c in cases.values():
        for d in [*c.documents, *c.followup_documents]:
            assert "SPECIMEN" in d.content and f"case={c.id}" in d.content
        assert c.specimen is True


def test_synthetic_values_are_obviously_fake(cases):
    for c in cases.values():
        for resp in c.kyc_response.values():
            ids = [f.value for f in resp.fields if f.name == "id_number"]
            assert all(v and v.startswith("SPEC-ID-") for v in ids)
        assert date.fromisoformat(c.applicant.dob).year > 1940


def test_kyc_recorded_responses_cover_the_documents(cases):
    for c in cases.values():
        if c.kyc_mode == "unavailable":
            continue
        for d in [*c.documents, *c.followup_documents]:
            assert d.doc_type in c.kyc_response, (c.id, d.doc_type)
        for resp in c.kyc_response.values():
            assert resp.needs_review == any(f.needs_review for f in resp.fields)
            assert resp.status == ("needs_review" if resp.needs_review else "extracted")


def test_trajectory_and_script_are_coherent(cases):
    for c in cases.values():
        e = c.expected
        t = e.trajectory
        assert t[:2] == ["intake", "extract"], c.id
        assert t.count("approve") == len(c.human_script), c.id
        assert t.count("await_docs") == sum(s.action == "request_more_info" for s in c.human_script), c.id
        it = iter(t)
        assert all(step in it for step in e.required_steps), c.id  # ordered subsequence
        if e.final_status == "approved":
            assert t[-1] == "execute" and c.human_script[-1].action == "approve"
        elif e.final_status == "rejected":
            assert t[-1] == "approve" and c.human_script[-1].action == "reject"
        else:
            assert t[-1] == "await_docs"
        if c.kyc_mode == "unavailable":
            assert "extraction_unavailable" in e.degraded and e.risk_rating == "high"
        if c.llm_mode == "unavailable":
            assert "llm_unavailable" in e.degraded


def test_expected_hits_match_the_snapshot(cases, index):
    for c in cases.values():
        scores = best_scores(c, index)
        raised = {k for k, s in scores.items() if s >= HIT_THRESHOLD}
        assert raised == {h.entry_id for h in c.expected.hits}, (c.id, sorted(raised))
        if not raised:
            assert max(scores.values()) < NO_HIT_MARGIN, c.id


def test_hit_classes_follow_the_corroboration(cases, index):
    """strong: DOB and nationality agree with the listed entry; possible: both differ."""
    for c in cases.values():
        for h in c.expected.hits:
            entry = index.get(h.entry_id)
            assert entry is not None
            dob_agrees = any(d.date == c.applicant.dob for d in entry.dobs)
            nat_agrees = c.applicant.nationality in entry.nationalities
            if h.cls == "strong":
                assert dob_agrees and nat_agrees, c.id
            else:
                assert not dob_agrees and not nat_agrees, c.id
            # every expected hit gets an officer disposition before approval or confirmation
            assert any(h.entry_id in s.dispositions for s in c.human_script), (c.id, h.entry_id)


def test_jurisdiction_cases_use_the_reference_lists(cases):
    import yaml

    ref = yaml.safe_load(Path("data/reference/jurisdictions.yaml").read_text(encoding="utf-8"))
    assert cases["high_risk_jurisdiction"].applicant.residence_country in ref["call_for_action"]
    assert cases["multiple_issues"].applicant.residence_country in ref["increased_monitoring"]
    clean = {
        c.applicant.residence_country
        for k, c in cases.items()
        if k in {"clean_approve", "high_risk_occupation", "llm_unavailable"}
    }
    assert clean.isdisjoint(set(ref["call_for_action"]) | set(ref["increased_monitoring"]))
