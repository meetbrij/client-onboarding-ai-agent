"""Tracing and prompt management, against the real Langfuse SDK with an in-memory span exporter (no network),
and against stubs for the failure paths."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from langfuse import Langfuse
from langgraph.errors import GraphInterrupt
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from onboarding.audit import MemoryAuditLog
from onboarding.langfuse_tracing import LangfusePromptStore, LangfuseTracer
from onboarding.llm.client import FakeLlm
from onboarding.llm.prompts import LOCAL_VERSION, LocalPromptStore
from onboarding.llm.service import LlmService
from onboarding.observability import NoopTracer, trace_id_for
from onboarding.runner import SUBMITTER, build_deps
from onboarding.service import CaseService, UploadedDoc
from onboarding.store import CaseStore
from tests.helpers import CASES


@pytest.fixture(scope="module")
def _langfuse():
    """One real Langfuse client for the module, exporting spans to memory instead of the network. (The SDK keeps
    process-wide state, so creating and shutting down several clients in one process can hang.)"""
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-{uuid.uuid4().hex}",
        secret_key="sk-lf-test",
        host="http://127.0.0.1:9",
        span_exporter=exporter,
        tracing_enabled=True,
    )
    yield client, exporter
    client.flush()


@pytest.fixture
def traced(_langfuse):
    client, exporter = _langfuse
    client.flush()
    exporter.clear()
    return LangfuseTracer(client, "test"), client, exporter


def spans_of(client, exporter):
    client.flush()
    return exporter.get_finished_spans()


def attrs(span) -> dict:
    return {k: v for k, v in (span.attributes or {}).items()}


def test_trace_id_is_stable_and_valid_for_any_case_id():
    assert trace_id_for(
        "0b5f4a9e-1111-4222-8333-444455556666"
    ) == "0b5f4a9e1111422283334444555 56666".replace(" ", "")
    for cid in ("clean_approve", "x", "a" * 80):
        t = trace_id_for(cid)
        assert len(t) == 32 and all(c in "0123456789abcdef" for c in t) and t == trace_id_for(cid)
    assert trace_id_for("a") != trace_id_for("b")


def test_a_case_produces_one_trace_with_node_tool_and_generation_spans(traced):
    from contextlib import ExitStack

    from langgraph.checkpoint.memory import InMemorySaver
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from onboarding.graph.serde import checkpoint_serde
    from onboarding.locks import LocalLocks

    tracer, client, exporter = traced
    stack = ExitStack()
    audit = MemoryAuditLog()
    deps = build_deps(audit, FakeLlm(), stack)
    deps.tracer = tracer
    deps.llm = LlmService(FakeLlm(), LocalPromptStore(), audit, tracer=tracer)
    engine = create_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = CaseStore(engine)
    store.create_schema()
    svc = CaseService(deps, store, InMemorySaver(serde=checkpoint_serde()), LocalLocks())
    case = CASES["missing_poa"]
    svc.create_case(
        case.applicant,
        [UploadedDoc(d.doc_type, d.content.encode()) for d in case.documents],
        SUBMITTER,
        case_id="missing_poa",
    )
    spans = spans_of(client, exporter)
    stack.close()

    by_name = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s)
    for expected in (
        "case:start",
        "intake",
        "extract",
        "kyc.extract",
        "screen",
        "assess",
        "explain",
        "summarise",
        "draft_missing_docs",
    ):
        assert expected in by_name, (expected, sorted(by_name))
    assert {s.context.trace_id for s in spans} == {
        int(trace_id_for("missing_poa"), 16)
    }  # one trace, derived from the case id
    root = by_name["case:start"][0]
    # Langfuse gives the root a synthetic remote parent so it can sit in the case's trace; it is none of our spans
    assert root.parent is None or root.parent.span_id not in {s.context.span_id for s in spans}
    node_ids = {s.context.span_id for n in ("intake", "extract", "screen", "assess") for s in by_name[n]}
    assert all(
        s.parent.span_id == root.context.span_id
        for n in ("intake", "extract", "screen", "assess")
        for s in by_name[n]
    )
    assert by_name["kyc.extract"][0].parent.span_id in node_ids  # tool span nested in its node
    gen = by_name["explain"][0]
    assert gen.parent.span_id in node_ids
    meta = json.dumps(attrs(gen), default=str)
    assert "onboarding-explain-recommendation" in meta and LOCAL_VERSION in meta and "fake-llm" in meta
    assert attrs(gen).get("langfuse.observation.type") == "generation"


def test_spans_carry_no_personal_data(traced):
    tracer, client, exporter = traced
    case = CASES["near_miss_dob_mismatch"]
    from onboarding.runner import build_offline_env

    env = build_offline_env()
    env.deps.tracer = tracer
    env.deps.llm.tracer = tracer
    env.service.create_case(
        case.applicant,
        [UploadedDoc(d.doc_type, d.content.encode()) for d in case.documents],
        SUBMITTER,
        case_id="near",
    )
    text = json.dumps([attrs(s) for s in spans_of(client, exporter)], default=str)
    env.close()
    assert case.applicant.dob not in text and "SPEC-ID" not in text and "SPECIMEN Street" not in text
    assert "SYNTHETIC TEST DOCUMENT" not in text


def test_a_graph_pause_is_not_an_error_but_a_failure_is(traced):
    tracer, client, exporter = traced
    with pytest.raises(GraphInterrupt):
        with tracer.span("pausing"):
            raise GraphInterrupt()
    with pytest.raises(ValueError):
        with tracer.span("failing"):
            raise ValueError("boom")
    spans = {s.name: s for s in spans_of(client, exporter)}
    assert spans["pausing"].status.status_code != StatusCode.ERROR
    assert spans["failing"].status.status_code == StatusCode.ERROR


def test_tracing_failures_never_break_the_traced_code():
    class Broken:
        def start_as_current_observation(self, **kw):
            raise RuntimeError("langfuse is down")

        def flush(self):
            raise RuntimeError("langfuse is down")

    tracer = LangfuseTracer(Broken(), "test")
    ran = []
    with tracer.run("case-1", "start"):
        with tracer.span("node"):
            with tracer.generation("explain", "m", "p", "1") as gen:
                gen.update(model="m", input_tokens=1)
                ran.append(True)
    assert ran == [True]


def test_the_noop_tracer_is_a_valid_tracer():
    t = NoopTracer()
    with t.run("c", "start") as r, t.span("n") as s, t.generation("g", "m", "p", "1") as g:
        r.update(a=1), s.update(b=2), g.update(c=3)


# ------------------------------------------------------------------ prompt management
class StubLangfuse:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def get_prompt(self, name, **kwargs):
        self.calls.append((name, kwargs))
        out = self.behaviour(name)
        if isinstance(out, Exception):
            raise out
        return out


def test_prompts_are_fetched_by_label_with_a_cache_ttl_and_versioned():
    stub = StubLangfuse(lambda n: SimpleNamespace(version=7, prompt="HELLO {{facts}}"))
    reg: dict = {}
    store = LangfusePromptStore(stub, "staging", LocalPromptStore(), cache_ttl_seconds=45, registry=reg)
    p = store.get("onboarding-summarise-case")
    assert (p.name, p.version, p.template) == ("onboarding-summarise-case", "7", "HELLO {{facts}}")
    name, kwargs = stub.calls[0]
    assert kwargs["label"] == "staging" and kwargs["cache_ttl_seconds"] == 45 and kwargs["type"] == "text"
    assert reg["onboarding-summarise-case"].version == 7  # kept so the generation can link to this version


def test_when_langfuse_is_unreachable_the_local_prompt_is_used_and_marked():
    stub = StubLangfuse(lambda n: ConnectionError("down"))
    reg: dict = {"onboarding-summarise-case": object()}
    store = LangfusePromptStore(stub, "production", LocalPromptStore(), registry=reg)
    p = store.get("onboarding-summarise-case")
    assert p.version == LOCAL_VERSION and "{{facts}}" in p.template
    assert "onboarding-summarise-case" not in reg  # a stale link must not be attached to a local prompt


def test_the_generation_links_to_the_prompt_client_when_there_is_one():
    seen = {}

    class Rec:
        def start_as_current_observation(self, **kw):
            seen.update(kw)
            from contextlib import nullcontext

            return nullcontext(SimpleNamespace(update=lambda **k: None))

        def flush(self):
            pass

    marker = object()
    tracer = LangfuseTracer(Rec(), "test", prompt_clients={"onboarding-explain-recommendation": marker})
    with tracer.generation("explain", "m", "onboarding-explain-recommendation", "7"):
        pass
    assert (
        seen["prompt"] is marker
        and seen["as_type"] == "generation"
        and seen["metadata"]["prompt_version"] == "7"
    )
    seen.clear()
    with tracer.generation("explain", "m", "onboarding-annotate-hit", "local-fallback"):
        pass
    assert "prompt" not in seen
