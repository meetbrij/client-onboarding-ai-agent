"""Fail if the README's evaluation numbers do not come from the latest committed live result file.

The README marks its numbers block with <!-- numbers:start --> and <!-- numbers:end -->. Every `a/b` fraction in that block
must be one the result file produces, the block must name the result file and its git sha, and every metric the file reports
as passed/of must appear. Percentages and bare decimals are not allowed in the block (write fractions, so nothing is rounded
by hand).

    uv run python scripts/check_numbers.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def latest_live_result() -> Path:
    files = sorted((ROOT / "evals" / "results").glob("*-live.json"))
    if not files:
        sys.exit("no live result file in evals/results/")
    return files[-1]


def allowed_fractions(data: dict) -> set[str]:
    m = data["metrics"]
    out = {f"{m['cases_passed']}/{m['cases']}"}
    for v in m.values():
        if isinstance(v, dict) and "passed" in v:
            out.add(f"{v['passed']}/{v['of']}")
    out.add(
        f"{m['extraction_fields_matching_specimen']['matched']}/{m['extraction_fields_matching_specimen']['of']}"
    )
    hc = m["hit_counts"]
    out.add(f"{hc['tp']}/{hc['tp'] + hc['fn']}")
    for d in (data.get("judge") or {}).get("drafts", {}).values():
        if "total" in d:
            out.add(f"{d['total']}/{2 * len(d['scores'])}")
    return out


def check(readme: str, result_path: Path) -> list[str]:
    data = json.loads(result_path.read_text())
    problems: list[str] = []
    m = re.search(r"<!-- numbers:start -->(.*?)<!-- numbers:end -->", readme, re.S)
    if not m:
        return ["README has no numbers block"]
    block = m.group(1)
    if result_path.name not in block:
        problems.append(f"the numbers block does not link {result_path.name}")
    if data["git_sha"] not in block:
        problems.append(f"the numbers block does not name the run's git sha {data['git_sha']}")
    allowed = allowed_fractions(data)
    for frac in sorted(set(re.findall(r"(?<![\d.])(\d+/\d+)(?![\d/])", block))):
        if frac not in allowed:
            problems.append(f"{frac} is not a figure in {result_path.name}")
    for v in data["metrics"].values():
        if isinstance(v, dict) and "passed" in v and f"{v['passed']}/{v['of']}" not in block:
            problems.append(f"{v['passed']}/{v['of']} (from the result file) is missing from the block")
    if re.search(r"\d\s?%|(?<![\w/.])0\.\d+", block):
        problems.append(
            "the block contains a percentage or a bare decimal; write fractions from the result file"
        )
    for needed in (data["llm_model"], data["sanctions_snapshot"]["date"], data["date"]):
        if needed not in block:
            problems.append(f"the block does not state {needed}")
    return problems


def main() -> int:
    path = latest_live_result()
    problems = check(README.read_text(encoding="utf-8"), path)
    for p in problems:
        print("FAIL", p)
    print(
        f"README numbers checked against {path.name}: {'OK' if not problems else f'{len(problems)} problem(s)'}"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
