"""HTTP API for submitters and officers, and the home of the officer UI (api/ui.py).

Roles (DECISIONS D-08): `submitter` sends cases and documents and sees only the status of their own cases (never
screening results or the risk assessment); `officer` sees everything and decides. Nobody decides a case they submitted.
"""

# No `from __future__ import annotations` here: FastAPI resolves the endpoint annotations (Depends on local
# functions) at runtime, which string annotations would break.
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from onboarding.auth import Principal, TokenStore
from onboarding.bootstrap import build_service_from_env
from onboarding.config import Settings
from onboarding.decision import DecisionRequest
from onboarding.logging_setup import configure_logging
from onboarding.models import Applicant, DocType
from onboarding.service import (
    CaseError,
    CaseNotFound,
    CaseService,
    CaseView,
    Conflict,
    Forbidden,
    NotAllowed,
    Unavailable,
    UploadedDoc,
)

log = logging.getLogger("onboarding.api")
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


class CaseOut(BaseModel):
    case_id: str
    status: str
    applicant_name: str
    waiting_on: str | None = None
    interrupt_id: str | None = None
    created_at: str
    updated_at: str
    last_error: str | None = None
    customer_id: str | None = None
    needed_documents: list[str] = []
    # officer only
    risk_rating: str | None = None
    recommendation: str | None = None
    approval: dict[str, Any] | None = None
    degraded: list[str] | None = None
    final: dict[str, Any] | None = None


def case_out(view: CaseView, who: Principal) -> CaseOut:
    row, state = view.row, view.state
    pending = view.pending or {}
    out = CaseOut(
        case_id=row.case_id,
        status=row.status,
        applicant_name=row.applicant_name,
        waiting_on=row.waiting_on,
        interrupt_id=row.interrupt_id,
        created_at=row.created_at.isoformat() + "Z",
        updated_at=row.updated_at.isoformat() + "Z",
        last_error=row.last_error,
        customer_id=(
            state.execution.customer_id if state and state.execution else (row.final or {}).get("customer_id")
        ),
        needed_documents=list(pending.get("needed", [])) if row.waiting_on == "await_docs" else [],
    )
    if who.is_officer:
        out.risk_rating, out.recommendation = row.risk_rating, row.recommendation
        out.approval = pending if row.waiting_on == "approve" else None
        out.degraded = list(state.degraded) if state else (row.final or {}).get("degraded")
        out.final = row.final
    return out


