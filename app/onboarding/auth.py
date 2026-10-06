"""Officer and submitter identity (DECISIONS D-08): per-person bearer tokens, stored only as SHA-256 hashes.

This stands in for a bank's SSO. It gives what the controls need: every decision carries a named person, roles
limit what a person may do, and nobody can approve a case they submitted (enforced in the service). In `dev`
only, built-in local tokens exist so the stack runs without secrets; qa and prod refuse to start without real ones.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Literal

Role = Literal["officer", "submitter"]

DEV_TOKENS = {
    "dev-submitter-token": ("submitter-1", "submitter"),
    "dev-officer-token": ("officer-1", "officer"),
    "dev-officer2-token": ("officer-2", "officer"),
}


@dataclass(frozen=True)
class Principal:
    id: str
    role: Role

    @property
    def is_officer(self) -> bool:
        return self.role == "officer"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, entries: dict[str, Principal]) -> None:
        self._by_hash = entries  # sha256 hex of the token -> principal

    @classmethod
    def from_json(cls, raw: str) -> TokenStore:
        entries: dict[str, Principal] = {}
        for item in json.loads(raw):
            if item["role"] not in ("officer", "submitter"):
                raise ValueError(f"unknown role {item['role']!r}")
            entries[str(item["sha256"]).lower()] = Principal(str(item["id"]), item["role"])
        return cls(entries)

    @classmethod
    def dev(cls) -> TokenStore:
        return cls({hash_token(t): Principal(i, r) for t, (i, r) in DEV_TOKENS.items()})  # type: ignore[arg-type]

    def authenticate(self, token: str | None) -> Principal | None:
        if not token:
            return None
        digest = hash_token(token)
        found = None
        for known, principal in self._by_hash.items():  # compare against every entry: no early exit
            if hmac.compare_digest(known, digest):
                found = principal
        return found
