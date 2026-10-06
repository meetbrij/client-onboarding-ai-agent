"""Plain-text facts handed to the LLM, and the deterministic templates used instead of it.

Privacy: the facts contain rule outputs, hit reasons (listed names, scores, agreement words), counts and
the applicant's synthetic name. They never contain a date of birth, an ID number, an address line or any
extracted field value (tests/test_prompt_payloads.py checks this on every fixture).
"""

from __future__ import annotations

import re

from onboarding.models import Action, CaseState, Hit, Recommendation, Risk

DOC_LABELS = {"id_document": "identity document", "proof_of_address": "proof of address"}
# Words a message to the applicant must not contain: no hint of screening, risk or a decision.
FORBIDDEN_DRAFT = re.compile(
    r"\b(sanction\w*|screen\w*|risk\w*|rating|hits?|approv\w*|reject\w*|declin\w*|rules?|compliance)\b",
    re.IGNORECASE,
)


def hit_facts(h: Hit) -> str:
    fa = h.field_agreement
    return (
        f"entry {h.entry_id} ({h.list_source} list): name score {h.score:g}, classified {h.classification}; "
        f"applicant name used '{h.applicant_name_used}', listed name '{h.matched_name}'; "
        f"date of birth {fa.dob}; nationality {fa.nationality}."
    )


def case_facts(state: CaseState, risk: Risk, action: Action) -> str:
    ex = state.extraction
    lines = [
        f"Case: {state.case_id}",
        f"Applicant: {state.applicant.name}",
        "Documents provided: " + (", ".join(d.doc_type for d in state.documents) or "none"),
        "Documents missing: " + (", ".join(state.missing_documents) or "none"),
    ]
    if not ex.attempted:
        lines.append("Extraction: not attempted")
    elif not ex.available:
        lines.append(f"Extraction: UNAVAILABLE for {len(ex.failed_documents)} document(s)")
    else:
        flagged = [f"{f.document}.{f.name} ({f.reason})" for f in ex.fields if f.needs_review]
        lines.append("Extraction: available; fields needing review: " + (", ".join(flagged) or "none"))
    if state.screening:
        s = state.screening
        lines.append(
            f"Sanctions screening: {s.list_source} list snapshot {s.snapshot_date}, {s.names_screened} name(s) "
            f"screened, {len(s.hits)} hit(s)"
        )
        lines += [f"  - {hit_facts(h)}" for h in s.hits]
    lines.append(
        f"Risk rating: {risk.rating}" + (" (escalated: three or more rules fired)" if risk.escalated else "")
    )
    if risk.fired_rules:
        lines.append("Fired rules:")
        lines += [f"  - {r.rule_id} ({r.severity}): {r.explanation}" for r in risk.fired_rules]
    else:
        lines.append("Fired rules: none")
    lines.append(f"Deterministic recommendation: {action}")
    if state.degraded:
        lines.append("Degraded components: " + ", ".join(state.degraded))
    return "\n".join(lines)


def template_summary(state: CaseState, risk: Risk, action: Action) -> str:
    n_hits = len(state.screening.hits) if state.screening else 0
    parts = [
        f"Case {state.case_id} for {state.applicant.name}: risk rating {risk.rating}, "
        f"{len(risk.fired_rules)} rule(s) fired"
        + (f" ({', '.join(r.rule_id for r in risk.fired_rules)})" if risk.fired_rules else "")
        + f", {n_hits} sanctions hit(s).",
    ]
    if state.missing_documents:
        parts.append("Missing: " + ", ".join(DOC_LABELS[d] for d in state.missing_documents) + ".")
    if state.extraction.attempted and not state.extraction.available:
        parts.append("Document extraction was unavailable.")
    if state.degraded:
        parts.append("Degraded: " + ", ".join(state.degraded) + ".")
    parts.append(f"Deterministic recommendation: {action}.")
    return " ".join(parts)


def template_draft(state: CaseState) -> str:
    items = "\n".join(f"- {DOC_LABELS[d]}" for d in state.missing_documents)
    return (
        f"Dear {state.applicant.name},\n\nThank you for your application. To continue, please send us the "
        f"following document(s):\n{items}\n\nPlease reply to [bank contact] with the document(s) attached.\n\n"
        "Kind regards"
    )


def template_explanation(action: Action, risk: Risk) -> Recommendation:
    """Deterministic wording of the recommendation, used when the LLM is disabled, down or fails the
    grounding check. Restates exactly the fired rules."""
    if risk.fired_rules:
        listed = " ".join(f"[{r.rule_id}] {r.explanation}" for r in risk.fired_rules)
    else:
        listed = "No risk rules fired."
    esc = " The rating was raised one level because three or more rules fired." if risk.escalated else ""
    text = f"Recommendation: {action}. Risk rating: {risk.rating}.{esc} {listed}"
    return Recommendation(action=action, explanation=text, drafted_by="template")
