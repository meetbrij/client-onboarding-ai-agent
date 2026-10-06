from __future__ import annotations

from pathlib import Path

import pytest

from onboarding.models import Applicant
from onboarding.screening.scorer import (
    ScreeningConfig,
    dob_agreement,
    nationality_agreement,
    screen_applicant,
)
from onboarding.screening.unlist import ListedDob, SanctionsIndex


@pytest.fixture(scope="module")
def index() -> SanctionsIndex:
    return SanctionsIndex.load(Path("data/sanctions"))


@pytest.fixture(scope="module")
def cfg() -> ScreeningConfig:
    return ScreeningConfig.load()


def applicant(name: str, dob="1990-01-01", nat="Utopia", aliases=None) -> Applicant:
    return Applicant(
        name=name,
        aliases=aliases or [],
        dob=dob,
        nationality=nat,
        residence_country="Utopia",
        occupation="Tester",
    )


def test_exact_listed_name_is_strong_when_corroborated(index, cfg):
    s = screen_applicant(
        applicant("Khawa Panga Mandro", "1973-08-20", "Democratic Republic of the Congo"), [], index, cfg
    )
    top = s.hits[0]
    assert (top.entry_id, top.score, top.classification) == ("CDi.009", 100.0, "strong")
    assert top.field_agreement.dob == "agree" and top.field_agreement.nationality == "agree"


def test_transposed_tokens_still_score_100(index, cfg):
    s = screen_applicant(
        applicant("Mandro Khawa Panga", "1973-08-20", "Democratic Republic of the Congo"), [], index, cfg
    )
    assert s.hits[0].entry_id == "CDi.009" and s.hits[0].score == 100.0


def test_typo_variant_is_a_hit(index, cfg):
    s = screen_applicant(
        applicant("Khawa Pangaa Mandro", "1973-08-20", "Democratic Republic of the Congo"), [], index, cfg
    )
    assert [h.entry_id for h in s.hits] == ["CDi.009"] and s.hits[0].score == 97.3


def test_dob_and_nationality_mismatch_demotes_strong_to_possible_but_keeps_the_hit(index, cfg):
    s = screen_applicant(applicant("Douglas Iruta Mpamoh", "1991-02-14", "Uganda"), [], index, cfg)
    h = s.hits[0]
    assert h.entry_id == "CDi.011" and h.score >= cfg.strong_threshold
    assert h.field_agreement.dob == "disagree" and h.field_agreement.nationality == "disagree"
    assert h.classification == "possible"


def test_alias_match_uses_the_applicant_alias(index, cfg):
    s = screen_applicant(
        applicant(
            "Etienne Kabasele Mbuyi",
            "1971-05-23",
            "Democratic Republic of the Congo",
            aliases=["Floribert Njabou"],
        ),
        [],
        index,
        cfg,
    )
    h = s.hits[0]
    assert h.entry_id == "CDi.021" and h.applicant_name_used == "Floribert Njabou"
    assert h.classification == "strong"


def test_extra_name_from_the_document_is_screened_too(index, cfg):
    s = screen_applicant(applicant("Layla Nasser"), ["Khawa Panga Mandro"], index, cfg)
    assert s.hits[0].entry_id == "CDi.009" and s.names_screened == 2


def test_shared_given_name_is_not_a_hit(index, cfg):
    assert screen_applicant(applicant("Douglas Peterson"), [], index, cfg).hits == []


def test_threshold_is_inclusive_and_compares_the_unrounded_score(index, cfg):
    exact = applicant("Khawa Panga Mandro")  # scores exactly 100
    assert screen_applicant(exact, [], index, ScreeningConfig(cfg.algorithm, 100.0, 100.0)).hits
    assert not screen_applicant(exact, [], index, ScreeningConfig(cfg.algorithm, 100.1, 100.1)).hits
    # 97.297... displays as 97.3 but is below a 97.3 threshold
    typo = applicant("Khawa Pangaa Mandro")
    assert not screen_applicant(typo, [], index, ScreeningConfig(cfg.algorithm, 97.3, 99.0)).hits


def test_result_records_source_snapshot_and_algorithm(index, cfg):
    s = screen_applicant(applicant("Khawa Pangaa Mandro"), [], index, cfg)
    assert s.list_source == "UN" and s.snapshot_date == "2026-10-03"
    assert s.algorithm.startswith("rapidfuzz.token_sort_ratio@rapidfuzz-")
    assert "CDi.009" in s.hits[0].reason and "2026-10-03" in s.hits[0].reason


def test_hits_are_ordered_by_score_then_id(index, cfg):
    s = screen_applicant(applicant("Ayman Sabawi Ibrahim Hasan Tikriti"), [], index, cfg)
    scores = [h.score for h in s.hits]
    assert scores == sorted(scores, reverse=True) and len(s.hits) >= 2


def test_empty_names_are_ignored(index, cfg):
    assert screen_applicant(applicant("   ", aliases=["", "!!"]), [], index, cfg).names_screened == 0


@pytest.mark.parametrize(
    ("dob", "listed", "expected"),
    [
        ("1973-08-20", [ListedDob("EXACT", date="1973-08-20")], "agree"),
        (
            "1973-08-20",
            [ListedDob("EXACT", date="1973-08-21"), ListedDob("EXACT", date="1973-08-20")],
            "agree",
        ),
        ("1973-08-20", [ListedDob("EXACT", date="1973-01-01")], "partial"),
        ("1973-08-20", [ListedDob("APPROXIMATELY", year=1973)], "partial"),
        ("1973-08-20", [ListedDob("BETWEEN", from_year=1970, to_year=1975)], "partial"),
        ("1980-08-20", [ListedDob("BETWEEN", from_year=1970, to_year=1975)], "disagree"),
        ("1980-08-20", [ListedDob("EXACT", date="1973-08-20")], "disagree"),
        ("1980-08-20", [ListedDob("EXACT")], "unknown"),
        ("1980-08-20", [], "unknown"),
    ],
)
def test_dob_agreement(dob, listed, expected):
    assert dob_agreement(dob, listed) == expected


def test_nationality_agreement():
    assert nationality_agreement("Iraq", ["Iraq"]) == "agree"
    assert nationality_agreement("iraq", ["IRAQ", "Jordan"]) == "agree"
    assert nationality_agreement("Spain", ["Iraq"]) == "disagree"
    assert nationality_agreement("Spain", []) == "unknown"


def test_config_validation(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("algorithm: other\nraise_threshold: 85\nstrong_threshold: 92\n")
    with pytest.raises(ValueError, match="unsupported"):
        ScreeningConfig.load(bad)
    bad.write_text("algorithm: rapidfuzz.token_sort_ratio\nraise_threshold: 90\nstrong_threshold: 80\n")
    with pytest.raises(ValueError, match="strong_threshold"):
        ScreeningConfig.load(bad)
