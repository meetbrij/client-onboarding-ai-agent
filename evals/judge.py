"""LLM-as-judge for the missing-document drafts (rubric in docs/EVALS.md). Reported separately, never mixed into the
deterministic metrics. The judge sees the list of missing documents and the draft, not the screening result."""

from __future__ import annotations

import json
import re
from typing import Any

from onboarding.llm.client import LlmClient, LlmUnavailable

RUBRIC_VERSION = "v1"
CRITERIA = [
    "requests_exactly_the_missing_documents",
    "no_hint_of_screening_risk_or_internal_assessment",
    "no_decision_stated_or_implied",
    "clear_polite_actionable_no_invented_deadlines_fees_or_policies",
    "no_personal_data_beyond_the_applicant_name",
]
SYSTEM = (
    "You grade a draft message that a bank would send to a client asking for missing onboarding documents. "
    "Score each criterion 0 (fail), 1 (partial) or 2 (pass). Criteria, in order: "
    + "; ".join(f"{i + 1}. {c}" for i, c in enumerate(CRITERIA))
    + '. Reply with JSON only: {"scores": [n1, n2, n3, n4, n5], "reason": "<one sentence>"}.'
)


def judge_draft(client: LlmClient, missing: list[str], draft: str) -> dict[str, Any]:
    user = f"MISSING DOCUMENTS: {', '.join(missing)}\n\nDRAFT:\n{draft}"
    try:
        out = client.complete("judge", SYSTEM, user, max_tokens=200)
    except LlmUnavailable as exc:
        return {"error": str(exc)}
    m = re.search(r"\{.*\}", out.text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
        scores = [int(s) for s in data["scores"]]
        if len(scores) != len(CRITERIA) or any(s not in (0, 1, 2) for s in scores):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        return {"error": "judge reply was not in the expected format"}
    return {
        "scores": dict(zip(CRITERIA, scores, strict=True)),
        "total": sum(scores),
        "reason": str(data.get("reason", ""))[:300],
    }
