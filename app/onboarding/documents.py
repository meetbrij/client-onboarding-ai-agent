"""Retention of the original uploaded documents (DECISIONS D-24, which replaces D-13's "never stored").

Officers must be able to look at what the applicant submitted, above all when extraction failed and a person has to
verify by eye. So the original bytes are kept in a restricted store: S3 in the cluster (private bucket, server-side
encryption, TLS only, no public access) and a local directory in development. The store is separate from the case
state: bytes never enter the checkpoint, the audit log, logs, traces or LLM prompts, and the original file name is never
kept (names can carry personal data).

Access rules (enforced in the service, not here)
- only officers can read a document, only through the API, and the read is audited before any byte is returned;
- the SHA-256 recorded at upload is checked on every read, so a changed object is detected;
- documents are deleted with the case's checkpoints after the retention window, and by the bucket's lifecycle rule.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Types an officer's browser may show inline. Anything else is offered as a download, never rendered.
INLINE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf", "text/plain"}
)


class DocumentStoreError(RuntimeError):
    """The store could not be reached or refused the operation."""


@dataclass(frozen=True)
class StoredDocument:
    content: bytes
    content_type: str


class DocumentStore(Protocol):
    def put(self, case_id: str, doc_ref: str, content: bytes, content_type: str) -> None: ...

    def get(self, case_id: str, doc_ref: str) -> StoredDocument | None: ...

    def delete(self, case_id: str, doc_ref: str) -> None: ...

    def delete_case(self, case_id: str) -> int: ...


def check_ids(case_id: str, doc_ref: str | None = None) -> None:
    """Identifiers become part of a path or an object key: allow only plain characters (no traversal, no slashes)."""
    for value in (case_id, doc_ref):
        if value is not None and not SAFE_ID.match(value):
            raise DocumentStoreError("invalid identifier")


def sniff_content_type(data: bytes) -> str:
    """Decide the file type from its first bytes. The client-supplied header is never trusted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    head = data[:4096]
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    if text and all(c.isprintable() or c in "\r\n\t" for c in text):
        return "text/plain"  # shown as plain text with nosniff: even HTML-looking content is never rendered
    return "application/octet-stream"


class MemoryDocumentStore:
    """For tests and the offline runner."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], StoredDocument] = {}

    def put(self, case_id: str, doc_ref: str, content: bytes, content_type: str) -> None:
        check_ids(case_id, doc_ref)
        self._items[(case_id, doc_ref)] = StoredDocument(content, content_type)

    def get(self, case_id: str, doc_ref: str) -> StoredDocument | None:
        check_ids(case_id, doc_ref)
        return self._items.get((case_id, doc_ref))

    def delete(self, case_id: str, doc_ref: str) -> None:
        check_ids(case_id, doc_ref)
        self._items.pop((case_id, doc_ref), None)

    def delete_case(self, case_id: str) -> int:
        check_ids(case_id)
        keys = [k for k in self._items if k[0] == case_id]
        for k in keys:
            del self._items[k]
        return len(keys)


class LocalDocumentStore:
    """A directory per case. Development only: no encryption, no access control beyond file permissions."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _dir(self, case_id: str) -> Path:
        check_ids(case_id)
        return self.directory / case_id

    def put(self, case_id: str, doc_ref: str, content: bytes, content_type: str) -> None:
        check_ids(case_id, doc_ref)
        d = self._dir(case_id)
        try:
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            (d / doc_ref).write_bytes(content)
            (d / f"{doc_ref}.meta").write_text(json.dumps({"content_type": content_type}))
        except OSError as exc:
            raise DocumentStoreError(f"could not write the document: {type(exc).__name__}") from exc

    def get(self, case_id: str, doc_ref: str) -> StoredDocument | None:
        check_ids(case_id, doc_ref)
        path = self._dir(case_id) / doc_ref
        if not path.is_file():
            return None
        meta = json.loads((self._dir(case_id) / f"{doc_ref}.meta").read_text())
        return StoredDocument(path.read_bytes(), meta["content_type"])

    def delete(self, case_id: str, doc_ref: str) -> None:
        check_ids(case_id, doc_ref)
        for name in (doc_ref, f"{doc_ref}.meta"):
            (self._dir(case_id) / name).unlink(missing_ok=True)

    def delete_case(self, case_id: str) -> int:
        d = self._dir(case_id)
        if not d.is_dir():
            return 0
        count = sum(1 for p in d.iterdir() if not p.name.endswith(".meta"))
        shutil.rmtree(d)
        return count


class S3DocumentStore:
    """A private S3 bucket. Objects are written with server-side encryption (AES-256, or a KMS key when one is
    configured); the bucket itself blocks public access and refuses non-TLS requests (infra/terraform)."""

    def __init__(self, client: Any, bucket: str, prefix: str = "", kms_key_id: str | None = None) -> None:
        self.client, self.bucket, self.kms_key_id = client, bucket, kms_key_id
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _key(self, case_id: str, doc_ref: str) -> str:
        return f"{self.prefix}{case_id}/{doc_ref}"

    def put(self, case_id: str, doc_ref: str, content: bytes, content_type: str) -> None:
        check_ids(case_id, doc_ref)
        extra: dict[str, Any] = (
            {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms_key_id}
            if self.kms_key_id
            else {"ServerSideEncryption": "AES256"}
        )
        try:
            # The object is stored as opaque bytes; the sniffed type travels as metadata and is applied by the API on read.
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(case_id, doc_ref),
                Body=content,
                ContentType="application/octet-stream",
                Metadata={"detected-type": content_type},
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 - boto raises many types; callers only need "it failed"
            raise DocumentStoreError(f"could not store the document: {type(exc).__name__}") from exc

    def get(self, case_id: str, doc_ref: str) -> StoredDocument | None:
        check_ids(case_id, doc_ref)
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(case_id, doc_ref))
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "response", {}).get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise DocumentStoreError(f"could not read the document: {type(exc).__name__}") from exc
        return StoredDocument(
            obj["Body"].read(), obj.get("Metadata", {}).get("detected-type", "application/octet-stream")
        )

    def delete(self, case_id: str, doc_ref: str) -> None:
        check_ids(case_id, doc_ref)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(case_id, doc_ref))
        except Exception as exc:  # noqa: BLE001
            raise DocumentStoreError(f"could not delete the document: {type(exc).__name__}") from exc

    def delete_case(self, case_id: str) -> int:
        check_ids(case_id)
        prefix = f"{self.prefix}{case_id}/"
        deleted = 0
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
                    deleted += len(keys)
        except Exception as exc:  # noqa: BLE001
            raise DocumentStoreError(f"could not delete the documents: {type(exc).__name__}") from exc
        return deleted
