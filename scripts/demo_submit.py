"""Submit a fixture case to a running API (for the local demo): uploads the fixture's SPECIMEN documents.

    uv run python scripts/demo_submit.py near_miss_dob_mismatch            # as submitter-1 on http://localhost:8000
    uv run python scripts/demo_submit.py --list

The fixture documents carry a `case=<id>` line the fake KYC service understands; real documents are never used.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from onboarding.fixtures import load_cases


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("case", nargs="?")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--token", default="dev-submitter-token")
    parser.add_argument("--cases-dir", type=Path, default=Path("evals/cases"))
    args = parser.parse_args()
    cases = load_cases(args.cases_dir)
    if args.list or not args.case:
        for c in cases.values():
            print(f"{c.id:28s} {c.description}")
        return 0
    case = cases[args.case]
    files = {str(d.doc_type): (f"{d.doc_type}.txt", d.content.encode(), "text/plain") for d in case.documents}
    r = httpx.post(
        f"{args.url}/cases",
        headers={"Authorization": f"Bearer {args.token}"},
        data={"applicant": case.applicant.model_dump_json()},
        files=files,
        timeout=60,
    )
    if r.status_code != 201:
        print(r.status_code, r.text, file=sys.stderr)
        return 1
    body = r.json()
    print(
        f"case {body['case_id']} is {body['status']}\nopen {args.url}/ui/cases/{body['case_id']} as an officer (token dev-officer-token)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
