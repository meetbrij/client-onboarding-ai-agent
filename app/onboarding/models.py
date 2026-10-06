"""Case state and its parts. Everything here is written to every LangGraph checkpoint, so it holds
references and decisions, never document bytes. Extracted values are synthetic in this project and are
kept out of logs, traces, LLM prompts and audit payloads.

Fields produced by an LLM are listed in LLM_FIELDS. Routing code must never read them (enforced by
tests/test_routes_ignore_llm.py): the LLM never decides.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DocType = Literal["id_document", "proof_of_address"]
REQUIRED_DOCUMENTS: tuple[DocType, ...] = ("id_document", "proof_of_address")

Status = Literal[
    "intake",
    "extracting",
    "screening",
    "assessing",
    "awaiting_officer",
    "awaiting_documents",
    "executing",
    "approved",
    "rejected",
    "failed",
]
Rating = Literal["low", "medium", "high"]
Action = Literal["approve", "reject", "request_info", "manual_review"]
Agreement = Literal["agree", "partial", "disagree", "unknown"]

# Names of state fields whose content comes from an LLM (or its template stand-in). Advisory only.
LLM_FIELDS = frozenset(
    {"summary", "summary_by", "explanation", "missing_doc_draft", "llm_note", "drafted_by"}
)


class Applicant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("dob")
    @classmethod
    def _dob_is_a_plausible_date(cls, v: str) -> str:
        try:
            parsed = date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("date of birth must be an ISO date (YYYY-MM-DD)") from exc
        if not 1900 <= parsed.year <= date.today().year:
            raise ValueError("date of birth is not plausible")
        return v

    name: str
    aliases: list[str] = Field(default_factory=list)
    dob: str  # ISO date
    nationality: str
    residence_country: str
    occupation: str


class DocumentRef(BaseModel):
    doc_ref: str
    doc_type: DocType
    sha256: str
    kyc_document_id: str | None = None


class ExtractedField(BaseModel):
    document: DocType
    name: str
    value: str | None
    confidence: float
    needs_review: bool
    reason: str | None = None


class Extraction(BaseModel):
    attempted: bool = False
    available: bool = False  # True only when every provided document was extracted
    fields: list[ExtractedField] = Field(default_factory=list)
    failed_documents: list[str] = Field(default_factory=list)  # doc_refs the KYC service did not process
    doc_flags: list[str] = Field(default_factory=list)  # document types the KYC service marked needs_review


class FieldAgreement(BaseModel):
    name: Literal["agree"] = "agree"  # a hit is only raised when the name score passes the threshold
    dob: Agreement = "unknown"
    nationality: Agreement = "unknown"


class Hit(BaseModel):
    entry_id: str
    list_source: str
    matched_name: str  # the listed name or alias that matched best
    applicant_name_used: str  # which of the applicant's names produced the score
    score: float
    classification: Literal["strong", "possible"]
    field_agreement: FieldAgreement
    reason: str
    llm_note: str | None = None  # advisory annotation; never changes classification or disposition
    disposition: Literal["cleared", "confirmed"] | None = None
    disposition_by: str | None = None


class Screening(BaseModel):
    list_source: str
    snapshot_date: str
    algorithm: str
    raise_threshold: float
    strong_threshold: float
    names_screened: int = 0
    hits: list[Hit] = Field(default_factory=list)


class FiredRule(BaseModel):
    rule_id: str
    severity: Rating
    inputs: dict[str, str | int | float | bool]
    explanation: str


class Risk(BaseModel):
    rating: Rating
    fired_rules: list[FiredRule] = Field(default_factory=list)
    escalated: bool = False  # raised one level because three or more rules fired (DECISIONS D-19)


class Recommendation(BaseModel):
    action: Action  # set by Python only
    explanation: str  # LLM wording of the rule outputs, or a template
    drafted_by: Literal["llm", "template"] = "template"


class Decision(BaseModel):
    action: Literal["approve", "reject", "request_more_info"]
    officer: str
    note: str | None = None
    at: str


class Execution(BaseModel):
    customer_id: str
    idempotency_key: str


class CaseState(BaseModel):
    case_id: str
    status: Status = "intake"
    applicant: Applicant
    submitted_by: str = "system"
    documents: list[DocumentRef] = Field(default_factory=list)
    extraction: Extraction = Field(default_factory=Extraction)
    missing_documents: list[DocType] = Field(default_factory=list)
    screening: Screening | None = None
    risk: Risk | None = None
    recommendation: Recommendation | None = None
    summary: str | None = None
    summary_by: Literal["llm", "template"] | None = None
    missing_doc_draft: str | None = None  # never sent automatically
    decision: Decision | None = None
    execution: Execution | None = None
    info_rounds: int = 0
    degraded: list[str] = Field(default_factory=list)
    error: str | None = None
    audit_head: str = ""
