from __future__ import annotations

import httpx
import pytest

from onboarding.tools.kyc import KycClient, KycUnavailable

OK_BODY = {
    "id": "doc-1",
    "document_type": "id_document",
    "status": "needs_review",
    "needs_review": True,
    "model_id": "m",
    "created_at": "2026-01-01T00:00:00Z",
    "fields": [
        {
            "name": "full_name",
            "value": "A B",
            "confidence": 0.97,
            "needs_review": False,
            "reason": None,
            "reviewed": False,
        },
        {
            "name": "id_number",
            "value": "SPEC-ID-0001",
            "confidence": 0.5,
            "needs_review": True,
            "reason": "low_confidence",
            "reviewed": False,
        },
    ],
}


def client_for(handler, **kw) -> tuple[KycClient, list[float]]:
    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://kyc")
    return KycClient(client=http, sleep=sleeps.append, **kw), sleeps


def test_success_parses_fields_and_flags():
    kyc, sleeps = client_for(lambda r: httpx.Response(201, json=OK_BODY))
    res = kyc.extract(b"x", "id_document")
    assert res.document_id == "doc-1" and res.attempts == 1 and sleeps == []
    assert [f.name for f in res.fields if f.needs_review] == ["id_number"]
    assert all(f.document == "id_document" for f in res.fields)


def test_sends_the_api_key_and_multipart_fields():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["key"] = req.headers.get("x-api-key")
        seen["body"] = req.read()
        return httpx.Response(201, json=OK_BODY)

    kyc, _ = client_for(handler, api_key="k1")
    kyc.extract(b"payload-bytes", "id_document")
    assert (
        seen["key"] == "k1" and b"payload-bytes" in seen["body"] and b'name="document_type"' in seen["body"]
    )


def test_server_errors_are_retried_with_backoff_then_succeed():
    calls = iter([502, 503, 201])
    kyc, sleeps = client_for(lambda r: httpx.Response(next(calls), json=OK_BODY))
    res = kyc.extract(b"x", "id_document")
    assert res.attempts == 3 and sleeps == [0.5, 1.0]


def test_persistent_server_errors_raise_unavailable_after_the_attempt_budget():
    count = {"n": 0}

    def handler(r):
        count["n"] += 1
        return httpx.Response(502, json={"detail": "extraction failed"})

    kyc, sleeps = client_for(handler, max_attempts=3)
    with pytest.raises(KycUnavailable) as e:
        kyc.extract(b"x", "id_document")
    assert count["n"] == 3 and e.value.reason == "HTTP 502" and e.value.attempts == 3 and len(sleeps) == 2


def test_client_errors_are_not_retried():
    count = {"n": 0}

    def handler(r):
        count["n"] += 1
        return httpx.Response(415, json={"detail": "unsupported"})

    kyc, _ = client_for(handler)
    with pytest.raises(KycUnavailable, match="HTTP 415"):
        kyc.extract(b"x", "id_document")
    assert count["n"] == 1


def test_rate_limit_is_retried():
    calls = iter([429, 201])
    kyc, _ = client_for(lambda r: httpx.Response(next(calls), json=OK_BODY))
    assert kyc.extract(b"x", "id_document").attempts == 2


def test_timeouts_and_connection_errors_are_retried_then_degrade():
    def handler(r):
        raise httpx.ConnectTimeout("boom")

    kyc, sleeps = client_for(handler, max_attempts=2)
    with pytest.raises(KycUnavailable, match="ConnectTimeout"):
        kyc.extract(b"x", "id_document")
    assert len(sleeps) == 1


def test_unexpected_response_shape_is_unavailable_not_a_crash():
    kyc, _ = client_for(lambda r: httpx.Response(201, json={"unexpected": True}))
    with pytest.raises(KycUnavailable, match="unexpected response shape"):
        kyc.extract(b"x", "id_document")
