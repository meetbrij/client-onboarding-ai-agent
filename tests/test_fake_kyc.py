from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fake_kyc.main import create_app
from onboarding.fixtures import load_cases

CASES = load_cases(Path("evals/cases"))


@pytest.fixture
def client():
    return TestClient(create_app(Path("evals/cases")))


def upload(client, case_id: str, doc_type: str, headers=None):
    case = CASES[case_id]
    doc = next(d for d in [*case.documents, *case.followup_documents] if d.doc_type == doc_type)
    return client.post(
        "/documents",
        files={"file": ("specimen.txt", doc.content.encode(), "text/plain")},
        data={"document_type": doc_type},
        headers=headers or {},
    )


def test_returns_recorded_response_in_p3_shape(client):
    r = upload(client, "low_confidence_extraction", "id_document")
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"id", "document_type", "status", "needs_review", "model_id", "created_at", "fields"}
    assert body["status"] == "needs_review" and body["needs_review"] is True
    flagged = {f["name"] for f in body["fields"] if f["needs_review"]}
    assert flagged == {"date_of_birth", "id_number"}
    assert set(body["fields"][0]) == {"name", "value", "confidence", "needs_review", "reason", "reviewed"}


def test_unavailable_case_returns_502(client):
    r = upload(client, "kyc_unavailable", "id_document")
    assert r.status_code == 502


def test_non_fixture_file_is_rejected(client):
    r = client.post(
        "/documents", files={"file": ("x.txt", b"hello", "text/plain")}, data={"document_type": "id_document"}
    )
    assert r.status_code == 415


def test_followup_document_type_is_served(client):
    r = client.post(
        "/documents",
        files={"file": ("x.txt", b"case=missing_poa\n", "text/plain")},
        data={"document_type": "proof_of_address"},
    )
    assert r.status_code == 201  # followup response exists for the second round


def test_api_key_enforced_when_configured():
    c = TestClient(create_app(Path("evals/cases"), api_key="local-test-key"))
    assert upload(c, "clean_approve", "id_document").status_code == 401
    assert upload(c, "clean_approve", "id_document", {"X-API-Key": "local-test-key"}).status_code == 201


def test_unknown_document_type_for_case_is_422(client):
    r = client.post(
        "/documents",
        files={"file": ("x.txt", b"case=kyc_unavailable_x\n", "text/plain")},
        data={"document_type": "id_document"},
    )
    assert r.status_code == 415
    r = client.post(
        "/documents",
        files={"file": ("x.txt", b"case=clean_approve\n", "text/plain")},
        data={"document_type": "passport"},
    )
    assert r.status_code == 422
