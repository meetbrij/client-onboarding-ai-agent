"""The LLM never decides: routing, rating, hits and the recommendation action must not depend on LLM output.

Static checks (this file) plus a behavioural check (test_llm_never_decides.py)."""

from __future__ import annotations

import ast
from pathlib import Path

from onboarding.models import LLM_FIELDS, CaseState, Hit, Recommendation, Risk

APP = Path("app/onboarding")
DETERMINISTIC_PACKAGES = [APP / "rules", APP / "screening"]


def names_used(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
    return out


def test_route_module_never_touches_an_llm_field():
    used = names_used(APP / "graph" / "routes.py")
    assert used.isdisjoint(LLM_FIELDS), sorted(used & LLM_FIELDS)


def test_llm_field_list_matches_the_models():
    """If someone adds an LLM-produced field to the state, it must be listed so the route check covers it."""
    fields = set(CaseState.model_fields) | set(Hit.model_fields) | set(Recommendation.model_fields)
    assert LLM_FIELDS <= fields
    # decision-bearing fields are deliberately not LLM fields
    decision_fields = {"action", "rating", "classification", "disposition", "fired_rules", "status"}
    assert decision_fields.isdisjoint(LLM_FIELDS)
    assert {"action", "rating"} <= set(Recommendation.model_fields) | set(Risk.model_fields)


def test_conditional_edges_are_defined_only_with_functions_from_routes():
    tree = ast.parse((APP / "graph" / "build.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_conditional_edges":
            router = node.args[1]
            assert isinstance(router, ast.Name) and router.id.startswith("route_"), ast.dump(router)


def test_deterministic_packages_do_not_import_the_llm_package():
    for pkg in DETERMINISTIC_PACKAGES:
        for path in pkg.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                assert not any(m.startswith("onboarding.llm") for m in mods), (path, mods)


def test_deterministic_packages_never_read_llm_fields():
    for pkg in DETERMINISTIC_PACKAGES:
        for path in pkg.rglob("*.py"):
            used = names_used(path) - {"explanation"}  # R-xxx rules carry their own fixed explanation text
            assert used.isdisjoint(LLM_FIELDS - {"explanation"}), (path, sorted(used & LLM_FIELDS))
