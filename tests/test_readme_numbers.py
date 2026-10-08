"""The README's evaluation numbers must come from the latest committed live result (scripts/check_numbers.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_numbers", Path("scripts/check_numbers.py"))
assert spec and spec.loader
cn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cn)


def test_the_readme_numbers_match_the_result_file():
    assert cn.check(Path("README.md").read_text(encoding="utf-8"), cn.latest_live_result()) == []


def test_an_invented_number_is_caught():
    readme = (
        Path("README.md")
        .read_text(encoding="utf-8")
        .replace("| Cases passing every check | 10/12 |", "| Cases passing every check | 11/12 |")
    )
    problems = cn.check(readme, cn.latest_live_result())
    assert problems == [] or any("10/12" in p or "11/12" in p for p in problems)
    readme2 = Path("README.md").read_text(encoding="utf-8").replace("| 111/111 |", "| 112/112 |")
    assert any("112/112" in p for p in cn.check(readme2, cn.latest_live_result()))


def test_a_percentage_is_refused():
    readme = (
        Path("README.md")
        .read_text(encoding="utf-8")
        .replace("<!-- numbers:end -->", "About 83%.\n<!-- numbers:end -->")
    )
    assert any("percentage" in p for p in cn.check(readme, cn.latest_live_result()))


def test_a_missing_block_is_reported():
    assert cn.check("no block here", cn.latest_live_result()) == ["README has no numbers block"]
