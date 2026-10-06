from __future__ import annotations

import httpx
import pytest

from onboarding.tools.bank import BankClient, BankRejected, BankUnavailable

PAYLOAD = {"case_id": "c1"}


def client_for(handler, **kw):
    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://bank")
    return BankClient(client=http, sleep=sleeps.append, **kw), sleeps


def test_sends_the_idempotency_key_and_reads_the_customer():
    seen = {}

    def handler(req):
        seen["key"] = req.headers["idempotency-key"]
        return httpx.Response(201, json={"customer_id": "CUST-1"})

    bank, _ = client_for(handler)
    r = bank.create_customer("case-9", PAYLOAD)
    assert seen["key"] == "case-9" and (r.customer_id, r.replayed, r.attempts) == ("CUST-1", False, 1)


def test_a_replayed_response_is_reported():
    bank, _ = client_for(
        lambda r: httpx.Response(200, json={"customer_id": "CUST-1"}, headers={"Idempotent-Replayed": "true"})
    )
    assert bank.create_customer("k", PAYLOAD).replayed is True


def test_server_errors_and_timeouts_are_retried_with_the_same_key():
    keys, calls = [], iter([503, "timeout", 201])

    def handler(req):
        keys.append(req.headers["idempotency-key"])
        c = next(calls)
        if c == "timeout":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(c, json={"customer_id": "CUST-2"})

    bank, sleeps = client_for(handler)
    r = bank.create_customer("k", PAYLOAD)
    assert r.attempts == 3 and keys == ["k", "k", "k"] and sleeps == [0.5, 1.0]


def test_gives_up_after_the_attempt_budget():
    bank, sleeps = client_for(lambda r: httpx.Response(502), max_attempts=3)
    with pytest.raises(BankUnavailable, match="HTTP 502"):
        bank.create_customer("k", PAYLOAD)
    assert len(sleeps) == 2


@pytest.mark.parametrize("status", [400, 409, 422])
def test_client_errors_are_not_retried(status):
    count = {"n": 0}

    def handler(r):
        count["n"] += 1
        return httpx.Response(status, json={"detail": "nope"})

    bank, _ = client_for(handler)
    with pytest.raises(BankRejected, match=str(status)):
        bank.create_customer("k", PAYLOAD)
    assert count["n"] == 1