def create_app(
    settings: Settings | None = None, service: CaseService | None = None, tokens: TokenStore | None = None
) -> FastAPI:
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        if service is not None:
            app.state.service = service
            recovered = service.recover()
            yield
        else:
            with build_service_from_env(cfg) as svc:
                app.state.service = svc
                recovered = svc.recover()
                yield
        log.info("stopped", extra={"status": "stopped"})
        del recovered

    app = FastAPI(
        title="Client onboarding case workflow (synthetic data only)",
        version="0.3.0",
        lifespan=lifespan,
        docs_url=None if cfg.environment == "prod" else "/docs",
        redoc_url=None,
        openapi_url=None if cfg.environment == "prod" else "/openapi.json",
    )
    token_store = tokens or (TokenStore.from_json(cfg.tokens_json) if cfg.tokens_json else TokenStore.dev())
    if cfg.environment in {"qa", "prod"} and not cfg.tokens_json:
        raise RuntimeError("real tokens are required in qa and prod")
    app.state.settings = cfg
    app.state.tokens = token_store

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if not request.url.path.startswith("/ui/static"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def svc() -> CaseService:
        return app.state.service  # type: ignore[no-any-return]

    def principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
        token = (
            authorization.split(" ", 1)[1]
            if authorization and authorization.lower().startswith("bearer ")
            else None
        )
        who = token_store.authenticate(token)
        if who is None:
            raise HTTPException(
                status_code=401,
                detail="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return who

    def officer(who: Annotated[Principal, Depends(principal)]) -> Principal:
        if not who.is_officer:
            raise HTTPException(status_code=403, detail="officer role required")
        return who

    def visible(view_or_id: CaseView, who: Principal) -> None:
        if not who.is_officer and view_or_id.row.submitted_by != who.id:
            raise HTTPException(
                status_code=404, detail="case not found"
            )  # do not reveal other people's cases

    @app.exception_handler(CaseError)
    async def case_error(_: Request, exc: CaseError) -> JSONResponse:
        code = 400
        body: dict[str, Any] = {"detail": str(exc)}
        if isinstance(exc, CaseNotFound):
            code, body = 404, {"detail": "case not found"}
        elif isinstance(exc, Conflict):
            code = 409
        elif isinstance(exc, Forbidden):
            code = 403
        elif isinstance(exc, NotAllowed):
            code, body = 422, {"detail": "decision not allowed", "problems": exc.problems}
        elif isinstance(exc, Unavailable):
            code = 503
        return JSONResponse(body, status_code=code)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", extra={"path": request.url.path, "method": request.method}, exc_info=exc)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    async def read_files(files: dict[str, UploadFile | None]) -> list[UploadedDoc]:
        docs: list[UploadedDoc] = []
        for doc_type, f in files.items():
            if f is None or not f.filename:
                continue
            data = await f.read(cfg.doc_max_bytes + 1)
            if len(data) > cfg.doc_max_bytes:
                raise HTTPException(
                    status_code=413, detail=f"{doc_type} is larger than {cfg.doc_max_bytes} bytes"
                )
            if not data:
                raise HTTPException(status_code=422, detail=f"{doc_type} is empty")
            docs.append(UploadedDoc(doc_type=doc_type, content=data, filename=f.filename))  # type: ignore[arg-type]
        return docs

    # ------------------------------------------------------------------ endpoints
    @app.get("/healthz")
    def healthz(response: Response) -> dict[str, str]:
        try:
            with svc().store.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            response.status_code = 503
            return {"status": "database unavailable"}
        return {"status": "ok"}

    @app.post("/cases", response_model=CaseOut, status_code=201)
    async def create_case(
        who: Annotated[Principal, Depends(principal)],
        applicant: Annotated[str, Form(description="Applicant details as a JSON object")],
        id_document: Annotated[UploadFile | None, File()] = None,
        proof_of_address: Annotated[UploadFile | None, File()] = None,
    ) -> CaseOut:
        try:
            parsed = Applicant.model_validate(json.loads(applicant))
        except (json.JSONDecodeError, ValidationError) as exc:
            detail = (
                exc.errors(include_input=False, include_url=False)
                if isinstance(exc, ValidationError)
                else "applicant is not valid JSON"
            )
            raise HTTPException(status_code=422, detail=detail) from exc
        docs = await read_files({"id_document": id_document, "proof_of_address": proof_of_address})
        # The service is synchronous and can take a while (KYC, LLM, the graph): run it on a worker thread so the
        # event loop keeps serving /healthz and other requests (a blocked loop fails the liveness probe).
        view = await run_in_threadpool(svc().create_case, parsed, docs, who.id)
        return case_out(view, who)

    @app.get("/cases", response_model=list[CaseOut])
    def list_cases(who: Annotated[Principal, Depends(principal)]) -> list[CaseOut]:
        rows = svc().list_cases(None if who.is_officer else who.id)
        return [case_out(svc().get(r.case_id), who) for r in rows]

    @app.get("/cases/{case_id}", response_model=CaseOut)
    def get_case(case_id: str, who: Annotated[Principal, Depends(principal)]) -> CaseOut:
        view = svc().get(case_id)
        visible(view, who)
        return case_out(view, who)

    @app.post("/cases/{case_id}/decision", response_model=CaseOut)
    def decide(case_id: str, body: DecisionRequest, who: Annotated[Principal, Depends(officer)]) -> CaseOut:
        return case_out(svc().decide(case_id, body, who.id), who)

    @app.post("/cases/{case_id}/documents", response_model=CaseOut)
    async def add_documents(
        case_id: str,
        who: Annotated[Principal, Depends(principal)],
        interrupt_id: Annotated[str, Form()],
        id_document: Annotated[UploadFile | None, File()] = None,
        proof_of_address: Annotated[UploadFile | None, File()] = None,
    ) -> CaseOut:
        visible(svc().get(case_id), who)
        docs = await read_files({"id_document": id_document, "proof_of_address": proof_of_address})
        view = await run_in_threadpool(svc().add_documents, case_id, interrupt_id, docs, who.id)
        return case_out(view, who)

    @app.get("/cases/{case_id}/audit")
    def case_audit(case_id: str, who: Annotated[Principal, Depends(officer)]) -> list[dict[str, Any]]:
        svc().get(case_id)  # 404 if unknown
        return [audit_row(r) for r in svc().audit_trail(case_id)]

    @app.get("/audit/verify")
    def verify_audit(who: Annotated[Principal, Depends(officer)]) -> dict[str, Any]:
        v = svc().audit.verify()
        return {
            "ok": v.ok,
            "rows_checked": v.rows_checked,
            "first_broken_seq": v.first_broken_seq,
            "reason": v.reason,
            "head_seq": v.head_seq,
            "head_hash": v.head_hash,
        }

    from onboarding.api.ui import build_ui_router

    app.include_router(build_ui_router(app, token_store, cfg))
    return app


def audit_row(r: Any) -> dict[str, Any]:
    return {
        "seq": r.seq,
        "ts": r.ts.isoformat(),
        "case_id": r.case_id,
        "event_type": r.event_type,
        "node": r.node,
        "actor": r.actor,
        "payload": r.payload,
        "prompt_name": r.prompt_name,
        "prompt_version": r.prompt_version,
        "model_id": r.model_id,
        "prev_hash": r.prev_hash,
        "row_hash": r.row_hash,
    }


def get_app() -> FastAPI:
    """`uvicorn onboarding.api.main:get_app --factory`."""
    return create_app()


__all__ = ["CaseOut", "DocType", "create_app", "get_app"]
