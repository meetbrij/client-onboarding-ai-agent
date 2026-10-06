"""Run a fixture case through the real CaseService offline, and compare the result with the fixture's expectations.

Everything is in-process: the KYC service is the fake from `fake_kyc`, the core bank is the mock service on an
in-memory database, the LLM is a fake, the audit log is in memory, and LangGraph uses its in-memory
checkpointer. The officer's and the client's later steps come from the fixture's `human_script` and
`followup_documents`. Used by the offline CLI, by tests, and (later) by the eval harness.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from fake_kyc.main import create_app as create_fake_kyc
from mock_bank.main import create_app as create_mock_bank
from mock_bank.main import metadata as bank_metadata
from onboarding.audit import AuditRow, AuditSink, MemoryAuditLog
from onboarding.decision import DecisionRequest
from onboarding.fixtures import Case, load_cases
from onboarding.graph import names
from onboarding.graph.nodes import Deps
from onboarding.graph.serde import checkpoint_serde
from onboarding.llm.client import FakeLlm
from onboarding.llm.prompts import LocalPromptStore
from onboarding.llm.service import LlmService
from onboarding.locks import LocalLocks
from onboarding.models import CaseState
from onboarding.rules.reference import Reference
from onboarding.screening.scorer import ScreeningConfig
from onboarding.screening.unlist import SanctionsIndex
from onboarding.service import CaseService, CaseView, UploadedDoc
from onboarding.store import CaseStore
from onboarding.tools.bank import BankClient
from onboarding.tools.buffer import DocumentBuffer
from onboarding.tools.kyc import KycClient

ROOT = Path(__file__).resolve().parents[2]
SUBMITTER = "submitter-eval"
OFFICER = "officer-eval"


@dataclass
class OfflineEnv:
    service: CaseService
    audit: MemoryAuditLog
    llm: FakeLlm
    deps: Deps
    stack: ExitStack

    def close(self) -> None:
        self.stack.close()


def build_deps(audit: AuditSink, llm: FakeLlm, stack: ExitStack, max_info_rounds: int = 2) -> Deps:
    """Dependencies with the fake KYC service, the mock bank on an in-memory database, and a fake LLM."""
    kyc_http = stack.enter_context(TestClient(create_fake_kyc(ROOT / "evals" / "cases")))
    bank_engine = create_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    bank_metadata.create_all(bank_engine)
    bank_http = stack.enter_context(TestClient(create_mock_bank(bank_engine)))
    return Deps(
        audit=audit,
        kyc=KycClient(client=kyc_http, sleep=lambda _s: None),  # type: ignore[arg-type]  # TestClient is an httpx.Client
        index=SanctionsIndex.load(ROOT / "data" / "sanctions"),
        screening_cfg=ScreeningConfig.load(),
        reference=Reference.load(),
        llm=LlmService(llm, LocalPromptStore(ROOT / "prompts"), audit),
        buffer=DocumentBuffer(),
        bank=BankClient(client=bank_http, sleep=lambda _s: None),  # type: ignore[arg-type]
        max_info_rounds=max_info_rounds,
    )


def build_offline_env(
    llm: FakeLlm | None = None, max_info_rounds: int = 2, audit: MemoryAuditLog | None = None
) -> OfflineEnv:
    stack = ExitStack()
    audit = audit or MemoryAuditLog()
    llm = llm or FakeLlm()
    deps = build_deps(audit, llm, stack, max_info_rounds)
    engine = create_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = CaseStore(engine)
    store.create_schema()
    service = CaseService(deps, store, InMemorySaver(serde=checkpoint_serde()), LocalLocks(), max_info_rounds)
    return OfflineEnv(service, audit, llm, deps, stack)


@dataclass
class RunResult:
    case_id: str
    view: CaseView
    first_pass: CaseState | None  # the state at the first approval pause
    trajectory: list[str]
    audit: MemoryAuditLog
    llm: FakeLlm
    env: OfflineEnv

    @property
    def state(self) -> CaseState:
        assert self.view.state is not None
        return self.view.state

    @property
    def interrupted(self) -> bool:
        return self.view.pending is not None

    @property
    def approval_payload(self) -> dict[str, Any] | None:
        return self.view.pending


def load_fixture_cases(directory: Path = ROOT / "evals" / "cases") -> dict[str, Case]:
    return load_cases(directory)


def trajectory_from_audit(rows: list[AuditRow], case_id: str) -> list[str]:
    """Node sequence as recorded in the audit log: each completed node, plus `approve` and `await_docs` when
    the case paused for the officer or for documents. This is what the evals compare with the expected trajectory."""
    out: list[str] = []
    for r in rows:
        if r.case_id != case_id:
            continue
        if r.event_type == names.NODE_COMPLETED and r.node:
            out.append(r.node)
        elif r.event_type == names.APPROVAL_REQUESTED:
            out.append(names.APPROVE)
        elif r.event_type == names.DOCUMENTS_REQUESTED:
            out.append(names.AWAIT_DOCS)
    return out


def _docs(items: list[Any]) -> list[UploadedDoc]:
    return [
        UploadedDoc(doc_type=d.doc_type, content=d.content.encode(), filename="specimen.txt") for d in items
    ]


def run_case(case: Case, llm: FakeLlm | None = None, full: bool = False) -> RunResult:
    """Submit a fixture case. With `full=True`, also play the fixture's human script to the end."""
    env = build_offline_env(llm or FakeLlm(unavailable=case.llm_mode == "unavailable"))
    svc = env.service
    view = svc.create_case(case.applicant, _docs(case.documents), SUBMITTER, case_id=case.id)
    first_pass = view.state.model_copy(deep=True) if view.state else None
    if full:
        followups = list(case.followup_documents)
        for step in case.human_script:
            if view.pending is None:
                break
            iid = view.pending["interrupt_id"]
            if view.pending["kind"] == "await_docs":
                view = svc.add_documents(case.id, iid, _docs(followups), SUBMITTER)
                followups = []
                iid = view.pending["interrupt_id"] if view.pending else ""
            if view.pending is None:
                break
            view = svc.decide(
                case.id,
                DecisionRequest(
                    interrupt_id=view.pending["interrupt_id"],
                    action=step.action,
                    note=step.note,
                    dispositions=dict(step.dispositions),
                ),
                OFFICER,
            )
        # a request for more information leaves the case waiting for documents; deliver them if they are due
        while view.pending is not None and view.pending["kind"] == "await_docs" and followups:
            view = svc.add_documents(case.id, view.pending["interrupt_id"], _docs(followups), SUBMITTER)
            followups = []
    return RunResult(
        case.id, view, first_pass, trajectory_from_audit(env.audit.rows(), case.id), env.audit, env.llm, env
    )


def compare_first_pass(case: Case, result: RunResult) -> list[str]:
    """Differences between the first approval pause and the fixture's expectations."""
    e = case.expected
    s = result.first_pass
    problems: list[str] = []
    if s is None:
        return ["the case produced no state"]
    first = e.trajectory[: e.trajectory.index("approve") + 1]
    if result.trajectory[: len(first)] != first:
        problems.append(f"first-pass trajectory {result.trajectory[: len(first)]} != expected {first}")
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


def compare_full(case: Case, result: RunResult) -> list[str]:
    """First-pass checks plus the end of the story: full trajectory, final status, and the bank record."""
    problems = compare_first_pass(case, result)
    e, row = case.expected, result.view.row
    if result.trajectory != e.trajectory:
        problems.append(f"trajectory {result.trajectory} != expected {e.trajectory}")
    final = {"approved": "approved", "rejected": "rejected", "awaiting_documents": "awaiting_documents"}[
        e.final_status
    ]
    if row.status != final:
        problems.append(f"final status {row.status} != {final}")
    executed = result.state.execution is not None
    if executed != (e.final_status == "approved"):
        problems.append(f"customer created: {executed}, expected {e.final_status == 'approved'}")
    return problems
