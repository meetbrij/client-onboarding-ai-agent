"""Run the 12 fixture cases through the real workflow and write a result file (docs/EVALS.md).

    uv run python -m evals.run --deterministic             # fake LLM, recorded KYC responses, no network (CI)
    uv run python -m evals.run --live --kyc-url URL        # live KYC service + Anthropic API; needs KYC_API_KEY / ANTHROPIC_API_KEY

Live mode prints and records exactly what ran: the KYC model the service reports, the LLM model, how many LLM calls fell back
to templates, and the sanctions snapshot. Calls are sequential. A live run never fails the command on a metric below its
threshold (the service may read a document differently); the deterministic run does.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: S404
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from evals.judge import CRITERIA, RUBRIC_VERSION, judge_draft
from evals.metrics import aggregate, evaluate_case
from evals.specimen import specimen_document
from onboarding.fixtures import Case
from onboarding.llm.client import AnthropicLlm, FakeLlm
from onboarding.llm.prompts import LOCAL_VERSION, PROMPT_NAMES
from onboarding.runner import ROOT, RecordingLlm, RunResult, load_fixture_cases, run_case
from onboarding.tools.kyc import KycClient

RESULTS = ROOT / "evals" / "results"
THRESHOLDS = ROOT / "evals" / "thresholds.yaml"
DEAD_KYC = "http://127.0.0.1:9"  # nothing listens here: the kyc_mode "unavailable" cases


def git_sha() -> str:
    try:
        return subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def thresholds_report(metrics: dict[str, Any]) -> dict[str, bool]:
    want = yaml.safe_load(THRESHOLDS.read_text())
    out: dict[str, bool] = {}
    for name, floor in want.items():
        v = metrics.get(name)
        got = v.get("rate") if isinstance(v, dict) and "rate" in v else v
        if isinstance(v, dict) and "of" in v and "rate" not in v:  # draft checks: passed/of
            got = None if v["of"] == 0 else v["passed"] / v["of"]
        out[name] = got is None or got >= floor  # a metric with nothing to measure cannot fail
    return out


def run_deterministic(case: Case) -> tuple[RunResult, dict[str, Any]]:
    result = run_case(case, full=True)
    return result, {"llm_calls": len(result.llm.calls)}


def run_live(case: Case, a: argparse.Namespace) -> tuple[RunResult, dict[str, Any]]:
    kyc = (
        KycClient(DEAD_KYC, None, timeout_s=3.0, max_attempts=1, sleep=lambda _s: None)
        if case.kyc_mode == "unavailable"
        else KycClient(
            a.kyc_url, os.environ.get("KYC_API_KEY"), timeout_s=120.0, max_attempts=3, base_wait_s=2.0
        )
    )
    llm: FakeLlm | RecordingLlm = (
        FakeLlm(unavailable=True)  # the llm_unavailable case is a degrade test, not a model test
        if case.llm_mode == "unavailable"
        else RecordingLlm(AnthropicLlm(a.model, os.environ["ANTHROPIC_API_KEY"], max_attempts=5))
    )
    started = time.monotonic()
    result = run_case(case, llm=llm, full=True, kyc=kyc, doc_factory=specimen_document)
    extra: dict[str, Any] = {"seconds": round(time.monotonic() - started, 1), "llm_calls": len(llm.calls)}
    if isinstance(llm, RecordingLlm):
        extra |= {
            "llm_failures": llm.failures,
            "tokens": {"input": llm.input_tokens, "output": llm.output_tokens},
        }
    extra["kyc_models"] = sorted(
        {str(r.payload["model_id"]) for r in result.audit.rows(case.id) if r.payload.get("model_id")}
    )
    return result, extra


def kyc_model_seen(cases_extra: list[dict[str, Any]]) -> list[str]:
    return sorted({m for e in cases_extra for m in e.get("kyc_models", [])})


def preflight(a: argparse.Namespace) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set (the LLM roles would all fall back to templates).")
    try:
        r = httpx.get(f"{a.kyc_url.rstrip('/')}/healthz", timeout=10.0)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"KYC service not reachable at {a.kyc_url}: {type(exc).__name__}")
    if not os.environ.get("KYC_API_KEY"):
        print("note: KYC_API_KEY is not set; the service rejects calls without it if one is configured.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--deterministic", action="store_true")
    mode.add_argument("--live", action="store_true")
    ap.add_argument("--kyc-url", default=os.environ.get("KYC_BASE_URL", ""))
    ap.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"))
    ap.add_argument("--judge-model", default=os.environ.get("JUDGE_MODEL", "claude-sonnet-5-5"))
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--only", nargs="*", default=None, help="case ids to run")
    ap.add_argument(
        "--resume", type=Path, help="a partial result file: cases already in it are not run again"
    )
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    live = a.live
    if live:
        if not a.kyc_url:
            sys.exit("--kyc-url (or KYC_BASE_URL) is required for --live")
        preflight(a)

    cases = load_fixture_cases()
    ids = a.only or list(cases)
    outcomes: list[dict[str, Any]] = []
    extras: dict[str, dict[str, Any]] = {}
    if a.resume and a.resume.exists():
        prior = json.loads(a.resume.read_text())
        outcomes = prior["cases"]
        extras = {o["id"]: o.get("run", {}) for o in outcomes}
    done = {o["id"] for o in outcomes}
    for cid in ids:
        if cid in done:
            continue
        case = cases[cid]
        result, extra = run_live(case, a) if live else run_deterministic(case)
        o = evaluate_case(case, result)
        o["run"] = extra
        outcomes.append(o)
        extras[cid] = extra
        print(
            f"{'PASS' if o['pass'] else 'FAIL'}  {cid:30s} {o['actual']['recommendation']:14s} {o['actual']['final_status']}"
        )
        for name, ok in o["checks"].items():
            if ok is False:
                print(f"      {name}: got {o['actual'].get(name)} expected {o['expected'].get(name)}")
        result.env.close()
    outcomes.sort(key=lambda o: list(cases).index(o["id"]))

    metrics = aggregate(outcomes)
    judge: dict[str, Any] | None = None
    if live and not a.no_judge:
        jc = AnthropicLlm(a.judge_model, os.environ["ANTHROPIC_API_KEY"], max_attempts=5)
        scored = {
            o["id"]: judge_draft(jc, o["actual"]["missing_documents"], o["actual"]["missing_doc_draft"])
            for o in outcomes
            if o["actual"]["missing_doc_draft"]
        }
        judge = {
            "model": a.judge_model,
            "rubric_version": RUBRIC_VERSION,
            "criteria": CRITERIA,
            "drafts": scored,
        }

    llm_modes = {"fake"} if not live else set()
    for o in outcomes:
        if live:
            llm_modes.add(
                "template"
                if o["run"].get("llm_calls", 0) == 0 or o["actual"]["summary_by"] != "llm"
                else "anthropic"
            )
    manifest = json.loads((ROOT / "data" / "sanctions" / "MANIFEST.json").read_text())
    report = thresholds_report(metrics)
    out = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "git_sha": git_sha(),
        "mode": "live" if live else "deterministic",
        "llm_modes_seen": sorted(llm_modes),
        "llm_model": a.model if live else "fake-llm",
        "kyc": {
            "url_host": httpx.URL(a.kyc_url).host if live else "recorded-fake",
            "models_reported": kyc_model_seen(list(extras.values())),
        },
        "prompt_versions": {n: LOCAL_VERSION for n in sorted(PROMPT_NAMES)},
        "sanctions_snapshot": {"source": manifest["source"], "date": manifest["snapshot_date"]},
        "n_cases": len(outcomes),
        "metrics": metrics,
        "thresholds_met": report,
        "judge": judge,
        "langfuse": "not used in this run",
        "cases": outcomes,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = a.out or RESULTS / f"{out['date']}-{out['mode']}.json"
    path.write_text(json.dumps(out, indent=2, sort_keys=False) + "\n")
    print(
        f"\n{metrics['cases_passed']}/{metrics['cases']} cases pass; thresholds met: {sum(report.values())}/{len(report)}; wrote {path}"
    )
    return 0 if (live or all(report.values())) else 1


if __name__ == "__main__":
    raise SystemExit(main())
