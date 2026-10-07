"""Prompt store. Prompts live in Langfuse prompt management in production (Phase 3, DECISIONS D-17);
this module is the interface and the checked-in fallback. Every LLM call records the prompt name and
version in the audit log and the trace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from onboarding.paths import PROMPTS_DIR

PROMPT_NAMES = (
    "onboarding-summarise-case",
    "onboarding-explain-recommendation",
    "onboarding-draft-missing-docs",
    "onboarding-annotate-hit",
)
LOCAL_VERSION = "local-fallback"
DEFAULT_DIR = PROMPTS_DIR


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    template: str

    def render(self, **values: str) -> str:
        out = self.template
        for key, value in values.items():
            out = out.replace("{{" + key + "}}", value)
        if "{{" in out:
            raise ValueError(f"prompt {self.name} has unfilled placeholders")
        return out


class PromptStore(Protocol):
    def get(self, name: str) -> Prompt: ...


class LocalPromptStore:
    def __init__(self, directory: Path = DEFAULT_DIR) -> None:
        self.directory = directory

    def get(self, name: str) -> Prompt:
        if name not in PROMPT_NAMES:
            raise KeyError(f"unknown prompt {name}")
        return Prompt(name, LOCAL_VERSION, (self.directory / f"{name}.txt").read_text(encoding="utf-8"))
