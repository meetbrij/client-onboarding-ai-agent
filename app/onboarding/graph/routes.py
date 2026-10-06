"""Routing functions: the only place the graph chooses a path.

Routes read deterministic facts only: the case status, the officer's decision and the information-round
count. They must never read a field in `onboarding.models.LLM_FIELDS` (summary, explanation,
missing_doc_draft, llm_note, drafted_by): tests/test_routes_ignore_llm.py parses this file and fails if
they do. Keep every conditional edge in this module.
"""

from __future__ import annotations

from langgraph.graph import END

from onboarding.graph import names
from onboarding.models import CaseState

MAX_INFO_ROUNDS = 2  # default; the graph is built with the configured value


def route_after_intake(state: CaseState) -> str:
    return END if state.status == "failed" else names.EXTRACT


def route_after_approve(state: CaseState) -> str:
    """Only an officer's approval reaches `execute`; a request for more information loops through
    `await_docs` back to intake; anything else ends the run."""
    d = state.decision
    if d is None:
        return END
    if d.action == "approve":
        return names.EXECUTE
    if d.action == "request_more_info":
        return names.AWAIT_DOCS
    return END
