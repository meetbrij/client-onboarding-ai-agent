from __future__ import annotations

import pytest

from onboarding.audit import MemoryAuditLog
from onboarding.llm.client import BedrockLlm, FakeLlm, LlmUnavailable
from onboarding.llm.prompts import LOCAL_VERSION, PROMPT_NAMES, LocalPromptStore
from onboarding.llm.service import LlmService, check_draft
from onboarding.models import CaseState
from onboarding.rules.engine import assess_case
from onboarding.rules.reference import Reference
from tests.helpers import applicant


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class StubBedrock:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def converse(self, **kwargs):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


OK = {
    "output": {"message": {"content": [{"text": " hello "}]}},
    "usage": {"inputTokens": 12, "outputTokens": 3},
}


def bedrock(outcomes, **kw) -> tuple[BedrockLlm, StubBedrock, list[float]]:
    stub, sleeps = StubBedrock(outcomes), []
    return BedrockLlm("profile-id", "ap-south-1", client=stub, sleep=sleeps.append, **kw), stub, sleeps


def test_bedrock_success_returns_text_and_token_counts():
    llm, stub, _ = bedrock([OK])
    r = llm.complete("explain", "sys", "user")
    assert (r.text, r.model_id, r.input_tokens, r.output_tokens) == ("hello", "profile-id", 12, 3)


def test_bedrock_throttling_is_retried_with_bounded_jittered_backoff():
    llm, stub, sleeps = bedrock(
        [ClientError("ThrottlingException")] * 3 + [OK], base_wait_s=1.0, max_wait_s=4.0
    )
    assert llm.complete("explain", "s", "u").text == "hello"
    assert stub.calls == 4 and len(sleeps) == 3
    assert all(0 <= s <= cap for s, cap in zip(sleeps, [1.0, 2.0, 4.0], strict=True))


def test_bedrock_gives_up_after_the_attempt_budget():
    llm, stub, sleeps = bedrock([ClientError("ThrottlingException")] * 5, max_attempts=5)
    with pytest.raises(LlmUnavailable, match="5 attempts"):
        llm.complete("explain", "s", "u")
    assert stub.calls == 5 and len(sleeps) == 4


def test_bedrock_non_retryable_errors_fail_fast():
    llm, stub, sleeps = bedrock([ClientError("AccessDeniedException")])
    with pytest.raises(LlmUnavailable, match="AccessDeniedException"):
        llm.complete("explain", "s", "u")
    assert stub.calls == 1 and sleeps == []


def test_bedrock_timeouts_are_retried():
    llm, stub, _ = bedrock([TimeoutError(), OK])
    assert llm.complete("explain", "s", "u").text == "hello" and stub.calls == 2


def test_local_prompts_exist_for_every_name_and_carry_a_version():
    store = LocalPromptStore()
    for name in PROMPT_NAMES:
        p = store.get(name)
        assert p.version == LOCAL_VERSION and "{{" in p.template
    with pytest.raises(KeyError):
        store.get("onboarding-unknown")


def test_prompt_render_rejects_unfilled_placeholders():
    p = LocalPromptStore().get("onboarding-draft-missing-docs")
    with pytest.raises(ValueError, match="unfilled"):
        p.render(applicant_name="X")
    assert "X" in p.render(applicant_name="X", missing="proof of address")


def service(llm, enabled=True, annotate=True):
    audit = MemoryAuditLog()
    return LlmService(llm, LocalPromptStore(), audit, enabled=enabled, annotate_hits=annotate), audit


def case_state(**kw) -> CaseState:
    return CaseState(case_id="c1", applicant=applicant(), **kw)


def test_disabled_service_never_calls_the_model_and_flags_it():
    fake = FakeLlm()
    svc, audit = service(fake, enabled=False)
    state = case_state(missing_documents=["proof_of_address"])
    risk, action = assess_case(state, Reference.load())
    out = svc.draft_missing_docs(state)
    assert out and out.drafted_by == "template" and "proof of address" in out.text
    assert svc.explain(state, risk, action).drafted_by == "template"
    assert fake.calls == [] and svc.degraded == {"llm_disabled"} and audit.rows() == []


def test_circuit_breaker_stops_calls_after_the_first_outage_until_reset():
    fake = FakeLlm(unavailable=True)
    svc, audit = service(fake)
    state = case_state(missing_documents=["id_document"])
    risk, action = assess_case(state, Reference.load())
    svc.explain(state, risk, action)
    svc.summarise(state, risk, action)
    svc.draft_missing_docs(state)
    assert len(fake.calls) == 1 and svc.degraded == {"llm_unavailable"}
    svc.reset()
    svc.explain(state, risk, action)
    assert len(fake.calls) == 2


def test_summary_naming_a_rule_that_did_not_fire_is_replaced():
    fake = FakeLlm(responder=lambda role, user: "Summary mentions R-JUR-01 which did not fire.")
    svc, audit = service(fake)
    state = case_state()
    risk, action = assess_case(state, Reference.load())
    out = svc.summarise(state, risk, action)
    assert out.drafted_by == "template" and "R-JUR-01" not in out.text
    assert [r.event_type for r in audit.rows()] == ["llm_call", "llm_output_rejected"]


def test_explanation_must_cite_every_fired_rule():
    fake = FakeLlm(responder=lambda role, user: "The rating is medium because of R-DOC-01.")
    svc, _ = service(fake)
    state = case_state(missing_documents=["proof_of_address"])
    state.applicant.residence_country = "Lebanon"
    risk, action = assess_case(state, Reference.load())
    assert {r.rule_id for r in risk.fired_rules} == {"R-DOC-01", "R-JUR-02"}
    assert svc.explain(state, risk, action).drafted_by == "template"  # R-JUR-02 not cited


def test_annotation_is_skipped_for_strong_hits_when_off_and_when_it_names_a_rule():
    from onboarding.models import FieldAgreement, Hit

    def hit(cls):
        return Hit(
            entry_id="X.1",
            list_source="UN",
            matched_name="N",
            applicant_name_used="N",
            score=90.0,
            classification=cls,
            field_agreement=FieldAgreement(),
            reason="r",
        )

    fake = FakeLlm()
    svc, _ = service(fake)
    state = case_state()
    assert svc.annotate_hit(state, hit("strong")) is None and fake.calls == []
    assert svc.annotate_hit(state, hit("possible"))
    off, _ = service(FakeLlm(), annotate=False)
    assert off.annotate_hit(state, hit("possible")) is None
    bad, _ = service(FakeLlm(responder=lambda r, u: "Cleared by R-SAN-01."))
    assert bad.annotate_hit(state, hit("possible")) is None


@pytest.mark.parametrize(
    ("text", "missing", "ok"),
    [
        ("Please send your proof of address to [bank contact].", ["proof_of_address"], True),
        (
            "Please send your identity document and proof of address.",
            ["id_document", "proof_of_address"],
            True,
        ),
        ("Please send your identity document.", ["proof_of_address"], False),  # wrong document
        (
            "Please send your proof of address and identity document.",
            ["proof_of_address"],
            False,
        ),  # extra document
        ("Your proof of address is needed after our sanctions screening.", ["proof_of_address"], False),
        ("Send your proof of address or we will reject the application.", ["proof_of_address"], False),
        (
            "Within a week please send your proof of address.",
            ["proof_of_address"],
            True,
        ),  # 'within' is not 'hit'
        ("Your risk rating requires a proof of address.", ["proof_of_address"], False),
    ],
)
def test_check_draft(text, missing, ok):
    assert (check_draft(text, missing) is None) is ok
