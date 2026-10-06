"""HTTP tool: the core-banking API (the mock in this project). One call, idempotent on `Idempotency-Key`.

The key is the case id, so retrying after a timeout, a crash or a duplicate click can never create a second
customer: the bank returns the original record with `Idempotent-Replayed: true`. Server errors and timeouts
are retried; a 409 (same key, different payload) and other client errors are not.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx


class BankUnavailable(RuntimeError):
    def __init__(self, reason: str, attempts: int = 1) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


class BankRejected(RuntimeError):
    """The bank refused the request and retrying will not help (4xx, including 409)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"bank rejected the request: HTTP {status} {detail}")
        self.status = status


@dataclass(frozen=True)
class BankResult:
    customer_id: str
    replayed: bool
    attempts: int


class BankClient:
    def __init__(
        self,
        base_url: str = "",
        timeout_s: float = 15.0,
        max_attempts: int = 4,
        base_wait_s: float = 0.5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout_s)
        self._max_attempts = max_attempts
        self._base_wait_s = base_wait_s
        self._sleep = sleep

    def create_customer(self, idempotency_key: str, payload: dict[str, Any]) -> BankResult:
        last = "unknown"
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._client.post(
                    "/customers", json=payload, headers={"Idempotency-Key": idempotency_key}
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = type(exc).__name__
            else:
                if resp.status_code in (200, 201):
                    return BankResult(
                        customer_id=str(resp.json()["customer_id"]),
                        replayed=resp.headers.get("Idempotent-Replayed", "").lower() == "true",
                        attempts=attempt,
                    )
                if resp.status_code < 500 and resp.status_code != 429:
                    detail = (
                        resp.json().get("detail", "")
                        if resp.headers.get("content-type", "").startswith("application/json")
                        else ""
                    )
                    raise BankRejected(resp.status_code, str(detail))
                last = f"HTTP {resp.status_code}"
            if attempt < self._max_attempts:
                self._sleep(self._base_wait_s * 2 ** (attempt - 1))
        raise BankUnavailable(last, self._max_attempts)
