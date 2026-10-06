from __future__ import annotations

import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from mock_bank.main import create_app

PAYLOAD = {
    "case_id": "case-0001",
    "full_name": "Layla Nasser Almazrouei",
    "date_of_birth": "1990-06-12",
    "nationality": "United Arab Emirates",
    "residence_country": "United Arab Emirates",
    "occupation": "Software engineer",
    "risk_rating": "low",
}


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with TestClient(create_app(engine)) as c:
        yield c


def post(client, payload=PAYLOAD, key="case-0001"):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/customers", json=payload, headers=headers)


def test_first_call_creates_customer(client):
    r = post(client)
    assert r.status_code == 201
    body = r.json()
    assert body["customer_id"].startswith("CUST-")
    assert body["case_id"] == "case-0001"
    assert "Idempotent-Replayed" not in r.headers


def test_same_key_same_payload_returns_same_customer(client):
    first = post(client)
    second = post(client)
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()


def test_same_key_different_payload_is_409(client):
    post(client)
    r = post(client, {**PAYLOAD, "risk_rating": "high"})
    assert r.status_code == 409


def test_different_keys_create_different_customers(client):
    a = post(client, key="case-0001")
    b = post(client, {**PAYLOAD, "case_id": "case-0002"}, key="case-0002")
    assert a.json()["customer_id"] != b.json()["customer_id"]


def test_missing_idempotency_key_is_400(client):
    assert post(client, key=None).status_code == 400


def test_invalid_payload_is_422(client):
    assert post(client, {**PAYLOAD, "risk_rating": "extreme"}).status_code == 422
    assert post(client, {**PAYLOAD, "unexpected": "x"}).status_code == 422


def test_get_customer_and_404(client):
    created = post(client).json()
    assert client.get(f"/customers/{created['customer_id']}").json() == created
    assert client.get("/customers/CUST-NOPE").status_code == 404


def test_concurrent_requests_with_one_key_create_one_customer(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path}/bank.db", connect_args={"check_same_thread": False}
    )
    with TestClient(create_app(engine)) as c:
        results: list[dict] = []

        def call() -> None:
            results.append(post(c).json())

        threads = [threading.Thread(target=call) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({r["customer_id"] for r in results}) == 1


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.skipif(
    not os.environ.get("MOCK_BANK_TEST_DATABASE_URL"),
    reason="set MOCK_BANK_TEST_DATABASE_URL to a Postgres URL (docker compose up -d postgres) to run",
)
def test_concurrent_requests_on_postgres_create_one_customer():
    engine = create_engine(os.environ["MOCK_BANK_TEST_DATABASE_URL"])
    key = f"pg-race-{uuid.uuid4()}"
    with TestClient(create_app(engine)) as c:
        results: list[dict] = []
        payload = {**PAYLOAD, "case_id": key}

        def call() -> None:
            results.append(post(c, payload, key=key).json())

        threads = [threading.Thread(target=call) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 16
        assert len({r["customer_id"] for r in results}) == 1
