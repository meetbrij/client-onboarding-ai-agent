"""Test double for P3's KYC service (POST /documents), driven by the recorded responses in
evals/cases/. Local development and tests only: it is not part of the product and not deployed.

The "document" is a small text file containing a `case=<id>` line (see the fixtures); the
service answers with the case's recorded response for the posted document_type, or HTTP 502
when the case says the KYC service is unavailable (P3 returns 502 when extraction fails).
"""

from __future__ import annotations

import hmac
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile

from onboarding.fixtures import Case, load_cases

_CASE_LINE = re.compile(r"^case=(\S+)$", re.MULTILINE)


def create_app(cases_dir: Path | None = None, api_key: str | None = None) -> FastAPI:
    directory = cases_dir or Path(os.environ.get("FAKE_KYC_CASES_DIR", "evals/cases"))
    key = api_key if api_key is not None else os.environ.get("FAKE_KYC_API_KEY") or None
    cases: dict[str, Case] = load_cases(directory)
    app = FastAPI(title="Fake KYC service (test double)", version="0.1.0")

    def require_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
        if key and not (x_api_key and hmac.compare_digest(x_api_key, key)):
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "cases": str(len(cases))}

    @app.post("/documents", status_code=201, dependencies=[Depends(require_key)])
    def upload_document(
        file: Annotated[UploadFile, File()], document_type: Annotated[str, Form()]
    ) -> dict[str, Any]:
        text = file.file.read(65536).decode("utf-8", errors="replace")
        match = _CASE_LINE.search(text)
        case = cases.get(match.group(1)) if match else None
        if case is None:
            raise HTTPException(status_code=415, detail="not a SPECIMEN fixture document")
        if case.kyc_mode == "unavailable":
            raise HTTPException(status_code=502, detail="extraction failed")
        recorded = case.kyc_response.get(document_type)  # type: ignore[call-overload]
        if recorded is None:
            raise HTTPException(status_code=422, detail="no recorded response for this document type")
        body = recorded.model_dump()
        body["id"] = str(uuid.uuid4())
        body["created_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
        return body

    return app


def get_app() -> FastAPI:
    """`uvicorn fake_kyc.main:get_app --factory`."""
    return create_app()
