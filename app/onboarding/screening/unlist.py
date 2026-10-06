"""Parse the UN Security Council Consolidated List (XML) into a source-neutral index.

The index is built offline by scripts/load_sanctions.py from the vendored snapshot in
data/sanctions/. Nothing here fetches anything at runtime; the only network code is the
explicit `fetch_snapshot` helper used by the loader script's --fetch flag.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from onboarding.screening.normalize import normalize_name

SOURCE = "UN"
SOURCE_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
XML_NAME = "un_consolidated.xml"
INDEX_NAME = "un_index.jsonl"
MANIFEST_NAME = "MANIFEST.json"


@dataclass(frozen=True)
class Alias:
    name: str
    quality: str  # "Good", "Low", "script" (original-script name) or "" when the list gives none


@dataclass(frozen=True)
class ListedDob:
    kind: str  # EXACT, APPROXIMATELY, BETWEEN or "" when the list gives none
    date: str | None = None  # as printed: YYYY-MM-DD or a partial form
    year: int | None = None
    from_year: int | None = None
    to_year: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class SanctionsEntry:
    entry_id: str  # the list's reference number, for example "CDi.006"
    data_id: str
    source: str
    list_type: str  # which UN regime or committee listed the individual
    name: str
    aliases: list[Alias] = field(default_factory=list)
    dobs: list[ListedDob] = field(default_factory=list)
    nationalities: list[str] = field(default_factory=list)
    listed_on: str | None = None


def _text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def parse_individual(node: ET.Element) -> SanctionsEntry:
    name = " ".join(
        p for p in (_text(node, t) for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")) if p
    )
    aliases: list[Alias] = []
    seen: set[str] = set()
    for a in node.findall("INDIVIDUAL_ALIAS"):
        alias = _text(a, "ALIAS_NAME")
        if alias and alias not in seen:
            seen.add(alias)
            aliases.append(Alias(alias, _text(a, "QUALITY")))
    original = _text(node, "NAME_ORIGINAL_SCRIPT")
    if original and original not in seen:
        aliases.append(Alias(original, "script"))
    dobs = [
        ListedDob(
            kind=_text(d, "TYPE_OF_DATE"),
            date=_text(d, "DATE") or None,
            year=_int(_text(d, "YEAR")),
            from_year=_int(_text(d, "FROM_YEAR")),
            to_year=_int(_text(d, "TO_YEAR")),
            note=_text(d, "NOTE") or None,
        )
        for d in node.findall("INDIVIDUAL_DATE_OF_BIRTH")
    ]
    nationalities = [v.text.strip() for v in node.findall("NATIONALITY/VALUE") if v.text and v.text.strip()]
    return SanctionsEntry(
        entry_id=_text(node, "REFERENCE_NUMBER"),
        data_id=_text(node, "DATAID"),
        source=SOURCE,
        list_type=_text(node, "UN_LIST_TYPE"),
        name=name,
        aliases=aliases,
        dobs=dobs,
        nationalities=nationalities,
        listed_on=_text(node, "LISTED_ON") or None,
    )


def parse_un_xml(path: Path) -> tuple[list[SanctionsEntry], str]:
    """Return (individuals sorted by entry_id, `dateGenerated` attribute of the list).

    Entities are not indexed: applicants are individuals. This is recorded in the manifest.
    """
    root = ET.parse(path).getroot()  # noqa: S314 - vendored file, not untrusted input at runtime
    generated = root.attrib.get("dateGenerated", "")
    individuals = root.find("INDIVIDUALS")
    entries = [parse_individual(n) for n in (individuals if individuals is not None else [])]
    entries.sort(key=lambda e: e.entry_id)
    return entries, generated


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_index(entries: list[SanctionsEntry], path: Path) -> None:
    lines = [json.dumps(asdict(e), ensure_ascii=False, sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_manifest(
    xml_path: Path, index_path: Path, entries: list[SanctionsEntry], generated: str, retrieved_on: str
) -> dict[str, object]:
    return {
        "source": SOURCE,
        "source_name": "UN Security Council Consolidated List",
        "source_url": SOURCE_URL,
        "retrieved_on": retrieved_on,
        "date_generated": generated,
        "snapshot_date": generated[:10],
        "raw_file": xml_path.name,
        "raw_sha256": sha256_file(xml_path),
        "index_file": index_path.name,
        "index_sha256": sha256_file(index_path),
        "individuals_indexed": len(entries),
        "entities_indexed": 0,
        "notes": (
            "Individuals only; entities are not indexed. The list's terms of use were not "
            "reviewed when the snapshot was taken: confirm them before redistributing."
        ),
    }


class SanctionsIndex:
    """Read-only view of the vendored index. Verifies file hashes against the manifest on load."""

    def __init__(self, entries: list[SanctionsEntry], manifest: dict[str, object]) -> None:
        self.entries = entries
        self.manifest = manifest
        self._by_id = {e.entry_id: e for e in entries}
        self._by_name: dict[str, list[SanctionsEntry]] = {}
        for e in entries:
            for n in [e.name, *(a.name for a in e.aliases)]:
                key = normalize_name(n)
                if key:
                    self._by_name.setdefault(key, []).append(e)

    @property
    def source(self) -> str:
        return str(self.manifest["source"])

    @property
    def snapshot_date(self) -> str:
        return str(self.manifest["snapshot_date"])

    def get(self, entry_id: str) -> SanctionsEntry | None:
        return self._by_id.get(entry_id)

    def find_exact(self, name: str) -> list[SanctionsEntry]:
        """Entries whose name or alias equals `name` after normalisation."""
        return list(self._by_name.get(normalize_name(name), []))

    @classmethod
    def load(cls, directory: Path) -> SanctionsIndex:
        manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
        index_path = directory / str(manifest["index_file"])
        if sha256_file(index_path) != manifest["index_sha256"]:
            raise ValueError(f"{index_path.name} does not match the manifest hash; rebuild it")
        entries = []
        for line in index_path.read_text(encoding="utf-8").splitlines():
            raw = json.loads(line)
            entries.append(
                SanctionsEntry(
                    **{
                        **raw,
                        "aliases": [Alias(**a) for a in raw["aliases"]],
                        "dobs": [ListedDob(**d) for d in raw["dobs"]],
                    }
                )
            )
        return cls(entries, manifest)
