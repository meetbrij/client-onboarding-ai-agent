"""Tracing interface. A trace per case (id derived from the case id), a span per node, tool spans for the KYC
and bank calls, and a generation per LLM call that records the model, token counts and the prompt name
and version. Implementations: `NoopTracer` (default, and what tests use) and `LangfuseTracer`
(onboarding/langfuse_tracing.py). Tracing must never fail a case: implementations swallow their own errors.

Privacy: attributes passed here carry ids, counts, rule ids, scores and prompt names only. Never put a date
of birth, ID number, address, document content or an officer's free-text note in a span.
"""

from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Protocol


class SpanHandle(Protocol):
    def update(self, **attributes: Any) -> None: ...


class Tracer(Protocol):
    def run(self, case_id: str, segment: str) -> AbstractContextManager[SpanHandle]:
        """One worker run of a case (start, resume, recover): the root span inside the case's trace."""
        ...

    def span(self, name: str, **attributes: Any) -> AbstractContextManager[SpanHandle]: ...

    def generation(
        self, name: str, model: str, prompt_name: str, prompt_version: str, **attributes: Any
    ) -> AbstractContextManager[SpanHandle]: ...


class _NoopSpan:
    def update(self, **attributes: Any) -> None:
        return None


class NoopTracer:
    def run(self, case_id: str, segment: str) -> AbstractContextManager[SpanHandle]:
        return nullcontext(_NoopSpan())

    def span(self, name: str, **attributes: Any) -> AbstractContextManager[SpanHandle]:
        return nullcontext(_NoopSpan())

    def generation(
        self, name: str, model: str, prompt_name: str, prompt_version: str, **attributes: Any
    ) -> AbstractContextManager[SpanHandle]:
        return nullcontext(_NoopSpan())


def trace_id_for(case_id: str) -> str:
    """Langfuse trace ids are 32 lowercase hex characters: a stable function of the case id, so a case's
    trace can be found from its id."""
    candidate = case_id.replace("-", "").lower()
    if len(candidate) == 32 and all(c in "0123456789abcdef" for c in candidate):
        return candidate
    return hashlib.sha256(case_id.encode()).hexdigest()[:32]
