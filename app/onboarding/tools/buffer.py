"""In-process holding area for uploaded document bytes (DECISIONS D-13).

Bytes are never put in the case state, the checkpoint or the database. They wait here, per process,
until `extract` takes them, or until the time-to-live passes. If the process dies first they are gone and
the case is shown to the officer as "extraction unavailable" (the client re-uploads).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class DocumentBuffer:
    def __init__(self, ttl_s: float = 900.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._items: dict[tuple[str, str], tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def put(self, case_id: str, doc_ref: str, content: bytes) -> None:
        with self._lock:
            self._purge()
            self._items[(case_id, doc_ref)] = (self._clock() + self._ttl, content)

    def take(self, case_id: str, doc_ref: str) -> bytes | None:
        """Remove and return the bytes, or None when absent or expired."""
        with self._lock:
            self._purge()
            item = self._items.pop((case_id, doc_ref), None)
            return item[1] if item else None

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._items)

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, (expires, _) in self._items.items() if expires <= now]:
            del self._items[key]
