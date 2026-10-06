"""Name normalisation shared by the loader, the scorer and the fixture checks."""

from __future__ import annotations

import re
import unicodedata

_HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "dr", "prof", "sir"})
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_name(name: str) -> str:
    """Lower-case ASCII tokens separated by single spaces; honorifics removed.

    NFKD plus dropping combining marks folds accents (e.g. e-acute to e). Characters that do
    not decompose to ASCII (Arabic, Cyrillic) are dropped, so original-script names are not
    matched by this normaliser; that is a documented limitation (docs/DECISIONS.md D-06).
    """
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    tokens = [t for t in _NON_ALNUM.split(folded) if t and t not in _HONORIFICS]
    return " ".join(tokens)
