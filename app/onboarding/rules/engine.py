"""Deterministic risk rules, rating and recommendation (DECISIONS D-14 and D-19).

Every rule is a pure function of the case facts. A rule that fires returns a FiredRule with its inputs
and a fixed-text explanation, so the officer page, the audit log and the LLM wording all restate the
same facts. Nothing here calls or reads an LLM.

Rule ids and severities
  R-SAN-01 high    a strong sanctions hit
  R-SAN-02 medium  a possible sanctions hit
  R-JUR-01 high    residence in an FATF call-for-action jurisdiction
  R-JUR-02 medium  residence in an FATF increased-monitoring jurisdiction
  R-OCC-01 medium  occupation in a higher-risk category
  R-DOC-01 medium  a required document was not provided
  R-DOC-02 medium  an extracted field is missing, malformed or low-confidence
  R-DOC-03 high    document extraction was unavailable
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from onboarding.models import (
    Action,
    CaseState,
    FiredRule,
    Rating,
    Risk,
)
from onboarding.rules.reference import Reference
from onboarding.screening.normalize import normalize_name

RULE_IDS = (
    "R-SAN-01",
    "R-SAN-02",
    "R-JUR-01",
    "R-JUR-02",
    "R-OCC-01",
    "R-DOC-01",
    "R-DOC-02",
    "R-DOC-03",
)
_ORDER: dict[Rating, int] = {"low": 0, "medium": 1, "high": 2}
_LEVELS: tuple[Rating, ...] = ("low", "medium", "high")
ESCALATION_RULE_COUNT = 3


@dataclass(frozen=True)
class RuleContext:
    state: CaseState
    reference: Reference


def r_san_01(c: RuleContext) -> FiredRule | None:
    hits = [h for h in (c.state.screening.hits if c.state.screening else []) if h.classification == "strong"]
    if not hits:
        return None
    ids = ", ".join(h.entry_id for h in hits)
    return FiredRule(
        rule_id="R-SAN-01",
        severity="high",
        inputs={"strong_hits": len(hits), "entry_ids": ids},
        explanation=f"{len(hits)} strong sanctions hit(s) ({ids}): name matches closely and date of birth "
        "and nationality do not disagree with the listed entry.",
    )


def r_san_02(c: RuleContext) -> FiredRule | None:
    hits = [
        h for h in (c.state.screening.hits if c.state.screening else []) if h.classification == "possible"
    ]
    if not hits:
        return None
    ids = ", ".join(h.entry_id for h in hits)
    return FiredRule(
        rule_id="R-SAN-02",
        severity="medium",
        inputs={"possible_hits": len(hits), "entry_ids": ids},
        explanation=f"{len(hits)} possible sanctions hit(s) ({ids}): similar name, but a lower score or a "
        "date of birth or nationality that disagrees with the listed entry. "
        "An officer must clear or confirm each.",
    )


def r_jur_01(c: RuleContext) -> FiredRule | None:
    country = c.state.applicant.residence_country
    if normalize_name(country) not in c.reference.call_for_action:
        return None
    return FiredRule(
        rule_id="R-JUR-01",
        severity="high",
        inputs={
            "residence_country": country,
            "list": "FATF call for action",
            "list_as_of": c.reference.jurisdictions_as_of,
        },
        explanation=f"Residence country {country} is on the FATF call-for-action list "
        f"(as of {c.reference.jurisdictions_as_of}).",
    )


def r_jur_02(c: RuleContext) -> FiredRule | None:
    country = c.state.applicant.residence_country
    if normalize_name(country) not in c.reference.increased_monitoring:
        return None
    return FiredRule(
        rule_id="R-JUR-02",
        severity="medium",
        inputs={
            "residence_country": country,
            "list": "FATF increased monitoring",
            "list_as_of": c.reference.jurisdictions_as_of,
        },
        explanation=f"Residence country {country} is on the FATF increased-monitoring list "
        f"(as of {c.reference.jurisdictions_as_of}).",
    )


def r_occ_01(c: RuleContext) -> FiredRule | None:
    occupation = normalize_name(c.state.applicant.occupation)
    for cat in c.reference.occupations:
        if any(k and k in occupation for k in cat.keywords):
            return FiredRule(
                rule_id="R-OCC-01",
                severity="medium",
                inputs={"occupation": c.state.applicant.occupation, "category": cat.category},
                explanation=f"Declared occupation '{c.state.applicant.occupation}' falls in the higher-risk "
                f"category {cat.category}.",
            )
    return None


def r_doc_01(c: RuleContext) -> FiredRule | None:
    missing = c.state.missing_documents
    if not missing:
        return None
    names = ", ".join(missing)
    return FiredRule(
        rule_id="R-DOC-01",
        severity="medium",
        inputs={"missing_count": len(missing), "missing": names},
        explanation=f"Required document(s) not provided: {names}.",
    )


def r_doc_02(c: RuleContext) -> FiredRule | None:
    flagged = [f for f in c.state.extraction.fields if f.needs_review]
    if not flagged:
        return None
    names = ", ".join(f"{f.document}.{f.name}" for f in flagged)
    reasons = ", ".join(sorted({f.reason or "unspecified" for f in flagged}))
    return FiredRule(
        rule_id="R-DOC-02",
        severity="medium",
        inputs={"flagged_fields": len(flagged), "fields": names, "reasons": reasons},
        explanation=f"{len(flagged)} extracted field(s) need review ({reasons}): {names}.",
    )


def r_doc_03(c: RuleContext) -> FiredRule | None:
    ex = c.state.extraction
    if not ex.attempted or ex.available:
        return None
    return FiredRule(
        rule_id="R-DOC-03",
        severity="high",
        inputs={"failed_documents": len(ex.failed_documents)},
        explanation="Document extraction was unavailable for "
        f"{len(ex.failed_documents)} document(s); identity details could not be checked "
        "against the documents.",
    )


RULES: tuple[Callable[[RuleContext], FiredRule | None], ...] = (
    r_san_01,
    r_san_02,
    r_jur_01,
    r_jur_02,
    r_occ_01,
    r_doc_01,
    r_doc_02,
    r_doc_03,
)


def evaluate_rules(state: CaseState, reference: Reference) -> list[FiredRule]:
    ctx = RuleContext(state, reference)
    fired = [r for rule in RULES if (r := rule(ctx)) is not None]
    return sorted(fired, key=lambda r: r.rule_id)


def rate(fired: list[FiredRule]) -> Risk:
    """Highest severity among fired rules, raised one level when three or more distinct rules fire."""
    if not fired:
        return Risk(rating="low", fired_rules=[])
    top = max(_ORDER[r.severity] for r in fired)
    escalated = len({r.rule_id for r in fired}) >= ESCALATION_RULE_COUNT and top < _ORDER["high"]
    return Risk(
        rating=_LEVELS[min(top + (1 if escalated else 0), 2)],
        fired_rules=fired,
        escalated=escalated,
    )


def recommend_action(risk: Risk) -> Action:
    """Precedence (DECISIONS D-14): strong hit reject; missing documents request_info; any other flag,
    or a rating above low, manual_review; otherwise approve. A human still decides in every case."""
    ids = {r.rule_id for r in risk.fired_rules}
    if "R-SAN-01" in ids:
        return "reject"
    if "R-DOC-01" in ids:
        return "request_info"
    if ids or risk.rating != "low":
        return "manual_review"
    return "approve"


def assess_case(state: CaseState, reference: Reference) -> tuple[Risk, Action]:
    risk = rate(evaluate_rules(state, reference))
    return risk, recommend_action(risk)
