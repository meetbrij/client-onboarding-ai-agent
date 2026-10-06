"""Reference lists for the jurisdiction and occupation rules, loaded from data/reference/."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from onboarding.paths import REFERENCE_DIR
from onboarding.screening.normalize import normalize_name

DEFAULT_DIR = REFERENCE_DIR


@dataclass(frozen=True)
class OccupationCategory:
    category: str
    keywords: tuple[str, ...]  # normalised


@dataclass(frozen=True)
class Reference:
    jurisdictions_as_of: str
    call_for_action: frozenset[str]  # normalised country names
    increased_monitoring: frozenset[str]
    occupations_as_of: str
    occupations: tuple[OccupationCategory, ...]

    @classmethod
    def load(cls, directory: Path = DEFAULT_DIR) -> Reference:
        j = yaml.safe_load((directory / "jurisdictions.yaml").read_text(encoding="utf-8"))
        o = yaml.safe_load((directory / "occupations.yaml").read_text(encoding="utf-8"))
        return cls(
            jurisdictions_as_of=str(j["as_of"]),
            call_for_action=frozenset(normalize_name(c) for c in j["call_for_action"]),
            increased_monitoring=frozenset(normalize_name(c) for c in j["increased_monitoring"]),
            occupations_as_of=str(o["as_of"]),
            occupations=tuple(
                OccupationCategory(c["category"], tuple(normalize_name(k) for k in c["keywords"]))
                for c in o["high_risk"]
            ),
        )
