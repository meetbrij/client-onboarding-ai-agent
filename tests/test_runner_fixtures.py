"""All 12 fixtures run end to end to the first approval pause and match their expectations."""

from __future__ import annotations

import pytest

from onboarding.graph.build import main as cli
from onboarding.graph.serde import checkpoint_serde, state_model_names
from onboarding.models import CaseState
from onboarding.runner import compare_first_pass, run_case, trajectory_from_audit
from tests.helpers import CASES


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_fixture_matches_its_expectations_and_the_audit_chain_verifies(case_id):
    result = run_case(CASES[case_id])
    assert compare_first_pass(CASES[case_id], result) == []
    assert result.interrupted and result.state.status == "awaiting_officer"
    chain = result.audit.verify()
    assert chain.ok and chain.rows_checked == len(result.audit.rows())
    assert result.state.audit_head and result.state.audit_head in {r.row_hash for r in result.audit.rows()}


def test_first_pass_trajectory_comes_from_the_audit_log():
    result = run_case(CASES["clean_approve"])
    assert trajectory_from_audit(result.audit.rows(), result.case_id) == [
        "intake",
        "extract",
        "screen",
        "assess",
        "approve",
    ]
    assert trajectory_from_audit(result.audit.rows(), "someone-else") == []


def test_a_run_writes_the_expected_audit_event_types():
    result = run_case(CASES["near_miss_dob_mismatch"])
    types = [r.event_type for r in result.audit.rows()]
    assert types[0] == "case_created" and types[-1] == "approval_requested"
    for needed in (
        "tool_call",
        "sanctions_hit",
        "rule_result",
        "llm_call",
        "recommendation_made",
        "node_completed",
    ):
        assert needed in types, needed
    llm_rows = [r for r in result.audit.rows() if r.event_type == "llm_call"]
    assert llm_rows and all(r.prompt_name and r.prompt_version and r.model_id for r in llm_rows)


def test_a_wrong_expectation_is_reported_not_ignored():
    case = CASES["clean_approve"].model_copy(deep=True)
    case.expected.recommendation = "reject"
    assert any("recommendation" in p for p in compare_first_pass(case, run_case(case)))


def test_cli_runs_every_fixture_and_exits_zero(capsys):
    assert cli(["--all"]) == 0
    out = capsys.readouterr().out
    assert "12/12 cases match their expectations; audit chains verified: 12/12" in out


def test_cli_single_case(capsys):
    assert cli(["--case", "evals/cases/true_sanctions_hit.yaml"]) == 0
    assert "true_sanctions_hit" in capsys.readouterr().out


def test_checkpoint_serializer_allows_only_the_state_models():
    names = state_model_names()
    assert ("onboarding.models", "CaseState") in names and ("onboarding.models", "Hit") in names
    assert all(module == "onboarding.models" for module, _ in names)
    serde = checkpoint_serde()
    state = run_case(CASES["clean_approve"]).state
    restored = serde.loads_typed(serde.dumps_typed(state))
    assert isinstance(restored, CaseState) and restored == state
