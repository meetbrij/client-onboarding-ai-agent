"""The deterministic eval run is the CI gate: every metric in evals/thresholds.yaml must be met. Also checks the specimen
renderer and the live-mode helpers that need no network."""

from __future__ import annotations

import io
import json

import pytest
from evals import run as evals_run
from evals.judge import judge_draft
from evals.metrics import aggregate
from evals.specimen import render
from PIL import Image

from onboarding.llm.client import FakeLlm
from onboarding.runner import load_fixture_cases


def test_the_deterministic_eval_meets_every_threshold(tmp_path):
    out = tmp_path / "r.json"
    assert evals_run.main(["--deterministic", "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["mode"] == "deterministic" and data["llm_model"] == "fake-llm" and data["n_cases"] == 12
    assert all(data["thresholds_met"].values()) and data["metrics"]["cases_passed"] == 12
    assert data["metrics"]["sanctions_recall"] == 1.0 and data["metrics"]["sanctions_precision"] == 1.0
    assert data["judge"] is None and data["langfuse"] == "not used in this run"


def test_a_wrong_expectation_is_reported_as_a_failed_case(tmp_path, monkeypatch):
    cases = load_fixture_cases()
    cases["clean_approve"].expected.recommendation = "reject"
    monkeypatch.setattr(evals_run, "load_fixture_cases", lambda: cases)
    out = tmp_path / "r.json"
    assert evals_run.main(["--deterministic", "--only", "clean_approve", "--out", str(out)]) == 1
    data = json.loads(out.read_text())
    assert data["cases"][0]["pass"] is False and data["metrics"]["recommendation_accuracy"]["rate"] == 0.0


def test_resume_does_not_run_finished_cases_again(tmp_path):
    first = tmp_path / "a.json"
    evals_run.main(["--deterministic", "--only", "clean_approve", "--out", str(first)])
    second = tmp_path / "b.json"
    evals_run.main(["--deterministic", "--resume", str(first), "--out", str(second)])
    assert json.loads(second.read_text())["n_cases"] == 12


@pytest.mark.parametrize("case_id", ["near_miss_dob_mismatch", "low_confidence_extraction", "missing_poa"])
def test_specimens_are_images_marked_as_specimens_and_deterministic(case_id):
    case = load_fixture_cases()[case_id]
    for doc in case.documents:
        a, b = render(case, doc.doc_type), render(case, doc.doc_type)
        assert a == b and a.startswith(b"\x89PNG")
        assert Image.open(io.BytesIO(a)).size[0] >= 800 and len(a) < 4 * 1024 * 1024  # under P3's upload cap


def test_every_fixture_document_renders_under_the_upload_cap():
    for case in load_fixture_cases().values():
        for doc in [*case.documents, *case.followup_documents]:
            assert len(render(case, doc.doc_type)) < 4 * 1024 * 1024


def test_the_judge_parses_scores_and_rejects_malformed_replies():
    good = FakeLlm(responder=lambda role, user: '{"scores": [2, 2, 2, 1, 2], "reason": "fine"}')
    assert judge_draft(good, ["proof_of_address"], "Please send it.")["total"] == 9
    bad = FakeLlm(responder=lambda role, user: "looks good to me")
    assert "error" in judge_draft(bad, ["proof_of_address"], "x")
    assert "error" in judge_draft(FakeLlm(unavailable=True), ["proof_of_address"], "x")


def test_aggregate_handles_an_empty_precision_denominator():
    assert aggregate([])["sanctions_recall"] is None
