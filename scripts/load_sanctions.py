"""Rebuild the screening index from the vendored UN snapshot (never run in production).

    uv run python scripts/load_sanctions.py            # rebuild index + manifest from the vendored XML
    uv run python scripts/load_sanctions.py --fetch    # developer only: download a fresh snapshot first

The runtime service reads data/sanctions/un_index.jsonl and MANIFEST.json only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from onboarding.screening import unlist

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "sanctions"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--fetch", action="store_true", help="download a new snapshot (developer use)")
    args = parser.parse_args()

    if args.fetch and os.environ.get("ENVIRONMENT", "dev") in {"qa", "prod"}:
        print("refusing to fetch the sanctions list in qa/prod: the snapshot is vendored", file=sys.stderr)
        return 2

    xml_path = args.dir / unlist.XML_NAME
    manifest_path = args.dir / unlist.MANIFEST_NAME
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    retrieved_on = str(old.get("retrieved_on", datetime.now(UTC).date().isoformat()))

    if args.fetch:
        resp = httpx.get(
            unlist.SOURCE_URL, timeout=60, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        xml_path.write_bytes(resp.content)
        retrieved_on = datetime.now(UTC).date().isoformat()

    entries, generated = unlist.parse_un_xml(xml_path)
    index_path = args.dir / unlist.INDEX_NAME
    unlist.write_index(entries, index_path)
    manifest = unlist.build_manifest(xml_path, index_path, entries, generated, retrieved_on)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"indexed {len(entries)} individuals; snapshot date {manifest['snapshot_date']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
