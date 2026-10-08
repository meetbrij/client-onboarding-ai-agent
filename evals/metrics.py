"""Per-case checks and aggregate metrics for the evals (definitions in docs/EVALS.md).

Everything here is deterministic Python over a finished run: nothing reads LLM output to decide a check, except the
draft checks, which test the drafted text against the list of missing documents.
"""

from __future__ import annotations

from typing import Any

from onboarding.fixtures import Case
from onboarding.llm.service import check_draft
from onboarding.runner import RunResult

CHECKS = [
    "recommendation",
    "risk_rating",
    "final_status",
    "hits",
    "fired_rules",
    "trajectory",
    "required_steps",
    "degraded",
    "invariants",
]


def _subsequence(needed: list[str], actual: list[str]) -> bool:
    it = iter(actual)
    return all(step in it for step in needed)


def _norm(v: str | None) -> str:
    return " ".join((v or "").lower().split())


def evaluate_case(case: Case, result: RunResult) -> dict[str, Any]:
    e = case.expected
    first = result.first_pass
    row = result.view.row
    rec = first.recommendation.action if first and first.recommendation else None
    rating = first.risk.rating if first and first.risk else None
    hits = sorted(
        (h.entry_id, h.classification) for h in (first.screening.hits if first and first.screening else [])
    )
    rules = sorted(r.rule_id for r in (first.risk.fired_rules if first and first.risk else []))
    degraded = sorted(first.degraded) if first else []
    chain = result.audit.verify()
    state = result.view.state
    executed = state is not None and state.execution is not None
    human_approved = state is not None and state.decision is not None and state.decision.action == "approve"
    invariants = (
        chain.ok
        and (not executed or human_approved)
        and ("execute" not in result.trajectory or human_approved)
    )
    want_hits = sorted((h.entry_id, h.cls) for h in e.hits)
    checks = {
        "recommendation": rec == e.recommendation,
        "risk_rating": rating == e.risk_rating,
        "final_status": row.status == e.final_status,
        "hits": hits == want_hits,
        "fired_rules": None if e.fired_rules is None else rules == sorted(e.fired_rules),
        "trajectory": result.trajectory == e.trajectory,
        "required_steps": _subsequence(e.required_steps, result.trajectory),
        "degraded": degraded == sorted(e.degraded),
        "invariants": invariants,
    }
    draft = first.missing_doc_draft if first else None
    draft_ok = None if draft is None else check_draft(draft, first.missing_documents if first else []) is None
    # How closely the live service read the document, against the values printed on the specimen. Information only.
    matched = total = 0
    if first:
        for f in first.extraction.fields:
            rec_doc = case.kyc_response.get(f.document)  # type: ignore[call-overload]
            want = next((x.value for x in (rec_doc.fields if rec_doc else []) if x.name == f.name), None)
            if want:
                total += 1
                matched += int(_norm(f.value) == _norm(want))
    return {
        "id": case.id,
        "pass": all(v for v in checks.values() if v is not None),
        "checks": checks,
        "draft_deterministic_ok": draft_ok,
        "actual": {
            "recommendation": rec,
            "risk_rating": rating,
            "hits": [list(h) for h in hits],
            "fired_rules": rules,
            "degraded": degraded,
            "trajectory": result.trajectory,
            "final_status": row.status,
            "summary_by": first.summary_by if first else None,
            "drafted_by": first.recommendation.drafted_by if first and first.recommendation else None,
            "missing_documents": list(first.missing_documents) if first else [],
            "missing_doc_draft": draft,
            "extraction_fields_matching_specimen": [matched, total],
        },
        "expected": {
            "recommendation": e.recommendation,
            "risk_rating": e.risk_rating,
            "hits": [list(h) for h in want_hits],
            "fired_rules": e.fired_rules,
            "degraded": sorted(e.degraded),
            "trajectory": e.trajectory,
            "final_status": e.final_status,
        },
        "audit_chain_ok": chain.ok,
    }


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else round(num / den, 4)


def aggregate(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(outcomes)

    def acc(check: str) -> dict[str, Any]:
        vals = [o["checks"][check] for o in outcomes if o["checks"][check] is not None]
        return {"passed": sum(vals), "of": len(vals), "rate": _ratio(sum(vals), len(vals))}

    tp = fp = fn = stp = sfp = sfn = 0
    for o in outcomes:
        got = {tuple(h) for h in o["actual"]["hits"]}
        want = {tuple(h) for h in o["expected"]["hits"]}
        tp += len({h[0] for h in got} & {h[0] for h in want})
        fp += len({h[0] for h in got} - {h[0] for h in want})
        fn += len({h[0] for h in want} - {h[0] for h in got})
        gs = {h[0] for h in got if h[1] == "strong"}
        ws = {h[0] for h in want if h[1] == "strong"}
        stp, sfp, sfn = stp + len(gs & ws), sfp + len(gs - ws), sfn + len(ws - gs)
    drafts = [o["draft_deterministic_ok"] for o in outcomes if o["draft_deterministic_ok"] is not None]
    em = [o["actual"]["extraction_fields_matching_specimen"] for o in outcomes]
    return {
        "cases": n,
        "cases_passed": sum(o["pass"] for o in outcomes),
        "recommendation_accuracy": acc("recommendation"),
        "risk_rating_accuracy": acc("risk_rating"),
        "final_status_accuracy": acc("final_status"),
        "fired_rules_match": acc("fired_rules"),
        "trajectory_exact": acc("trajectory"),
        "required_steps_present": acc("required_steps"),
        "degrade_correctness": acc("degraded"),
        "invariants": acc("invariants"),
        "sanctions_recall": _ratio(tp, tp + fn),
        "sanctions_precision": _ratio(tp, tp + fp),
        "sanctions_recall_strong": _ratio(stp, stp + sfn),
        "sanctions_precision_strong": _ratio(stp, stp + sfp),
        "hit_counts": {"tp": tp, "fp": fp, "fn": fn},
        "draft_deterministic_checks": {"passed": sum(drafts), "of": len(drafts)},
        "extraction_fields_matching_specimen": {
            "matched": sum(m for m, _ in em),
            "of": sum(t for _, t in em),
        },
    }
