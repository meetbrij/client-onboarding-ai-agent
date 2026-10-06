"""LLM client interface, a deterministic fake for tests, and the Bedrock implementation.

The LLM is advisory (CLAUDE.md hard rules). Calls are sequential, with one retry layer: boto3's own
retries are switched off so attempts do not multiply (same policy as MIA D-29). When the model is
unreachable after the retries, `LlmUnavailable` is raised and the caller falls back to a template.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class LlmUnavailable(RuntimeError):
    """The model could not be used (throttled past the retry budget, unreachable, or not permitted)."""


@dataclass(frozen=True)
class LlmResult:
    text: str
    model_id: str
    input_tokens: int
    output_tokens: int


class LlmClient(Protocol):
    def complete(self, role: str, system: str, user: str, max_tokens: int = 400) -> LlmResult: ...


Responder = Callable[[str, str], str]  # (role, user prompt) -> text


def default_responder(role: str, user: str) -> str:
    """Deterministic stand-in text. Cites exactly the rule ids that appear in the prompt, so it stays grounded."""
    rule_ids = sorted(set(re.findall(r"R-[A-Z]{3}-\d{2}", user)))
    if role == "explain":
        return "Fake explanation citing " + (", ".join(rule_ids) if rule_ids else "no rules") + "."
    if role == "summarise":
        return "Fake summary mentioning " + (", ".join(rule_ids) if rule_ids else "no rules") + "."
    if role == "draft_missing_docs":
        missing = re.search(r"MISSING DOCUMENTS\n(.*)", user, re.S)
        return (
            "Dear applicant, please send: "
            + (missing.group(1).strip() if missing else "")
            + " Reply via [bank contact]."
        )
    if role == "annotate_hit":
        return "Fake note: evidence is inconclusive."
    return "fake"


@dataclass
class FakeLlm:
    """Records every call. `unavailable=True` makes every call raise LlmUnavailable."""

    unavailable: bool = False
    responder: Responder = default_responder
    model_id: str = "fake-llm"
    calls: list[tuple[str, str, str]] = field(default_factory=list)  # (role, system, user)

    def complete(self, role: str, system: str, user: str, max_tokens: int = 400) -> LlmResult:
        self.calls.append((role, system, user))
        if self.unavailable:
            raise LlmUnavailable("fake LLM is unavailable")
        text = self.responder(role, user)
        return LlmResult(text, self.model_id, len((system + user).split()), len(text.split()))


RETRYABLE_CODES = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "ModelTimeoutException",
        "InternalServerException",
        "ModelNotReadyException",
    }
)


class BedrockLlm:
    """Amazon Bedrock Converse API. `model_id` may be a model id or a cross-region inference profile id."""

    def __init__(
        self,
        model_id: str,
        region: str,
        client: Any | None = None,
        max_attempts: int = 5,
        base_wait_s: float = 1.0,
        max_wait_s: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=Config(
                    retries={"max_attempts": 1, "mode": "standard"}, read_timeout=30, connect_timeout=5
                ),
            )
        self.client = client
        self.model_id = model_id
        self.max_attempts = max_attempts
        self.base_wait_s = base_wait_s
        self.max_wait_s = max_wait_s
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security

    def _wait(self, attempt: int) -> float:
        return self._rng.uniform(0, min(self.max_wait_s, self.base_wait_s * (2 ** (attempt - 1))))

    def complete(self, role: str, system: str, user: str, max_tokens: int = 400) -> LlmResult:
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self.client.converse(
                    modelId=self.model_id,
                    system=[{"text": system}],
                    messages=[{"role": "user", "content": [{"text": user}]}],
                    inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
                )
                text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
                usage = resp.get("usage", {})
                return LlmResult(
                    text.strip(), self.model_id, usage.get("inputTokens", 0), usage.get("outputTokens", 0)
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                code = (
                    getattr(exc, "response", {}).get("Error", {}).get("Code")
                    if hasattr(exc, "response")
                    else None
                )
                retryable = code in RETRYABLE_CODES or isinstance(exc, TimeoutError | ConnectionError)
                if not retryable:
                    raise LlmUnavailable(f"Bedrock call failed: {code or type(exc).__name__}") from exc
                last = exc
                if attempt < self.max_attempts:
                    self._sleep(self._wait(attempt))
        raise LlmUnavailable(f"Bedrock still failing after {self.max_attempts} attempts") from last
