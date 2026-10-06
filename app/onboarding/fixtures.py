"""Schema and loader for the synthetic onboarding cases in evals/cases/.

Every case is a SPECIMEN: synthetic applicants and synthetic documents. A case carries a
recorded KYC response shaped like P3's DocumentOut so the graph can be tested without the live
KYC service. See docs/EVALS.md for the format and the meaning of each expected field.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from onboarding.models import Applicant

DocType = Literal["id_document", "proof_of_address"]
Recommendation = Literal["approve", "reject", "request_info", "manual_review"]
Rating = Literal["low", "medium", "high"]
Action = Literal["approve", "reject", "request_more_info"]
FinalStatus = Literal["approved", "rejected", "awaiting_documents"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseDocument(_Strict):
    doc_type: DocType
    content: str  # text standing in for the file; must contain SPECIMEN


class KycField(_Strict):
    name: str
    value: str | None
    confidence: float
    needs_review: bool
    reason: str | None = None
    reviewed: bool = False


class KycResponse(_Strict):
    """Shape of P3's DocumentOut (id and created_at are filled in by the fake service)."""

    document_type: DocType
    status: Literal["extracted", "needs_review", "reviewed"]
    needs_review: bool
    model_id: str = "SPECIMEN-recorded-response"
    fields: list[KycField]


class HumanStep(_Strict):
    action: Action
    dispositions: dict[str, Literal["cleared", "confirmed"]] = Field(default_factory=dict)
    note: str | None = None


class ExpectedHit(_Strict):
    entry_id: str
    cls: Literal["strong", "possible"] = Field(alias="class")


class Expected(_Strict):
    recommendation: Recommendation
    risk_rating: Rating
    hits: list[ExpectedHit] = Field(default_factory=list)
    fired_rules: list[str] | None = None
    final_status: FinalStatus
    degraded: list[str] = Field(default_factory=list)
    trajectory: list[str]
    required_steps: list[str]


class Case(_Strict):
    id: str
    specimen: Literal[True]
    description: str
    applicant: Applicant
    documents: list[CaseDocument]
    followup_documents: list[CaseDocument] = Field(default_factory=list)
    kyc_mode: Literal["ok", "unavailable"] = "ok"
    llm_mode: Literal["ok", "unavailable"] = "ok"
    kyc_response: dict[DocType, KycResponse] = Field(default_factory=dict)
    human_script: list[HumanStep]
    expected: Expected


def load_case(path: Path) -> Case:
    return Case.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_cases(directory: Path) -> dict[str, Case]:
    cases = [load_case(p) for p in sorted(directory.glob("*.yaml"))]
    return {c.id: c for c in cases}
