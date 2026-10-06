"""The mock bank stands in for an external system: it must not import the onboarding package."""

import ast
from pathlib import Path


def test_mock_bank_does_not_import_onboarding():
    for path in Path("app/mock_bank").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n == "onboarding" or n.startswith("onboarding.") for n in names), path
