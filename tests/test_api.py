"""The JSON API: authentication, roles, the decision flow and its error codes, and what each role may see."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from onboarding.api.main import create_app
from onboarding.auth import Principal, TokenStore, hash_token
from onboarding.config import ConfigError, Settings
from onboarding.runner import build_offline_env
from tests.helpers import CASES

SUBMITTER = {"Authorization": "Bearer dev-submitter-token"}  # submitter-1
OFFICER = {"Authorization": "Bearer dev-officer-token"}  # officer-1
OFFICER2 = {"Authorization": "Bearer dev-officer2-token"}


@pytest.fixture
def env():
    e = build_offline_env()
    yield e
    e.close()


@pytest.fixture
def client(env):
    with TestClient(create_app(Settings(environment="test"), service=env.service)) as c:
        yield c


def files_for(case_id: str, followup: bool = False) -> dict:
    case = CASES[case_id]
    docs = case.followup_documents if followup else case.documents
    return {d.doc_type: (f"{d.doc_type}.txt", d.content.encode(), "text/plain") for d in docs}


def submit(client, case_id="clean_approve", headers=SUBMITTER):
    case = CASES[case_id]
    r = client.post(
        "/cases",
        headers=headers,
        data={"applicant": case.applicant.model_dump_json()},
        files=files_for(case_id),
    )
    assert r.status_code == 201, r.text
    return r.json()


def decide(client, case_id, body, headers=OFFICER):
    return client.post(f"/cases/{case_id}/decision", headers=headers, json=body)


# ------------------------------------------------------------------ authentication and roles
def test_healthz_needs_no_token(client):
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}],
)
def test_missing_or_bad_tokens_are_401(client, headers):
    assert client.get("/cases", headers=headers).status_code == 401
    assert client.post("/cases", headers=headers, data={"applicant": "{}"}).status_code == 401


def test_officer_only_endpoints_refuse_a_submitter(client):
    body = submit(client)
    cid = body["case_id"]
    assert (
        decide(client, cid, {"interrupt_id": body["interrupt_id"], "action": "reject"}, SUBMITTER).status_code
        == 403
    )
    assert client.get(f"/cases/{cid}/audit", headers=SUBMITTER).status_code == 403
    assert client.get("/audit/verify", headers=SUBMITTER).status_code == 403


def test_token_store_matches_hashes_only_and_rejects_unknown_roles():
    store = TokenStore.from_json(
        json.dumps([{"id": "o1", "role": "officer", "sha256": hash_token("s3cret").upper()}])
    )
    assert store.authenticate("s3cret") == Principal("o1", "officer")
    assert (
        store.authenticate("S3CRET") is None
        and store.authenticate(None) is None
        and store.authenticate("") is None
    )
    with pytest.raises(ValueError):
        TokenStore.from_json(json.dumps([{"id": "x", "role": "root", "sha256": "00"}]))


def test_qa_and_prod_refuse_to_start_without_real_tokens_and_secrets():
    with pytest.raises(ConfigError, match="ONBOARDING_TOKENS"):
        Settings.from_env({"ENVIRONMENT": "qa"})
    with pytest.raises(ConfigError, match="fake LLM"):
        Settings.from_env({"ENVIRONMENT": "prod", "ONBOARDING_TOKENS": "[]", "SESSION_SECRET": "x"})
    ok = Settings.from_env({"ENVIRONMENT": "qa", "ONBOARDING_TOKENS": "[]", "SESSION_SECRET": "x"})
    assert ok.secure_cookies is True


def test_openapi_is_hidden_in_prod_only(env):
    prod = Settings(
        environment="prod", llm_backend="bedrock", bedrock_model_id="m", tokens_json="[]", session_secret="x"
    )
    with TestClient(create_app(prod, service=env.service, tokens=TokenStore({}))) as c:
        assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


# ------------------------------------------------------------------ creating and reading cases
def test_creating_a_case_returns_its_status_and_pause(client):
    body = submit(client)
    assert body["status"] == "awaiting_officer" and body["waiting_on"] == "approve"
    assert body["interrupt_id"] == f"{body['case_id']}:a0"
    # a submitter does not see the assessment
    assert body["risk_rating"] is None and body["recommendation"] is None and body["approval"] is None


def test_officer_sees_the_full_assessment_and_the_submitter_does_not(client):
    cid = submit(client, "near_miss_dob_mismatch")["case_id"]
    off = client.get(f"/cases/{cid}", headers=OFFICER).json()
    assert off["risk_rating"] == "medium" and off["recommendation"] == "manual_review"
    assert off["approval"]["screening"]["hits"][0]["entry_id"] == "CDi.011"
    assert off["approval"]["fired_rules"][0]["rule_id"] == "R-SAN-02"
    sub = client.get(f"/cases/{cid}", headers=SUBMITTER).json()
    assert sub["risk_rating"] is None and sub["approval"] is None and "CDi.011" not in json.dumps(sub)


def test_a_submitter_cannot_see_someone_elses_case(client, env):
    cid = submit(client)["case_id"]
    other = TokenStore({hash_token("other"): Principal("submitter-2", "submitter")})
    with TestClient(create_app(Settings(environment="test"), service=env.service, tokens=other)) as c2:
        assert c2.get(f"/cases/{cid}", headers={"Authorization": "Bearer other"}).status_code == 404
        assert c2.get("/cases", headers={"Authorization": "Bearer other"}).json() == []


def test_listing_shows_own_cases_to_submitters_and_all_to_officers(client):
    submit(client, "clean_approve")
    submit(client, "true_sanctions_hit")
    assert len(client.get("/cases", headers=OFFICER).json()) == 2
    assert len(client.get("/cases", headers=SUBMITTER).json()) == 2
    assert client.get("/cases", headers=OFFICER2).json()[0]["risk_rating"] is not None


def test_unknown_case_is_404(client):
    assert client.get("/cases/nope", headers=OFFICER).status_code == 404
    assert decide(client, "nope", {"interrupt_id": "x", "action": "reject"}).status_code == 404


@pytest.mark.parametrize(
    "applicant",
    [
        "not json",
        "{}",
        json.dumps({"name": "A"}),
        json.dumps({**CASES["clean_approve"].applicant.model_dump(), "extra": 1}),
    ],
)
def test_bad_applicant_details_are_422(client, applicant):
    r = client.post("/cases", headers=SUBMITTER, data={"applicant": applicant})
    assert r.status_code == 422


def test_empty_and_oversized_files_are_refused(env):
    small = Settings(environment="test", doc_max_bytes=64)
    with TestClient(create_app(small, service=env.service)) as c:
        data = {"applicant": CASES["clean_approve"].applicant.model_dump_json()}
        assert (
            c.post(
                "/cases",
                headers=SUBMITTER,
                data=data,
                files={"id_document": ("a.txt", b"x" * 100, "text/plain")},
            ).status_code
            == 413
        )
        assert (
            c.post(
                "/cases", headers=SUBMITTER, data=data, files={"id_document": ("a.txt", b"", "text/plain")}
            ).status_code
            == 422
        )


# ------------------------------------------------------------------ decisions
def test_full_decision_flow_over_http(client):
    body = submit(client)
    cid = body["case_id"]
    r = decide(client, cid, {"interrupt_id": body["interrupt_id"], "action": "approve"})
    assert (
        r.status_code == 200
        and r.json()["status"] == "approved"
        and r.json()["customer_id"].startswith("CUST-")
    )
    again = decide(client, cid, {"interrupt_id": body["interrupt_id"], "action": "approve"})
    assert again.status_code == 409  # the same decision twice
    assert client.get(f"/cases/{cid}", headers=SUBMITTER).json()["customer_id"] == r.json()["customer_id"]


def test_an_officer_cannot_decide_their_own_case(client):
    body = submit(client, headers=OFFICER)  # officer-1 submits
    r = decide(client, body["case_id"], {"interrupt_id": body["interrupt_id"], "action": "reject"}, OFFICER)
    assert r.status_code == 403
    assert (
        decide(
            client, body["case_id"], {"interrupt_id": body["interrupt_id"], "action": "reject"}, OFFICER2
        ).status_code
        == 200
    )


def test_the_guard_answers_422_with_reasons(client):
    body = submit(client, "near_miss_dob_mismatch")
    r = decide(
        client,
        body["case_id"],
        {"interrupt_id": body["interrupt_id"], "action": "approve", "note": "Looks fine to me"},
    )
    assert r.status_code == 422 and any("CDi.011" in p for p in r.json()["problems"])
    ok = decide(
        client,
        body["case_id"],
        {
            "interrupt_id": body["interrupt_id"],
            "action": "approve",
            "note": "Different person: DOB and nationality differ",
            "dispositions": {"CDi.011": "cleared"},
        },
    )
    assert ok.status_code == 200 and ok.json()["status"] == "approved"


def test_malformed_decisions_are_422(client):
    body = submit(client)
    for bad in (
        {"interrupt_id": body["interrupt_id"], "action": "maybe"},
        {"action": "approve"},
        {"interrupt_id": body["interrupt_id"], "action": "approve", "surprise": 1},
        {"interrupt_id": body["interrupt_id"], "action": "approve", "dispositions": {"X": "wiped"}},
    ):
        assert decide(client, body["case_id"], bad).status_code == 422


def test_a_stale_interrupt_id_is_409(client):
    body = submit(client)
    assert (
        decide(
            client, body["case_id"], {"interrupt_id": f"{body['case_id']}:a5", "action": "approve"}
        ).status_code
        == 409
    )


def test_document_round_over_http(client):
    body = submit(client, "missing_poa")
    cid = body["case_id"]
    r = decide(
        client,
        cid,
        {
            "interrupt_id": body["interrupt_id"],
            "action": "request_more_info",
            "note": "Proof of address needed",
        },
    )
    assert r.json()["status"] == "awaiting_documents" and r.json()["needed_documents"] == ["proof_of_address"]
    iid = r.json()["interrupt_id"]
    assert (
        client.post(
            f"/cases/{cid}/documents",
            headers=SUBMITTER,
            data={"interrupt_id": "wrong"},
            files=files_for("missing_poa", True),
        ).status_code
        == 409
    )
    assert (
        client.post(f"/cases/{cid}/documents", headers=SUBMITTER, data={"interrupt_id": iid}).status_code
        == 422
    )  # nothing attached
    ok = client.post(
        f"/cases/{cid}/documents",
        headers=SUBMITTER,
        data={"interrupt_id": iid},
        files=files_for("missing_poa", True),
    )
    assert ok.status_code == 200 and ok.json()["status"] == "awaiting_officer"
    final = decide(client, cid, {"interrupt_id": ok.json()["interrupt_id"], "action": "approve"})
    assert final.json()["status"] == "approved"


def test_a_stranger_cannot_upload_documents_to_a_case(client, env):
    body = submit(client, "missing_poa")
    other = TokenStore({hash_token("other"): Principal("submitter-2", "submitter")})
    with TestClient(create_app(Settings(environment="test"), service=env.service, tokens=other)) as c2:
        r = c2.post(
            f"/cases/{body['case_id']}/documents",
            headers={"Authorization": "Bearer other"},
            data={"interrupt_id": "x"},
            files=files_for("missing_poa", True),
        )
        assert r.status_code == 404


# ------------------------------------------------------------------ audit and hygiene
def test_audit_endpoints_for_officers(client):
    body = submit(client)
    decide(client, body["case_id"], {"interrupt_id": body["interrupt_id"], "action": "approve"})
    rows = client.get(f"/cases/{body['case_id']}/audit", headers=OFFICER).json()
    kinds = [r["event_type"] for r in rows]
    assert kinds[0] == "case_created" and "decision_received" in kinds and "execute_completed" in kinds
    assert all(len(r["row_hash"]) == 64 for r in rows)
    v = client.get("/audit/verify", headers=OFFICER).json()
    assert v["ok"] is True and v["rows_checked"] >= len(rows)


def test_security_headers_and_no_store(client):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_unexpected_errors_are_a_generic_500_without_details(client, env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret detail 1990-01-01")

    monkeypatch.setattr(env.service, "list_cases", boom)
    with TestClient(client.app, raise_server_exceptions=False) as c:
        r = c.get("/cases", headers=OFFICER)
    assert r.status_code == 500 and r.json() == {"detail": "internal error"}


def test_a_failed_run_stores_only_the_error_class_and_logs_no_personal_data(env, capsys):
    case = CASES["clean_approve"]

    def explode(*a, **k):
        raise RuntimeError(f"extraction exploded for {case.applicant.dob} SPEC-ID-0001 {case.applicant.name}")

    env.deps.kyc.extract = explode  # type: ignore[method-assign]
    with TestClient(create_app(Settings(environment="test"), service=env.service)) as c:
        r = c.post(
            "/cases",
            headers=SUBMITTER,
            data={"applicant": case.applicant.model_dump_json()},
            files=files_for("clean_approve"),
        )
    assert r.status_code == 201 and r.json()["last_error"] == "RuntimeError"
    out = capsys.readouterr().out
    assert "run failed" in out and r.json()["case_id"] in out
    assert case.applicant.dob not in out and "SPEC-ID" not in out
    rows = [x for x in env.audit.rows() if x.event_type == "run_failed"]
    assert rows and rows[0].payload == {"error": "RuntimeError"}


def test_llm_retry_attempts_are_configurable_and_validated():
    assert Settings.from_env({"LLM_RETRY_ATTEMPTS": "2"}).llm_retry_attempts == 2
    assert Settings.from_env({}).llm_retry_attempts == 5
    with pytest.raises(ConfigError, match="LLM_RETRY_ATTEMPTS"):
        Settings.from_env({"LLM_RETRY_ATTEMPTS": "0"})
