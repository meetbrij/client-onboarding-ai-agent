"""Graph assembly and the offline CLI.

    START -> intake -(failed)-> END
                   \\-> extract -> screen -> assess -> approve (interrupt)
        approve -(approve)-> execute -> END
        approve -(reject)-> END
        approve -(request more info)-> await_docs (interrupt) -> intake   (the loop)

Offline CLI (fake KYC, fake LLM, in-memory audit and checkpoints, no AWS):

    uv run python -m onboarding.graph.build --case evals/cases/clean_approve.yaml
    uv run python -m onboarding.graph.build --all        # every fixture; non-zero exit on any mismatch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from onboarding.graph import names
from onboarding.graph.nodes import Deps, make_nodes
from onboarding.graph.routes import route_after_approve, route_after_intake
from onboarding.models import CaseState


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver[Any]) -> CompiledStateGraph[Any]:
    nodes = make_nodes(deps)
    builder = StateGraph(CaseState)
    for name, fn in nodes.items():
        builder.add_node(name, cast(Any, fn))
    builder.add_edge(START, names.INTAKE)
    builder.add_conditional_edges(names.INTAKE, route_after_intake, [names.EXTRACT, END])
    builder.add_edge(names.EXTRACT, names.SCREEN)
    builder.add_edge(names.SCREEN, names.ASSESS)
    builder.add_edge(names.ASSESS, names.APPROVE)
    builder.add_conditional_edges(names.APPROVE, route_after_approve, [names.EXECUTE, names.AWAIT_DOCS, END])
    builder.add_edge(names.AWAIT_DOCS, names.INTAKE)
    builder.add_edge(names.EXECUTE, END)
    return builder.compile(checkpointer=checkpointer)


def main(argv: list[str] | None = None) -> int:
    from onboarding.runner import compare_full, load_fixture_cases, run_case

    parser = argparse.ArgumentParser(prog="python -m onboarding.graph.build", description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--case", type=Path, help="one fixture file from evals/cases/")
    group.add_argument(
        "--all", action="store_true", help="run every fixture and compare with its expectations"
    )
    parser.add_argument("--cases-dir", type=Path, default=Path("evals/cases"))
    parser.add_argument("--json", action="store_true", help="print the final state as JSON")
    args = parser.parse_args(argv)

    cases = load_fixture_cases(args.cases_dir)
    selected = (
        list(cases.values()) if args.all else [next(c for c in cases.values() if c.id == args.case.stem)]
    )
    failures = broken = 0
    for case in selected:
        result = run_case(case, full=True)
        problems = compare_full(case, result)
        chain = result.audit.verify()
        s = result.state
        flag = "ok  " if not problems and chain.ok else "FAIL"
        print(
            f"{flag} {case.id:28s} {' > '.join(result.trajectory)} | rating={result.first_pass.risk.rating if result.first_pass and result.first_pass.risk else '-'} "
            f"final={result.view.row.status} customer={s.execution.customer_id if s.execution else '-'} degraded={s.degraded}"
        )
        for p in problems:
            print(f"       - {p}")
        failures += bool(problems)
        broken += not chain.ok
        if args.json:
            print(s.model_dump_json(indent=2))
        result.env.close()
    print(
        f"{len(selected) - failures}/{len(selected)} cases match their expectations end to end; "
        f"audit chains verified: {len(selected) - broken}/{len(selected)}"
    )
    return 1 if failures or broken else 0


if __name__ == "__main__":
    sys.exit(main())
