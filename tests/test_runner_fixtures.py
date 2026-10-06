"""All 12 fixtures run end to end through the real CaseService: first pause, the scripted officer, any
document round, execution, and the audit trail."""

from __future__ import annotations

import pytest

from onboarding.graph.build import main as cli
from onboarding.graph.serde import checkpoint_serde, state_model_names
from onboarding.models import CaseState
from onboarding.runner import compare_first_pass, compare_full, run_case, trajectory_from_audit
from tests.helpers import CASES


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_fixture_runs_to_the_expected_outcome_and_the_audit_chain_verifies(case_id):
    result = run_case(CASES[case_id], full=True)
    try:
        assert compare_full(CASES[case_id], result) == []
        chain = result.audit.verify()
        assert chain.ok and chain.rows_checked == len(result.audit.rows())
        assert result.state.audit_head in {r.row_hash for r in result.audit.rows()}
    finally:
        result.env.close()


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_first_pause_matches_before_any_human_step(case_id):
    result = run_case(CASES[case_id])
    try:
        assert compare_first_pass(CASES[case_id], result) == []
        assert result.interrupted and result.view.row.status == "awaiting_officer"
        assert result.state.execution is None  # nothing executes without a human decision
    finally:
        result.env.close()


def test_trajectory_comes_from_the_audit_log():
    result = run_case(CASES["clean_approve"])
    assert trajectory_from_audit(result.audit.rows(), result.case_id) == [
        "intake",
        "extract",
        "screen",
        "assess",
        "approve",
    ]
    assert trajectory_from_audit(result.audit.rows(), "someone-else") == []
    result.env.close()


def test_a_full_run_writes_the_expected_audit_event_types_with_prompt_versions():
    result = run_case(CASES["near_miss_dob_mismatch"], full=True)
    types = [r.event_type for r in result.audit.rows()]
    assert types[0] == "case_created" and types[-1] == "node_completed"
    for needed in (
        "tool_call",
        "sanctions_hit",
        "rule_result",
        "llm_call",
        "recommendation_made",
        "approval_requested",
        "decision_received",
        "execute_requested",
        "execute_completed",
    ):
        assert needed in types, needed
    llm_rows = [r for r in result.audit.rows() if r.event_type == "llm_call"]
    assert llm_rows and all(r.prompt_name and r.prompt_version and r.model_id for r in llm_rows)
    decision = next(r for r in result.audit.rows() if r.event_type == "decision_received")
    assert decision.actor == "officer-eval" and decision.payload["dispositions"] == {"CDi.011": "cleared"}
    result.env.close()


def test_a_wrong_expectation_is_reported_not_ignored():
    case = CASES["clean_approve"].model_copy(deep=True)
    case.expected.recommendation = "reject"
    result = run_case(case)
    assert any("recommendation" in p for p in compare_first_pass(case, result))
    result.env.close()


def test_cli_runs_every_fixture_end_to_end_and_exits_zero(capsys):
    assert cli(["--all"]) == 0
    assert (
        "12/12 cases match their expectations end to end; audit chains verified: 12/12"
        in capsys.readouterr().out
    )


def test_cli_single_case(capsys):
    assert cli(["--case", "evals/cases/true_sanctions_hit.yaml"]) == 0
    assert "true_sanctions_hit" in capsys.readouterr().out


def test_checkpoint_serializer_allows_only_the_state_models():
    names = state_model_names()
    assert ("onboarding.models", "CaseState") in names and ("onboarding.models", "Hit") in names
    assert all(module == "onboarding.models" for module, _ in names)
    serde = checkpoint_serde()
    result = run_case(CASES["clean_approve"])
    restored = serde.loads_typed(serde.dumps_typed(result.state))
    assert isinstance(restored, CaseState) and restored == result.state
    result.env.close()
