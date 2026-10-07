"""Guards for the two pipeline files (syntax is linted by actionlint in review; these catch the mistakes it cannot)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

QA = Path(".github/workflows/qa-cicd.yml")
PROD = Path(".github/workflows/prod-cd.yaml")
DEPLOY_ONLY = {"push-to-ecr", "deploy-qa"}
# Actions that still run on Node 20 (GitHub forces Node 24 and warns): do not go back to these majors.
NODE20 = {
    "actions/checkout": {"v4", "v3"},
    "actions/upload-artifact": {"v4", "v5", "v3"},
    "actions/download-artifact": {"v4", "v5", "v6", "v3"},
    "actions/setup-python": {"v5", "v4"},
    "aws-actions/configure-aws-credentials": {"v4", "v5"},
    "astral-sh/setup-uv": {"v5", "v6"},
}


def test_no_job_before_the_deploy_stage_can_be_skipped():
    """A skipped job skips every job after it, even when its direct dependency succeeded. That is how an optional
    SonarCloud job once stopped the image push and the deploy. Optional work belongs in conditional steps."""
    jobs = yaml.safe_load(QA.read_text())["jobs"]
    for name, job in jobs.items():
        if name not in DEPLOY_ONLY:
            assert "if" not in job, f"{name} has a job-level condition"
    for name in DEPLOY_ONLY:
        assert "github.event_name == 'push'" in jobs[name]["if"] and "refs/heads/qa" in jobs[name]["if"]


def test_the_deploy_chain_is_connected_to_the_build():
    jobs = yaml.safe_load(QA.read_text())["jobs"]
    assert jobs["docker-build"]["needs"] == ["lint", "test", "sonarcloud"]
    assert (
        set(jobs["push-to-ecr"]["needs"]) == {"trivy-image", "sbom"}
        and jobs["deploy-qa"]["needs"] == "push-to-ecr"
    )


def test_the_optional_sonarcloud_job_runs_but_its_steps_are_conditional():
    steps = yaml.safe_load(QA.read_text())["jobs"]["sonarcloud"]["steps"]
    assert steps and all("vars.SONAR_ENABLED" in s["if"] for s in steps)


def test_actions_are_not_on_deprecated_node_runtimes():
    for path in (QA, PROD):
        for action, version in re.findall(r"uses:\s*([\w./-]+)@(v\d+)", path.read_text()):
            assert version not in NODE20.get(action, set()), (path.name, action, version)


def test_prod_workflow_never_builds_or_scans():
    text = PROD.read_text()
    assert "docker build" not in text and "trivy" not in text.lower() and "pytest" not in text
    assert "environment:" in text and "put-image" in text
