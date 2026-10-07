"""Smoke test for a deployed environment: submit one synthetic case and check it reaches the officer.

    SMOKE_TOKEN=... python scripts/smoke_test.py --url http://127.0.0.1:8080

Uses only httpx and the standard library, so the pipeline can run it without installing the project. The token
is a submitter-only token (it cannot see screening results or decide anything). The case is a SPECIMEN: a
fictional applicant and two plain-text documents. P3's KYC service will not read text files, so extraction is
expected to be reported unavailable; the case must still reach `awaiting_officer`, which is the "degrade, don't
fail" behaviour. The script says so instead of hiding it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

APPLICANT = {
    "name": "Smoke Test Specimen",
    "aliases": [],
    "dob": "1990-01-01",
    "nationality": "United Arab Emirates",
    "residence_country": "United Arab Emirates",
    "occupation": "Test engineer",
}
DOC = b"SPECIMEN - SYNTHETIC SMOKE-TEST DOCUMENT, NOT A REAL DOCUMENT\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default=os.environ.get("SMOKE_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        print("set SMOKE_TOKEN or pass --token", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {args.token}"}

    with httpx.Client(base_url=args.url, timeout=120) as client:
        health = client.get("/healthz")
        print(f"healthz: {health.status_code} {health.text.strip()}")
        if health.status_code != 200:
            return 1
        r = client.post(
            "/cases",
            headers=headers,
            data={"applicant": json.dumps(APPLICANT)},
            files={
                "id_document": ("id.txt", DOC, "text/plain"),
                "proof_of_address": ("poa.txt", DOC, "text/plain"),
            },
        )
        if r.status_code != 201:
            print(f"create case: HTTP {r.status_code} {r.text[:300]}", file=sys.stderr)
            return 1
        body = r.json()
        print(f"case {body['case_id']}: status {body['status']}, waiting on {body['waiting_on']}")
        if body.get("last_error"):
            print(f"note: the run reported {body['last_error']}")
        if body["status"] != "awaiting_officer":
            print("FAIL: the case did not reach the officer", file=sys.stderr)
            return 1
        listed = client.get("/cases", headers=headers).json()
        if body["case_id"] not in {c["case_id"] for c in listed}:
            print("FAIL: the new case is not in the submitter's list", file=sys.stderr)
            return 1
    print(
        "OK: the case reached the officer. (Extraction may be unavailable until P3's Bedrock quota is raised; "
        "an officer can see it on the case page.)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
