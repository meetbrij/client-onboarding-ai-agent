"""`python -m onboarding.audit verify`: prove the audit chain is intact (the `verify_audit_chain` command).

Exit status 0 when the chain verifies, 1 when a row is broken (the first one is printed), 2 on usage errors.
Reads DATABASE_URL, or pass --database-url.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine

from onboarding.audit.postgres import PostgresAuditLog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m onboarding.audit", description=__doc__)
    parser.add_argument("command", choices=["verify"])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args(argv)
    if not args.database_url:
        print("set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    result = PostgresAuditLog(create_engine(args.database_url)).verify()
    if result.ok:
        print(
            f"audit chain OK: {result.rows_checked} rows, head seq {result.head_seq}, head hash {result.head_hash}"
        )
        return 0
    print(
        f"audit chain BROKEN at seq {result.first_broken_seq}: {result.reason} ({result.rows_checked} rows verified before it)"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
