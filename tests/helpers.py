from __future__ import annotations

from pathlib import Path

from onboarding.audit import MemoryAuditLog
from onboarding.fixtures import Case, load_cases
from onboarding.graph.nodes import Deps, make_nodes
from onboarding.llm.client import FakeLlm
from onboarding.models import Applicant, CaseState, DocumentRef
from onboarding.runner import build_offline_env

CASES = load_cases(Path("evals/cases"))


def offline(
    case_id: str = "clean_approve", llm: FakeLlm | None = None
) -> tuple[Case, Deps, MemoryAuditLog, FakeLlm]:
    case = CASES[case_id]
    env = build_offline_env(llm or FakeLlm(unavailable=case.llm_mode == "unavailable"))
    return case, env.deps, env.audit, env.llm


def initial_state(case: Case, deps: Deps) -> CaseState:
    refs = []
    for i, d in enumerate(case.documents, start=1):
        ref = f"{case.id}-doc-{i}"
        deps.buffer.put(case.id, ref, d.content.encode())
        refs.append(DocumentRef(doc_ref=ref, doc_type=d.doc_type, sha256="0" * 64))
    return CaseState(case_id=case.id, applicant=case.applicant, documents=refs)


def apply(state: CaseState, update: dict) -> CaseState:
    return state.model_copy(update=update)


def run_nodes(deps: Deps, state: CaseState, *node_names: str) -> CaseState:
    nodes = make_nodes(deps)
    for name in node_names:
        state = apply(state, nodes[name](state))
    return state


def applicant(**kw) -> Applicant:
    base = dict(
        name="Test Person",
        dob="1990-01-01",
        nationality="Utopia",
        residence_country="Utopia",
        occupation="Tester",
    )
    return Applicant(**{**base, **kw})
