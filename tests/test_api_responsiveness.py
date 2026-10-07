"""The API must keep answering /healthz while a case is being processed.

Case processing is synchronous and can take a while (the KYC call, Bedrock retries, the graph). If an endpoint runs it on
the event loop, nothing else is served: the Kubernetes liveness probe fails and the pod is restarted mid-request, which is
what happened on the first QA deployment. Runs a real uvicorn server, because the test client cannot show a blocked loop."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from onboarding.api.main import create_app
from onboarding.config import Settings
from onboarding.runner import build_offline_env
from tests.helpers import CASES

SLOW_SECONDS = 3.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    env = build_offline_env()
    real = env.service.create_case

    def slow_create_case(*args, **kwargs):
        time.sleep(SLOW_SECONDS)  # stands in for slow KYC and LLM calls
        return real(*args, **kwargs)

    env.service.create_case = slow_create_case  # type: ignore[method-assign]
    port = free_port()
    cfg = uvicorn.Config(
        create_app(Settings(environment="test"), service=env.service),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    srv = uvicorn.Server(cfg)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)
    env.close()


def test_healthz_answers_while_a_slow_case_is_being_created(server):
    case = CASES["clean_approve"]
    files = {d.doc_type: (f"{d.doc_type}.txt", d.content.encode(), "text/plain") for d in case.documents}
    result: dict = {}

    def submit() -> None:
        result["r"] = httpx.post(
            f"{server}/cases",
            headers={"Authorization": "Bearer dev-submitter-token"},
            data={"applicant": case.applicant.model_dump_json()},
            files=files,
            timeout=30,
        )

    t = threading.Thread(target=submit)
    t.start()
    time.sleep(0.5)  # the slow request is now in flight
    started = time.monotonic()
    health = httpx.get(f"{server}/healthz", timeout=10)
    answered_in = time.monotonic() - started
    t.join(timeout=30)
    assert health.status_code == 200
    assert answered_in < 1.0, f"/healthz took {answered_in:.1f}s: the event loop was blocked by the request"
    assert result["r"].status_code == 201


def test_async_endpoints_never_call_the_long_running_service_directly():
    """Static guard: in an `async def`, create_case, decide and add_documents must go through run_in_threadpool."""
    import ast
    from pathlib import Path

    blocking = {"create_case", "decide", "add_documents"}
    for path in (Path("app/onboarding/api/main.py"), Path("app/onboarding/api/ui.py")):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            threaded_args = {
                id(arg)
                for call in ast.walk(fn)
                if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "run_in_threadpool"
                for arg in call.args
            }
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in blocking
                ):
                    owner = node.func.value
                    if isinstance(owner, ast.Call) and getattr(owner.func, "id", "") in {"svc", "service"}:
                        raise AssertionError(
                            f"{path.name}:{node.lineno} {fn.name} calls {node.func.attr} on the event loop"
                        )
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in blocking
                    and id(node) not in threaded_args
                ):
                    owner = node.value
                    if isinstance(owner, ast.Call) and getattr(owner.func, "id", "") in {"svc", "service"}:
                        # a bare reference is only allowed as an argument to run_in_threadpool
                        parents = [
                            c
                            for c in ast.walk(fn)
                            if isinstance(c, ast.Call)
                            and getattr(c.func, "id", "") == "run_in_threadpool"
                            and any(a is node for a in c.args)
                        ]
                        assert parents, (
                            f"{path.name}:{node.lineno} {fn.name} uses {node.attr} outside run_in_threadpool"
                        )
