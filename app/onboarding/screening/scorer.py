"""Deterministic sanctions scorer (DECISIONS D-06).

Name similarity is rapidfuzz `token_sort_ratio` on normalised names. DOB and nationality are compared
separately and only classify a hit: a disagreement demotes "strong" to "possible", it never removes
the hit. Nothing here calls an LLM, and nothing here clears a hit: only an officer disposition does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import yaml
from rapidfuzz import fuzz

from onboarding.models import Agreement, Applicant, FieldAgreement, Hit, Screening
from onboarding.paths import REFERENCE_DIR
from onboarding.screening.normalize import normalize_name
from onboarding.screening.unlist import ListedDob, SanctionsEntry, SanctionsIndex

DEFAULT_CONFIG = REFERENCE_DIR / "screening_config.yaml"


@dataclass(frozen=True)
class ScreeningConfig:
    algorithm: str
    raise_threshold: float
    strong_threshold: float

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> ScreeningConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        cfg = cls(str(raw["algorithm"]), float(raw["raise_threshold"]), float(raw["strong_threshold"]))
        if cfg.algorithm != "rapidfuzz.token_sort_ratio":
            raise ValueError(f"unsupported algorithm {cfg.algorithm}")
        if cfg.strong_threshold < cfg.raise_threshold:
            raise ValueError("strong_threshold must be >= raise_threshold")
        return cfg

    @property
    def algorithm_label(self) -> str:
        return f"{self.algorithm}@rapidfuzz-{version('rapidfuzz')}"


def dob_agreement(applicant_dob: str, listed: list[ListedDob]) -> Agreement:
    """agree: an exact listed date equals the DOB. partial: only the year is consistent (a listed year,
    a year range, or the year of a listed date). disagree: the list gives dates or years and none fit.
    unknown: the list gives no usable date."""
    if not listed:
        return "unknown"
    if any(d.date == applicant_dob for d in listed):
        return "agree"
    year = date.fromisoformat(applicant_dob).year
    years_known = False
    for d in listed:
        years: set[int] = set()
        if d.date and d.date[:4].isdigit():
            years.add(int(d.date[:4]))
        if d.year:
            years.add(d.year)
        if d.from_year and d.to_year:
            years.update(range(d.from_year, d.to_year + 1))
        if years:
            years_known = True
        if year in years:
            return "partial"
    return "disagree" if years_known else "unknown"


def nationality_agreement(applicant_nationality: str, listed: list[str]) -> Agreement:
    if not listed:
        return "unknown"
    wanted = normalize_name(applicant_nationality)
    return "agree" if any(normalize_name(n) == wanted for n in listed) else "disagree"


def _best_pair(queries: list[str], entry: SanctionsEntry) -> tuple[float, str, str]:
    """Best (score, applicant name used, listed name) over all query and listed-name pairs."""
    best = (0.0, "", "")
    listed_names = [entry.name, *(a.name for a in entry.aliases)]
    for q in queries:
        qn = normalize_name(q)
        if not qn:
            continue
        for ln in listed_names:
            cand = normalize_name(ln)
            if not cand:
                continue
            score = fuzz.token_sort_ratio(qn, cand)
            if score > best[0]:
                best = (score, q, ln)
    return best


def _reason(
    hit_score: float,
    used: str,
    matched: str,
    entry: SanctionsEntry,
    index: SanctionsIndex,
    fa: FieldAgreement,
    cfg: ScreeningConfig,
    classification: str,
) -> str:
    return (
        f"Name score {hit_score:.1f} ({cfg.algorithm}) between applicant name '{used}' and listed name "
        f"'{matched}' (entry {entry.entry_id}, {index.source} list, snapshot {index.snapshot_date}). "
        f"Date of birth: {fa.dob}. Nationality: {fa.nationality}. Classified {classification} "
        f"(strong needs score >= {cfg.strong_threshold:g} and no DOB or nationality disagreement)."
    )


def screen_applicant(
    applicant: Applicant,
    extra_names: list[str],
    index: SanctionsIndex,
    cfg: ScreeningConfig,
) -> Screening:
    """Screen the applicant's declared name, aliases and any extra names (for example the name the KYC
    service read from the document) against the index."""
    seen: set[str] = set()
    queries: list[str] = []
    for n in [applicant.name, *applicant.aliases, *extra_names]:
        key = normalize_name(n)
        if key and key not in seen:
            seen.add(key)
            queries.append(n)

    hits: list[Hit] = []
    for entry in index.entries:
        score, used, matched = _best_pair(queries, entry)
        if score < cfg.raise_threshold:
            continue
        fa = FieldAgreement(
            dob=dob_agreement(applicant.dob, entry.dobs),
            nationality=nationality_agreement(applicant.nationality, entry.nationalities),
        )
        strong = score >= cfg.strong_threshold and "disagree" not in (fa.dob, fa.nationality)
        classification: Literal["strong", "possible"] = "strong" if strong else "possible"
        hits.append(
            Hit(
                entry_id=entry.entry_id,
                list_source=index.source,
                matched_name=matched,
                applicant_name_used=used,
                score=round(score, 1),
                classification=classification,
                field_agreement=fa,
                reason=_reason(score, used, matched, entry, index, fa, cfg, classification),
            )
        )
    hits.sort(key=lambda h: (-h.score, h.entry_id))
    return Screening(
        list_source=index.source,
        snapshot_date=index.snapshot_date,
        algorithm=cfg.algorithm_label,
        raise_threshold=cfg.raise_threshold,
        strong_threshold=cfg.strong_threshold,
        names_screened=len(queries),
        hits=hits,
    )
