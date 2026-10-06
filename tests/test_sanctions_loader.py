from __future__ import annotations

import json
from pathlib import Path

import pytest

from onboarding.screening import unlist
from onboarding.screening.normalize import normalize_name

DATA = Path("data/sanctions")


@pytest.fixture(scope="module")
def index() -> unlist.SanctionsIndex:
    return unlist.SanctionsIndex.load(DATA)


def test_normalize_name():
    assert normalize_name("  Dr. José  O'Brien-Smith ") == "jose o brien smith"
    assert normalize_name("MPAMO, Douglas") == "mpamo douglas"
    assert normalize_name("") == ""


def test_known_entry_found_by_exact_name_and_alias(index):
    by_name = index.find_exact("Khawa Panga Mandro")
    assert [e.entry_id for e in by_name] == ["CDi.009"]
    # token order and case do not matter for the exact index only through normalisation of spelling,
    # an alias is found too:
    assert "CDi.009" in [e.entry_id for e in index.find_exact("kawa panga")]


def test_manifest_matches_files(index):
    m = index.manifest
    assert m["source"] == "UN"
    assert m["snapshot_date"] == "2026-10-03"
    assert m["individuals_indexed"] == len(index.entries) == 736
    assert unlist.sha256_file(DATA / m["raw_file"]) == m["raw_sha256"]
    assert unlist.sha256_file(DATA / m["index_file"]) == m["index_sha256"]


def test_index_rebuild_is_deterministic(tmp_path):
    entries, generated = unlist.parse_un_xml(DATA / unlist.XML_NAME)
    out = tmp_path / "idx.jsonl"
    unlist.write_index(entries, out)
    assert out.read_bytes() == (DATA / unlist.INDEX_NAME).read_bytes()
    assert generated.startswith("2026-10-03")


def test_dob_forms_are_parsed(index):
    e = index.get("CDi.011")
    assert e is not None
    assert [d.date for d in e.dobs] == ["1965-12-28", "1965-12-29"]
    assert e.nationalities == ["Democratic Republic of the Congo"]
    kinds = {d.kind for entry in index.entries for d in entry.dobs}
    assert {"EXACT", "APPROXIMATELY", "BETWEEN"} <= kinds
    assert any(d.year is not None for entry in index.entries for d in entry.dobs)


def test_empty_aliases_are_dropped(index):
    assert all(a.name for e in index.entries for a in e.aliases)


def test_tampered_index_is_rejected(tmp_path):
    for f in (unlist.INDEX_NAME, unlist.MANIFEST_NAME):
        (tmp_path / f).write_bytes((DATA / f).read_bytes())
    (tmp_path / unlist.INDEX_NAME).write_text("{}\n")
    with pytest.raises(ValueError, match="manifest hash"):
        unlist.SanctionsIndex.load(tmp_path)


def test_manifest_is_valid_json_with_required_keys():
    m = json.loads((DATA / unlist.MANIFEST_NAME).read_text())
    for k in ("source_url", "retrieved_on", "date_generated", "raw_sha256", "index_sha256"):
        assert m[k]
