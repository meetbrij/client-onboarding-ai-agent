"""Records what the chosen thresholds do on the development set (not the eval cases).
If a threshold, the normaliser or the snapshot changes these numbers, update the expectations and
data/reference/screening_config.yaml deliberately, with a DECISIONS entry."""

from __future__ import annotations

from pathlib import Path

import yaml

from onboarding.models import Applicant
from onboarding.screening.scorer import ScreeningConfig, screen_applicant
from onboarding.screening.unlist import SanctionsIndex

DEV = yaml.safe_load(Path("data/reference/screening_devset.yaml").read_text(encoding="utf-8"))
INDEX = SanctionsIndex.load(Path("data/sanctions"))


def applicant(name: str) -> Applicant:
    return Applicant(
        name=name, dob="1990-01-01", nationality="Utopia", residence_country="Utopia", occupation="Tester"
    )


def run(threshold: float) -> tuple[int, list[str]]:
    cfg = ScreeningConfig("rapidfuzz.token_sort_ratio", threshold, max(threshold, 92.0))
    caught = sum(
        any(h.entry_id == v["entry_id"] for h in screen_applicant(applicant(v["name"]), [], INDEX, cfg).hits)
        for v in DEV["variants"]
    )
    false_positives = [n for n in DEV["negatives"] if screen_applicant(applicant(n), [], INDEX, cfg).hits]
    return caught, false_positives


def test_chosen_threshold_results_are_as_documented():
    caught, fps = run(ScreeningConfig.load().raise_threshold)
    assert caught == 8  # the two misses omit a middle name (token_sort_ratio 84.2 and 66.7)
    assert fps == ["Rania Abdullah"]  # a common-looking name scores 85.7 against a listed name


def test_alternative_thresholds_for_the_record():
    assert run(80.0) == (9, ["Rania Abdullah", "Yusuf Khan"])
    assert run(90.0) == (8, [])
