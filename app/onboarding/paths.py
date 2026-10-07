"""Where the vendored data and local prompts live.

From a repository checkout they are found relative to the source tree. In the container image the package
is installed into a virtualenv, so the image sets ONBOARDING_DATA_DIR and ONBOARDING_PROMPTS_DIR.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("ONBOARDING_DATA_DIR") or _REPO / "data")
PROMPTS_DIR = Path(os.environ.get("ONBOARDING_PROMPTS_DIR") or _REPO / "prompts")
SANCTIONS_DIR = DATA_DIR / "sanctions"
REFERENCE_DIR = DATA_DIR / "reference"
