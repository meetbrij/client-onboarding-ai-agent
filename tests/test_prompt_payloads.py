"""No date of birth, ID number or address reaches an LLM prompt, the audit log or the approval payload."""

from __future__ import annotations

import json

import pytest

from onboarding.runner import run_case
from tests.helpers import CASES

FORBIDDEN_FRAGMENTS = ["SPEC-ID-", "SPECIMEN Street", "SPECIMEN - SYNTHETIC TEST DOCUMENT"]


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_prompts_audit_rows_and_payload_carry_no_personal_data(case_id):
    case = CASES[case_id]
    result = run_case(case)
    prompts = "\n".join(user for _role, _sys, user in result.llm.calls)
    audit_text = json.dumps([{**r.__dict__, "ts": str(r.ts)} for r in result.audit.rows()], default=str)
    payload_text = json.dumps(result.approval_payload)
    for label, text in [("prompts", prompts), ("audit", audit_text), ("approval payload", payload_text)]:
        assert case.applicant.dob not in text, (case_id, label, "date of birth")
        for frag in FORBIDDEN_FRAGMENTS:
            assert frag not in text, (case_id, label, frag)
    # extracted values do not appear in the audit log either (names of fields and counts only)
    for resp in case.kyc_response.values():
        for f in resp.fields:
            if f.name in {"id_number", "address_line", "expiry_date", "date_of_birth"} and f.value:
                assert f.value not in audit_text, (case_id, f.name)


def test_the_checkpointed_state_holds_no_document_bytes():
    result = run_case(CASES["clean_approve"])
    dumped = result.state.model_dump_json()
    assert "SYNTHETIC TEST DOCUMENT" not in dumped
    assert all(len(d.sha256) == 64 for d in result.state.documents)
