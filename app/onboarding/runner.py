"""Run a fixture case through the graph offline, and compare the result with the fixture's expectations.

Used by the offline CLI, by tests, and (later) by the eval harness. Everything is in-process: the KYC
service is the fake from `fake_kyc` (via Starlette's TestClient), the LLM is a fake, the audit log is in
memory, and the checkpointer is LangGraph's in-memory one. Phase 2 runs each case up to the first
approval pause; the officer's scripted steps (`human_script`) are applied from Phase 3 on.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from fake_kyc.main import create_app as create_fake_kyc
from onboarding.audit import AuditEvent, AuditRow, MemoryAuditLog
from onboarding.fixtures import Case, load_cases
from onboarding.graph import names
from onboarding.graph.build import build_graph
from onboarding.graph.nodes import Deps, approval_payload
from onboarding.graph.serde import checkpoint_serde
from onboarding.llm.client import FakeLlm
from onboarding.llm.prompts import LocalPromptStore
from onboarding.llm.service import LlmService
from onboarding.models import CaseState, DocumentRef
from onboarding.rules.reference import Reference
from onboarding.screening.scorer import ScreeningConfig
from onboarding.screening.unlist import SanctionsIndex
from onboarding.tools.buffer import DocumentBuffer
from onboarding.tools.kyc import KycClient

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class RunResult:
    case_id: str
    state: CaseState
    interrupted: bool
    trajectory: list[str]
    audit: MemoryAuditLog
    llm: FakeLlm
    approval_payload: dict[str, Any] | None


def load_fixture_cases(directory: Path = ROOT / "evals" / "cases") -> dict[str, Case]:
    return load_cases(directory)


def trajectory_from_audit(rows: list[AuditRow], case_id: str) -> list[str]:
    """Node sequence as recorded in the audit log: each completed node, and `approve` when the case was
    handed to an officer. This is what the evals will compare against the expected trajectory."""
    out: list[str] = []
    for r in rows:
        if r.case_id != case_id:
            continue
        if r.event_type == names.NODE_COMPLETED and r.node:
            out.append(r.node)
        elif r.event_type == names.APPROVAL_REQUESTED:
            out.append(names.APPROVE)
    return out


def build_offline_deps(case: Case, audit: MemoryAuditLog, llm: FakeLlm) -> Deps:
    fake_kyc = TestClient(create_fake_kyc(ROOT / "evals" / "cases"))
    return Deps(
        audit=audit,
        kyc=KycClient(
            client=fake_kyc,  # type: ignore[arg-type]  # TestClient is an httpx.Client
            sleep=lambda _s: None,
        ),
        index=SanctionsIndex.load(ROOT / "data" / "sanctions"),
        screening_cfg=ScreeningConfig.load(),
        reference=Reference.load(),
        llm=LlmService(llm, LocalPromptStore(ROOT / "prompts"), audit),
        buffer=DocumentBuffer(),
    )


def run_case(case: Case, thread_suffix: str = "", llm: FakeLlm | None = None) -> RunResult:
    """Submit a fixture case and run it to the first approval pause."""
    audit = MemoryAuditLog()
    llm = llm or FakeLlm(unavailable=case.llm_mode == "unavailable")
    deps = build_offline_deps(case, audit, llm)
    case_id = case.id + thread_suffix

    refs = []
    for i, doc in enumerate(case.documents, start=1):
        content = doc.content.encode()
        ref = f"{case_id}-doc-{i}"
        deps.buffer.put(case_id, ref, content)
        refs.append(
            DocumentRef(doc_ref=ref, doc_type=doc.doc_type, sha256=hashlib.sha256(content).hexdigest())
        )
    initial = CaseState(case_id=case_id, applicant=case.applicant, documents=refs)

    audit.append(AuditEvent(case_id, "case_created", payload={"documents": len(refs)}))
    graph = build_graph(deps, InMemorySaver(serde=checkpoint_serde()))
    config = RunnableConfig(configurable={"thread_id": case_id})
    graph.invoke(initial, config)
    snapshot = graph.get_state(config)
    state = CaseState.model_validate(snapshot.values)
    interrupted = bool(snapshot.interrupts)
    payload = None
    if interrupted:
        payload = approval_payload(state)
        # Written by the runner, not the node: an interrupted node re-runs on resume and must stay pure.
        audit.append(
            AuditEvent(
                case_id,
                names.APPROVAL_REQUESTED,
                node=names.APPROVE,
                payload={
                    "recommendation": state.recommendation.action if state.recommendation else None,
                    "rating": state.risk.rating if state.risk else None,
                },
            )
        )
    return RunResult(
        case_id, state, interrupted, trajectory_from_audit(audit.rows(), case_id), audit, llm, payload
    )


def compare_first_pass(case: Case, result: RunResult) -> list[str]:
    """Differences between the run and the fixture's expectations for the first approval pass."""
    e, s = case.expected, result.state
    problems: list[str] = []
    first_pass = e.trajectory[: e.trajectory.index("approve") + 1]
    if result.trajectory != first_pass:
        problems.append(f"trajectory {result.trajectory} != expected {first_pass}")
    if not result.interrupted:
        problems.append("the graph did not pause for the officer")
    if s.recommendation is None or s.recommendation.action != e.recommendation:
        problems.append(
            f"recommendation {s.recommendation.action if s.recommendation else None} != {e.recommendation}"
        )
    if s.risk is None or s.risk.rating != e.risk_rating:
        problems.append(f"risk rating {s.risk.rating if s.risk else None} != {e.risk_rating}")
    got_hits = sorted((h.entry_id, h.classification) for h in (s.screening.hits if s.screening else []))
    want_hits = sorted((h.entry_id, h.cls) for h in e.hits)
    if got_hits != want_hits:
        problems.append(f"hits {got_hits} != {want_hits}")
    if e.fired_rules is not None:
        got_rules = sorted(r.rule_id for r in (s.risk.fired_rules if s.risk else []))
        if got_rules != sorted(e.fired_rules):
            problems.append(f"fired rules {got_rules} != {sorted(e.fired_rules)}")
    if sorted(s.degraded) != sorted(e.degraded):
        problems.append(f"degraded {sorted(s.degraded)} != {sorted(e.degraded)}")
    return problems
