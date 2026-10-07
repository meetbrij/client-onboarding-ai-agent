"""The four advisory LLM roles, with the rules that keep them advisory.

  summarise            a short case summary for the officer
  explain              wording for a recommendation the rules already decided
  draft_missing_docs   a message asking the applicant for missing documents (never sent automatically)
  annotate_hit         an optional note on a possible hit (never changes its class or disposition)

Every call writes an audit row (prompt name and version, model id, token counts) and checks the output
deterministically: an explanation or summary that names a rule that did not fire, or a draft that asks for
the wrong documents or mentions screening, risk or a decision, is rejected and replaced by the template.
If the model is disabled or unavailable the template is used and the case is marked degraded.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from onboarding.audit import AuditEvent, AuditSink
from onboarding.llm.client import LlmClient, LlmUnavailable
from onboarding.llm.facts import (
    DOC_LABELS,
    FORBIDDEN_DRAFT,
    case_facts,
    hit_facts,
    template_draft,
    template_explanation,
    template_summary,
)
from onboarding.llm.prompts import PromptStore
from onboarding.models import Action, CaseState, Hit, Recommendation, Risk
from onboarding.observability import NoopTracer, Tracer

RULE_ID = re.compile(r"R-[A-Z]{3}-\d{2}")
MAX_NOTE = 400


@dataclass
class TextOutcome:
    text: str
    drafted_by: str  # "llm" or "template"


@dataclass
class LlmService:
    client: LlmClient | None
    prompts: PromptStore
    audit: AuditSink
    enabled: bool = True
    annotate_hits: bool = True
    tracer: Tracer = field(default_factory=NoopTracer)
    degraded: set[str] = field(default_factory=set)  # flags raised since the last reset
    _open: bool = False  # circuit breaker: after one outage in a run, stop calling the model

    def reset(self) -> None:
        self.degraded = set()
        self._open = False

    # ---------------- plumbing ----------------
    def _call(self, case_id: str, role: str, prompt_name: str, node: str, **values: str) -> str | None:
        """Return the model text, or None when the template must be used (reason recorded)."""
        if not self.enabled or self.client is None:
            self.degraded.add("llm_disabled")
            return None
        if self._open:
            self.degraded.add("llm_unavailable")
            return None
        prompt = self.prompts.get(prompt_name)
        user = prompt.render(**values)
        started = time.monotonic()
        try:
            with self.tracer.generation(
                role, getattr(self.client, "model_id", "unknown"), prompt.name, prompt.version
            ) as gen:
                result = self.client.complete(
                    role, "You are an assistant to a bank compliance officer.", user
                )
                gen.update(
                    model=result.model_id,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                )
        except LlmUnavailable as exc:
            self._open = True
            self.degraded.add("llm_unavailable")
            self.audit.append(
                AuditEvent(
                    case_id,
                    "llm_failed",
                    node=node,
                    payload={"role": role, "reason": str(exc)[:200]},
                    prompt_name=prompt.name,
                    prompt_version=prompt.version,
                )
            )
            return None
        self.audit.append(
            AuditEvent(
                case_id,
                "llm_call",
                node=node,
                payload={
                    "role": role,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                },
                prompt_name=prompt.name,
                prompt_version=prompt.version,
                model_id=result.model_id,
            )
        )
        return result.text.strip()

    def _reject(self, case_id: str, role: str, node: str, reason: str) -> None:
        self.audit.append(
            AuditEvent(case_id, "llm_output_rejected", node=node, payload={"role": role, "reason": reason})
        )

    # ---------------- roles ----------------
    def summarise(self, state: CaseState, risk: Risk, action: Action) -> TextOutcome:
        text = self._call(
            state.case_id,
            "summarise",
            "onboarding-summarise-case",
            "assess",
            facts=case_facts(state, risk, action),
        )
        if text is not None:
            extra = set(RULE_ID.findall(text)) - {r.rule_id for r in risk.fired_rules}
            if extra:
                self._reject(
                    state.case_id, "summarise", "assess", f"names rules that did not fire: {sorted(extra)}"
                )
                text = None
        return (
            TextOutcome(text, "llm")
            if text
            else TextOutcome(template_summary(state, risk, action), "template")
        )

    def explain(self, state: CaseState, risk: Risk, action: Action) -> Recommendation:
        template = template_explanation(action, risk)
        text = self._call(
            state.case_id,
            "explain",
            "onboarding-explain-recommendation",
            "assess",
            facts=case_facts(state, risk, action),
        )
        if text is None:
            return template
        fired = {r.rule_id for r in risk.fired_rules}
        extra = set(RULE_ID.findall(text)) - fired
        missing = fired - set(RULE_ID.findall(text))
        if extra or missing:
            self._reject(
                state.case_id,
                "explain",
                "assess",
                f"not grounded in the fired rules: extra={sorted(extra)} missing={sorted(missing)}",
            )
            return template
        return Recommendation(action=action, explanation=text, drafted_by="llm")

    def draft_missing_docs(self, state: CaseState) -> TextOutcome | None:
        if not state.missing_documents:
            return None
        labels = ", ".join(DOC_LABELS[d] for d in state.missing_documents)
        text = self._call(
            state.case_id,
            "draft_missing_docs",
            "onboarding-draft-missing-docs",
            "assess",
            applicant_name=state.applicant.name,
            missing=labels,
        )
        if text is not None:
            problem = check_draft(text, state.missing_documents)
            if problem:
                self._reject(state.case_id, "draft_missing_docs", "assess", problem)
                text = None
        return TextOutcome(text, "llm") if text else TextOutcome(template_draft(state), "template")

    def annotate_hit(self, state: CaseState, hit: Hit) -> str | None:
        if not self.annotate_hits or hit.classification != "possible":
            return None
        text = self._call(
            state.case_id, "annotate_hit", "onboarding-annotate-hit", "screen", facts=hit_facts(hit)
        )
        if text is None:
            return None
        if RULE_ID.search(text) or len(text) > MAX_NOTE:
            self._reject(state.case_id, "annotate_hit", "screen", "note names a rule or is too long")
            return None
        return text


def check_draft(text: str, missing: Sequence[str]) -> str | None:
    """Deterministic checks on a drafted request. Returns a problem description, or None when it passes."""
    low = text.lower()
    if found := FORBIDDEN_DRAFT.search(text):
        return f"draft contains forbidden term '{found.group(0).lower()}'"
    for doc, label in DOC_LABELS.items():
        asked = label in low
        if doc in missing and not asked:
            return f"draft does not ask for the {label}"
        if doc not in missing and asked:
            return f"draft asks for a document that is not missing: {label}"
    return None
