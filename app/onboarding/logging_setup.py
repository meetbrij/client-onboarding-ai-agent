"""JSON logs to stdout with an allowlist of fields. Anything not on the list (request bodies, applicant details,
extracted values) cannot reach the logs even if a caller passes it as `extra`."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

ALLOWED_EXTRA = ("case_id", "segment", "customer_id", "idempotency_key", "status", "path", "method", "code")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ALLOWED_EXTRA:
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info and record.exc_info[0] is not None:
            out["error"] = record.exc_info[0].__name__  # the class only: messages can echo user data
        return json.dumps(out, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(
        logging.WARNING
    )  # access lines carry paths and ids; metrics cover it
