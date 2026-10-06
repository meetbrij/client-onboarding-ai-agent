"""Routing functions: the only place the graph chooses a path.

Routes read deterministic facts only. They must never read a field in `onboarding.models.LLM_FIELDS`
(summary, explanation, missing_doc_draft, llm_note, drafted_by): tests/test_routes_ignore_llm.py parses
this file and fails if they do. Keep every conditional edge in this module.
"""

from __future__ import annotations

from langgraph.graph import END

from onboarding.graph import names
from onboarding.models import CaseState


def route_after_intake(state: CaseState) -> str:
    return END if state.status == "failed" else names.EXTRACT
