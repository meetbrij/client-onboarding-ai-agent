"""HTTP tool: P3's KYC document service (`POST /documents`, multipart: file, document_type).

Failure policy ("degrade, don't fail"): timeouts, connection errors, HTTP 429 and 5xx are retried a few
times with backoff; anything still failing, and any 4xx, raises KycUnavailable. The caller then marks the
case "extraction unavailable" and carries on to the officer. Document bytes are sent from memory and
never written anywhere; field values are returned to the caller and never logged here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from onboarding.models import DocType, ExtractedField


class KycUnavailable(RuntimeError):
    def __init__(self, reason: str, attempts: int = 1) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


@dataclass(frozen=True)
class KycResult:
    document_id: str
    status: str  # extracted | needs_review | reviewed (P3's values)
    needs_review: bool
    model_id: str
    fields: list[ExtractedField]
    attempts: int
    latency_ms: int


class KycClient:
    def __init__(
        self,
        base_url: str = "",
        api_key: str | None = None,
        timeout_s: float = 30.0,
        max_attempts: int = 3,
        base_wait_s: float = 0.5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout_s)
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._max_attempts = max_attempts
        self._base_wait_s = base_wait_s
        self._sleep = sleep

    def extract(self, content: bytes, doc_type: DocType, filename: str = "document") -> KycResult:
        started = time.monotonic()
        last_reason = "unknown"
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._client.post(
                    "/documents",
                    files={"file": (filename, content, "application/octet-stream")},
                    data={"document_type": doc_type},
                    headers=self._headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_reason = f"{type(exc).__name__}"
            else:
                if resp.status_code == 201:
                    return self._parse(
                        resp.json(), doc_type, attempt, int((time.monotonic() - started) * 1000)
                    )
                last_reason = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    raise KycUnavailable(last_reason, attempt)  # a client error will not fix itself
            if attempt < self._max_attempts:
                self._sleep(self._base_wait_s * 2 ** (attempt - 1))
        raise KycUnavailable(last_reason, self._max_attempts)

    @staticmethod
    def _parse(body: dict, doc_type: DocType, attempts: int, latency_ms: int) -> KycResult:
        try:
            fields = [
                ExtractedField(
                    document=doc_type,
                    name=f["name"],
                    value=f.get("value"),
                    confidence=float(f["confidence"]),
                    needs_review=bool(f["needs_review"]),
                    reason=f.get("reason"),
                )
                for f in body["fields"]
            ]
            return KycResult(
                str(body["id"]),
                str(body["status"]),
                bool(body["needs_review"]),
                str(body.get("model_id", "")),
                fields,
                attempts,
                latency_ms,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise KycUnavailable(f"unexpected response shape ({type(exc).__name__})", attempts) from exc
