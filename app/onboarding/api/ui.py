"""Officer and submitter pages: server-rendered, no JavaScript, strict Content Security Policy.

Design rules
- Every value is rendered by Jinja2 with autoescaping on; nothing is marked safe.
- The CSP allows only same-origin stylesheets and same-origin form posts: no scripts, no inline styles, no frames.
- Sessions are a signed, HttpOnly, SameSite=Strict cookie. Every state-changing form carries a CSRF token.
- Submitters never see screening results or the risk assessment (that could tip off an applicant); officers see all of it.
- The AI-drafted text on the page is always labelled as advisory, and the rule outputs sit next to it.
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError
from starlette.datastructures import (
    UploadFile,  # the base class: form parsing returns this, not FastAPI's subclass
)

from onboarding.auth import Principal, TokenStore
from onboarding.config import Settings
from onboarding.decision import DecisionRequest
from onboarding.models import Applicant
from onboarding.service import (
    CaseError,
    CaseNotFound,
    CaseService,
    Conflict,
    Forbidden,
    NotAllowed,
    Unavailable,
    UploadedDoc,
)

HERE = Path(__file__).parent
COOKIE = "onb_session"
SESSION_SECONDS = 8 * 3600
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
UI_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}
STATUS_LABELS = {
    "running": "Processing",
    "resuming": "Processing",
    "awaiting_officer": "Awaiting officer",
    "awaiting_documents": "Awaiting documents",
    "approved": "Approved",
    "rejected": "Rejected",
    "failed": "Failed",
}


def build_ui_router(app: FastAPI, tokens: TokenStore, cfg: Settings) -> APIRouter:
    router = APIRouter(prefix="/ui", include_in_schema=False)
    env = Environment(
        loader=FileSystemLoader(str(HERE / "templates")), autoescape=True
    )  # never switch autoescape off
    templates = Jinja2Templates(env=env)
    templates.env.globals["status_label"] = lambda s: STATUS_LABELS.get(s, s)
    serializer = URLSafeTimedSerializer(cfg.session_secret or secrets.token_hex(32), salt="onboarding-ui")

    def service() -> CaseService:
        return app.state.service  # type: ignore[no-any-return]

    # ---------------------------------------------------------------- helpers
    def session(request: Request) -> dict[str, str] | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            data = serializer.loads(raw, max_age=SESSION_SECONDS)
        except BadSignature:
            return None
        return data if isinstance(data, dict) and {"id", "role", "csrf"} <= data.keys() else None

    def who_from(sess: dict[str, str]) -> Principal:
        return Principal(sess["id"], sess["role"])  # type: ignore[arg-type]

    def page(
        request: Request,
        name: str,
        ctx: dict[str, Any],
        status: int = 200,
        sess: dict[str, str] | None = None,
    ) -> HTMLResponse:
        full = {
            "request": request,
            "who": who_from(sess) if sess else None,
            "csrf": sess["csrf"] if sess else "",
            **ctx,
        }
        resp = templates.TemplateResponse(request, name, full, status_code=status)
        resp.headers.update(UI_HEADERS)
        return resp

    def redirect(url: str) -> RedirectResponse:
        resp = RedirectResponse(url, status_code=303)
        resp.headers.update(UI_HEADERS)
        return resp

    def csrf_ok(sess: dict[str, str], token: str) -> bool:
        return hmac.compare_digest(sess["csrf"], token or "")

    def login_token() -> str:
        return serializer.dumps("login-form")

    def login_page(request: Request, error: str | None = None, status: int = 200) -> HTMLResponse:
        return page(request, "login.html", {"error": error, "form_token": login_token()}, status)

    # ---------------------------------------------------------------- static
    @router.get("/static/ui.css")
    def stylesheet() -> FileResponse:
        resp = FileResponse(HERE / "static" / "ui.css", media_type="text/css")
        resp.headers.update({**UI_HEADERS, "Cache-Control": "public, max-age=300"})
        return resp

    # ---------------------------------------------------------------- login
    @router.get("")
    def home(request: Request) -> Response:
        return redirect("/ui/cases" if session(request) else "/ui/login")

    @router.get("/login")
    def login_form(request: Request) -> Response:
        return login_page(request)

    @router.post("/login")
    def login(request: Request, token: str = Form(""), form_token: str = Form("")) -> Response:
        try:
            if serializer.loads(form_token, max_age=3600) != "login-form":
                raise BadSignature("wrong form")
        except BadSignature:
            return login_page(request, "The form expired. Try again.", 400)
        who = tokens.authenticate(token.strip())
        if who is None:
            return login_page(request, "That token was not recognised.", 401)
        resp = redirect("/ui/cases")
        resp.set_cookie(
            COOKIE,
            serializer.dumps({"id": who.id, "role": who.role, "csrf": secrets.token_urlsafe(24)}),
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=cfg.secure_cookies,
            path="/ui",
        )
        return resp

    @router.post("/logout")
    def logout(request: Request, csrf: str = Form("")) -> Response:
        sess = session(request)
        if sess and not csrf_ok(sess, csrf):
            return page(request, "error.html", {"message": "Invalid form token."}, 403, sess)
        resp = redirect("/ui/login")
        resp.delete_cookie(COOKIE, path="/ui")
        return resp

    # ---------------------------------------------------------------- queue and submit
    @router.get("/cases")
    def queue(request: Request) -> Response:
        sess = session(request)
        if not sess:
            return redirect("/ui/login")
        who = who_from(sess)
        rows = service().list_cases(None if who.is_officer else who.id)
        order = {"awaiting_officer": 0, "awaiting_documents": 1, "running": 2, "resuming": 2}
        rows.sort(key=lambda r: (order.get(r.status, 9), -r.created_at.timestamp()))
        return page(request, "queue.html", {"rows": rows}, sess=sess)

    @router.get("/new")
    def new_form(request: Request) -> Response:
        sess = session(request)
        return (
            page(request, "new.html", {"error": None, "values": {}}, sess=sess)
            if sess
            else redirect("/ui/login")
        )

    @router.post("/new")
    async def new_submit(
        request: Request,
        csrf: str = Form(""),
        name: str = Form(""),
        aliases: str = Form(""),
        dob: str = Form(""),
        nationality: str = Form(""),
        residence_country: str = Form(""),
        occupation: str = Form(""),
    ) -> Response:
        sess = session(request)
        if not sess:
            return redirect("/ui/login")
        if not csrf_ok(sess, csrf):
            return page(request, "error.html", {"message": "Invalid form token."}, 403, sess)
        values = {
            "name": name,
            "aliases": aliases,
            "dob": dob,
            "nationality": nationality,
            "residence_country": residence_country,
            "occupation": occupation,
        }
        form = await request.form()
        try:
            applicant = Applicant(
                name=name.strip(),
                aliases=[a.strip() for a in aliases.split(",") if a.strip()],
                dob=dob.strip(),
                nationality=nationality.strip(),
                residence_country=residence_country.strip(),
                occupation=occupation.strip(),
            )
            docs = await uploads(form, cfg.doc_max_bytes)
        except (ValidationError, ValueError) as exc:
            msg = "Check the applicant details and files." if isinstance(exc, ValidationError) else str(exc)
            return page(request, "new.html", {"error": msg, "values": values}, 422, sess)
        view = service().create_case(applicant, docs, submitted_by=sess["id"])
        return redirect(f"/ui/cases/{view.row.case_id}")

    # ---------------------------------------------------------------- one case
    def case_page(
        request: Request,
        case_id: str,
        sess: dict[str, str],
        error: str | None = None,
        problems: list[str] | None = None,
        status: int = 200,
        draft: dict[str, Any] | None = None,
    ) -> Response:
        try:
            view = service().get(case_id)
        except CaseNotFound:
            return page(request, "error.html", {"message": "Case not found."}, 404, sess)
        who = who_from(sess)
        if not who.is_officer and view.row.submitted_by != who.id:
            return page(request, "error.html", {"message": "Case not found."}, 404, sess)
        trail = service().audit_trail(case_id) if who.is_officer else []
        ctx = {
            "view": view,
            "row": view.row,
            "state": view.state,
            "pending": view.pending,
            "trail": trail,
            "error": error,
            "draft": draft or {},
            "problems": problems or [],
            "is_own": view.row.submitted_by == who.id,
        }
        return page(request, "case.html", ctx, status, sess)

    @router.get("/cases/{case_id}")
    def case_view(request: Request, case_id: str) -> Response:
        sess = session(request)
        return case_page(request, case_id, sess) if sess else redirect("/ui/login")

    @router.post("/cases/{case_id}/decision")
    async def decision(request: Request, case_id: str) -> Response:
        sess = session(request)
        if not sess:
            return redirect("/ui/login")
        form = await request.form()
        if not csrf_ok(sess, str(form.get("csrf", ""))):
            return page(request, "error.html", {"message": "Invalid form token."}, 403, sess)
        who = who_from(sess)
        if not who.is_officer:
            return case_page(request, case_id, sess, "Only officers can decide.", status=403)
        dispositions: dict[str, Any] = {
            k[len("disp__") :]: str(v) for k, v in form.multi_items() if k.startswith("disp__") and v
        }
        draft = {
            "action": str(form.get("action", "")),
            "note": str(form.get("note", "")),
            "disp": dispositions,
        }
        try:
            req = DecisionRequest(
                interrupt_id=str(form.get("interrupt_id", "")),
                action=str(form.get("action", "")),  # type: ignore[arg-type]
                note=str(form.get("note", "")).strip() or None,
                dispositions=dispositions,
            )  # type: ignore[arg-type]
        except ValidationError:
            return case_page(
                request, case_id, sess, "Choose an action and a disposition for each hit.", status=422
            )
        try:
            service().decide(case_id, req, who.id)
        except NotAllowed as exc:
            return case_page(
                request, case_id, sess, "That decision is not allowed.", exc.problems, 422, draft=draft
            )
        except Forbidden as exc:
            return case_page(request, case_id, sess, str(exc), status=403)
        except Conflict as exc:
            return case_page(
                request, case_id, sess, f"{exc}. Reload the page to see the current state.", status=409
            )
        except Unavailable as exc:
            return case_page(request, case_id, sess, str(exc), status=503)
        except CaseError as exc:
            return case_page(request, case_id, sess, str(exc), status=400)
        return redirect(f"/ui/cases/{case_id}")

    @router.post("/cases/{case_id}/documents")
    async def documents(request: Request, case_id: str) -> Response:
        sess = session(request)
        if not sess:
            return redirect("/ui/login")
        form = await request.form()
        if not csrf_ok(sess, str(form.get("csrf", ""))):
            return page(request, "error.html", {"message": "Invalid form token."}, 403, sess)
        try:
            docs = await uploads(form, cfg.doc_max_bytes)
            service().add_documents(case_id, str(form.get("interrupt_id", "")), docs, sess["id"])
        except ValueError as exc:
            return case_page(request, case_id, sess, str(exc), status=422)
        except NotAllowed as exc:
            return case_page(request, case_id, sess, "No documents were provided.", exc.problems, 422)
        except Conflict as exc:
            return case_page(
                request, case_id, sess, f"{exc}. Reload the page to see the current state.", status=409
            )
        except CaseError as exc:
            return case_page(request, case_id, sess, str(exc), status=400)
        return redirect(f"/ui/cases/{case_id}")

    return router


async def uploads(form: Any, limit: int) -> list[UploadedDoc]:
    docs: list[UploadedDoc] = []
    for doc_type in ("id_document", "proof_of_address"):
        f = form.get(doc_type)
        if not isinstance(f, UploadFile) or not f.filename:
            continue
        data = await f.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"The {doc_type.replace('_', ' ')} is larger than {limit} bytes.")
        if not data:
            raise ValueError(f"The {doc_type.replace('_', ' ')} is empty.")
        docs.append(UploadedDoc(doc_type=doc_type, content=data, filename=f.filename))  # type: ignore[arg-type]
    return docs
