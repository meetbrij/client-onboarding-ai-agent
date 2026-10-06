"""Langfuse: prompt management and tracing (DECISIONS D-07, D-17).

Prompts are fetched by label (`production` or `staging`) and cached by the SDK; if Langfuse is unreachable the
checked-in copy in `prompts/` is used and the version is recorded as `local-fallback`. Every LLM call records the
prompt name and version in the audit log and on its generation here.

Tracing never fails a case: errors inside the Langfuse SDK are logged and swallowed, and exceptions from the
traced code pass through untouched. LangGraph's pause (GraphInterrupt) is not an error and is not marked as one.
Spans carry ids, counts, rule ids and prompt names only, never applicant details (see onboarding/observability.py).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langgraph.errors import GraphInterrupt

from onboarding.config import Settings
from onboarding.llm.prompts import Prompt, PromptStore
from onboarding.observability import trace_id_for

log = logging.getLogger("onboarding.langfuse")


def make_client(s: Settings) -> Any:
    from langfuse import Langfuse

    return Langfuse(
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key,
        host=s.langfuse_host,
        environment=s.environment,
    )


class LangfusePromptStore:
    def __init__(
        self,
        client: Any,
        label: str,
        fallback: PromptStore,
        cache_ttl_seconds: int = 60,
        registry: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.label = label
        self.fallback = fallback
        self.cache_ttl_seconds = cache_ttl_seconds
        self.registry = (
            registry if registry is not None else {}
        )  # prompt name -> Langfuse prompt client (for linking)

    def get(self, name: str) -> Prompt:
        try:
            p = self.client.get_prompt(
                name,
                label=self.label,
                type="text",
                cache_ttl_seconds=self.cache_ttl_seconds,
                fetch_timeout_seconds=3,
                max_retries=1,
            )
            self.registry[name] = p
            return Prompt(name=name, version=str(p.version), template=p.prompt)
        except Exception as exc:  # noqa: BLE001 - Langfuse is a vendor; the case must not depend on it
            log.warning(
                "prompt %s not available from Langfuse (%s); using the local copy", name, type(exc).__name__
            )
            self.registry.pop(name, None)
            return self.fallback.get(name)


class _Handle:
    def __init__(self, observation: Any, kind: str) -> None:
        self._o, self._kind = observation, kind

    def update(self, **attributes: Any) -> None:
        try:
            if self._kind == "generation":
                usage = {k.removesuffix("_tokens"): v for k, v in attributes.items() if k.endswith("_tokens")}
                meta = {k: v for k, v in attributes.items() if not k.endswith("_tokens") and k != "model"}
                self._o.update(
                    model=attributes.get("model"), usage_details=usage or None, metadata=meta or None
                )
            else:
                self._o.update(metadata=attributes)
        except Exception:  # noqa: BLE001
            log.warning("could not update a Langfuse observation")


class _NoHandle:
    def update(self, **attributes: Any) -> None:
        return None


class LangfuseTracer:
    def __init__(self, client: Any, environment: str, prompt_clients: dict[str, Any] | None = None) -> None:
        self.client = client
        self.environment = environment
        self.prompt_clients = prompt_clients if prompt_clients is not None else {}

    @contextmanager
    def _observe(self, kind: str, **kwargs: Any) -> Iterator[Any]:
        try:
            cm = self.client.start_as_current_observation(**kwargs)
            observation = cm.__enter__()
        except Exception:  # noqa: BLE001
            log.warning("Langfuse tracing unavailable; continuing untraced")
            yield _NoHandle()
            return
        exc: BaseException | None = None
        try:
            yield _Handle(observation, kind)
        except GraphInterrupt:
            raise  # a pause, not a failure
        except BaseException as e:
            exc = e
            raise
        finally:
            try:
                cm.__exit__(type(exc) if exc else None, exc, exc.__traceback__ if exc else None)
            except Exception:  # noqa: BLE001
                log.warning("could not close a Langfuse observation")

    @contextmanager
    def run(self, case_id: str, segment: str) -> Iterator[Any]:
        try:
            from langfuse import propagate_attributes

            attrs = propagate_attributes(
                trace_name="onboarding-case",
                session_id=case_id,
                tags=[f"env:{self.environment}", f"segment:{segment}"],
                metadata={"case_id": case_id},
            )
            attrs.__enter__()
        except Exception:  # noqa: BLE001
            attrs = None
        try:
            with self._observe(
                "span",
                trace_context={"trace_id": trace_id_for(case_id)},
                name=f"case:{segment}",
                as_type="span",
                metadata={"case_id": case_id},
            ) as handle:
                yield handle
        finally:
            if attrs is not None:
                try:
                    attrs.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    log.warning("could not close Langfuse trace attributes")
            try:
                self.client.flush()
            except Exception:  # noqa: BLE001
                log.warning("could not flush Langfuse")

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]:
        with self._observe("span", name=name, as_type="span", metadata=attributes or None) as handle:
            yield handle

    @contextmanager
    def generation(
        self, name: str, model: str, prompt_name: str, prompt_version: str, **attributes: Any
    ) -> Iterator[Any]:
        kwargs: dict[str, Any] = {
            "name": name,
            "as_type": "generation",
            "model": model,
            "metadata": {"prompt_name": prompt_name, "prompt_version": prompt_version, **attributes},
        }
        linked = self.prompt_clients.get(prompt_name)
        if linked is not None:
            kwargs["prompt"] = linked  # links the generation to the exact prompt version in Langfuse
        with self._observe("generation", **kwargs) as handle:
            yield handle
