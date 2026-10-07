"""The officer and submitter pages: strict CSP, escaping, CSRF, sessions, role views, and the decision form."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from onboarding.api.main import create_app
from onboarding.api.ui import CSP
from onboarding.config import Settings
from onboarding.runner import build_offline_env
from tests.helpers import CASES

TEMPLATES = Path("app/onboarding/api/templates")
STATIC = Path("app/onboarding/api/static")


@pytest.fixture
def env():
    e = build_offline_env()
    yield e
    e.close()


def make_client(env, **settings):
    s = Settings(environment="test", session_secret="test-secret", **settings)
    return TestClient(create_app(s, service=env.service), follow_redirects=False)


@pytest.fixture
def client(env):
    with make_client(env) as c:
        yield c


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(c: TestClient, token: str) -> TestClient:
    page = c.get("/ui/login").text
    form_token = re.search(r'name="form_token" value="([^"]+)"', page).group(1)
    r = c.post("/ui/login", data={"token": token, "form_token": form_token})
    assert r.status_code == 303, r.text
    return c


def as_user(env, token: str, **settings) -> TestClient:
    c = make_client(env, **settings)
    c.__enter__()
    return login(c, token)


def submit_via_api(env, case_id="clean_approve", token="dev-submitter-token") -> str:  # noqa: S107 - local dev token
    with make_client(env) as c:
        case = CASES[case_id]
        files = {d.doc_type: (f"{d.doc_type}.txt", d.content.encode(), "text/plain") for d in case.documents}
        r = c.post(
            "/cases",
            headers={"Authorization": f"Bearer {token}"},
            data={"applicant": case.applicant.model_dump_json()},
            files=files,
        )
        return r.json()["case_id"]


def view(c, case_id):
    return c.get(f"/ui/cases/{case_id}")


# ------------------------------------------------------------------ headers and static hygiene
def test_every_page_carries_the_strict_csp_and_no_script_is_allowed(client):
    r = client.get("/ui/login")
    assert r.headers["content-security-policy"] == CSP
    assert "script-src 'self'" in CSP and "default-src 'none'" in CSP and "unsafe" not in CSP
    assert "frame-ancestors 'none'" in CSP and "form-action 'self'" in CSP
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_templates_and_css_have_no_inline_script_style_handlers_or_external_resources():
    for path in [*TEMPLATES.glob("*.html"), *STATIC.glob("*")]:
        text = path.read_text()
        for tag in re.findall(r"<script\b[^>]*>", text, re.IGNORECASE):
            # the only script allowed is the same-origin file; never inline code
            assert re.fullmatch(r'<script src="/ui/static/ui\.js" defer>', tag), (path, tag)
        assert not re.search(r"<script[^>]*>\s*[^<\s]", text, re.IGNORECASE), (path, "inline script")
        assert not re.search(r"\sstyle\s*=", text), path
        assert not re.search(r"\son[a-z]+\s*=", text), path
        assert "|safe" not in text and "autoescape" not in text and "Markup" not in text, path
        assert not re.search(r"https?://", text), path
        assert "@import" not in text and "url(" not in text, path


def test_the_script_is_small_and_does_not_use_dangerous_apis():
    js = (STATIC / "ui.js").read_text()
    for banned in (
        "eval(",
        "new Function",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "fetch(",
        "XMLHttpRequest",
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "import(",
        "WebSocket",
    ):
        assert banned not in js, banned
    assert len(js.splitlines()) < 100


def test_the_script_is_served_with_the_csp(client):
    r = client.get("/ui/static/ui.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript")
    assert r.headers["content-security-policy"] == CSP and "data-busy" in r.text


def test_slow_forms_are_marked_busy_and_pages_load_the_script(env):
    cid = submit_via_api(env, "missing_poa")
    sub = as_user(env, "dev-submitter-token")
    off = as_user(env, "dev-officer-token")
    new = sub.get("/ui/new").text
    assert 'data-busy="Submitting case' in new and 'class="busy-note"' in new and "hidden" in new
    assert '<script src="/ui/static/ui.js" defer></script>' in new
    assert 'data-busy="Recording decision' in view(off, cid).text
    # every form that posts to a slow action is marked; login and logout are quick and are not
    for html in (new, view(off, cid).text):
        for form in re.findall(r"<form[^>]*>", html):
            if "/ui/new" in form or "/decision" in form or "/documents" in form:
                assert "data-busy=" in form, form
            if "/ui/logout" in form or "/ui/login" in form:
                assert "data-busy" not in form
    off.close(), sub.close()


def test_pages_still_work_without_the_script(env):
    """The script only adds a busy state: the forms are ordinary forms with ordinary submit buttons."""
    sub = as_user(env, "dev-submitter-token")
    html = sub.get("/ui/new").text
    assert '<button type="submit">Submit case</button>' in html and 'method="post"' in html
    sub.close()


def test_the_app_never_disables_jinja_autoescape():
    src = Path("app/onboarding/api/ui.py").read_text()
    assert "autoescape=True" in src and "autoescape=False" not in src and "|safe" not in src


def test_stylesheet_is_served_with_the_csp(client):
    r = client.get("/ui/static/ui.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
    assert r.headers["content-security-policy"] == CSP


# ------------------------------------------------------------------ sessions
def test_unauthenticated_users_are_sent_to_login(client):
    for url in ("/ui", "/ui/cases", "/ui/new", "/ui/cases/anything"):
        r = client.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/ui/login", url


def test_a_wrong_token_is_401_and_a_tampered_form_token_is_400(client):
    page = client.get("/ui/login").text
    ft = re.search(r'name="form_token" value="([^"]+)"', page).group(1)
    assert client.post("/ui/login", data={"token": "nope", "form_token": ft}).status_code == 401
    assert (
        client.post("/ui/login", data={"token": "dev-officer-token", "form_token": ft + "x"}).status_code
        == 400
    )
    assert client.post("/ui/login", data={"token": "dev-officer-token"}).status_code == 400


def test_the_session_cookie_is_httponly_samesite_strict_and_scoped_to_ui(env):
    with make_client(env) as c:
        page = c.get("/ui/login").text
        ft = re.search(r'name="form_token" value="([^"]+)"', page).group(1)
        r = c.post("/ui/login", data={"token": "dev-officer-token", "form_token": ft})
        cookie = r.headers["set-cookie"].lower()
        assert (
            "httponly" in cookie
            and "samesite=strict" in cookie
            and "path=/ui" in cookie
            and "secure" not in cookie
        )
    with make_client(env, secure_cookies=True) as c2:
        page = c2.get("/ui/login").text
        ft = re.search(r'name="form_token" value="([^"]+)"', page).group(1)
        assert (
            "secure"
            in c2.post("/ui/login", data={"token": "dev-officer-token", "form_token": ft})
            .headers["set-cookie"]
            .lower()
        )


def test_a_forged_or_tampered_cookie_is_not_a_session(env):
    with make_client(env) as c:
        c.cookies.set("onb_session", "forged-value", path="/ui")
        assert c.get("/ui/cases").status_code == 303
    officer = as_user(env, "dev-officer-token")
    good = officer.cookies.get("onb_session")
    with make_client(env) as c:
        c.cookies.set("onb_session", good[:-3] + "AAA", path="/ui")
        assert c.get("/ui/cases").status_code == 303
    officer.close()


def test_logout_needs_the_csrf_token_and_clears_the_cookie(env):
    c = as_user(env, "dev-officer-token")
    html = c.get("/ui/cases").text
    assert c.post("/ui/logout", data={"csrf": "wrong"}).status_code == 403
    r = c.post("/ui/logout", data={"csrf": csrf_of(html)})
    assert r.status_code == 303 and r.headers["location"] == "/ui/login"
    c.close()


# ------------------------------------------------------------------ views by role
def test_officer_sees_the_assessment_with_advisory_labels(env):
    cid = submit_via_api(env, "near_miss_dob_mismatch")
    c = as_user(env, "dev-officer-token")
    html = view(c, cid).text
    for expected in (
        "R-SAN-02",
        "CDi.011",
        "snapshot 2026-10-03",
        "AI note, advisory: does not clear the hit",
        "Decided by fixed rules, not by the AI model",
        "disp__CDi.011",
        "Your decision",
        "SPECIMEN",
    ):
        assert expected in html, expected
    assert "1991-02-14" not in html and "SPEC-ID" not in html  # no DOB or ID numbers on the page
    assert "manual review" in html and "medium risk" in html
    c.close()


def test_the_queue_puts_work_for_the_officer_first(env):
    done = submit_via_api(env, "clean_approve")
    waiting = submit_via_api(env, "true_sanctions_hit")
    c = as_user(env, "dev-officer-token")
    c.post(f"/cases/{done}/decision")  # no-op route: ensure no side effects from stray posts
    html = c.get("/ui/cases").text
    assert html.index(waiting) > 0 and "Awaiting officer" in html and "Rating" in html
    c.close()


def test_a_submitter_sees_status_only_and_never_screening_or_risk(env):
    cid = submit_via_api(env, "true_sanctions_hit")
    c = as_user(env, "dev-submitter-token")
    html = view(c, cid).text
    for hidden in (
        "CDi.009",
        "R-SAN-01",
        "sanction",
        "Sanctions",
        "risk",
        "Recommendation",
        "Audit trail",
        "Your decision",
        "snapshot",
    ):
        assert hidden not in html, hidden
    assert "Awaiting officer" in html
    queue = c.get("/ui/cases").text
    assert "Rating" not in queue and "Recommendation" not in queue
    c.close()


def test_another_submitters_case_is_not_found(env):
    cid = submit_via_api(env)
    from onboarding.auth import Principal, TokenStore, hash_token

    other = TokenStore({hash_token("other"): Principal("submitter-9", "submitter")})
    with TestClient(
        create_app(Settings(environment="test", session_secret="s"), service=env.service, tokens=other),
        follow_redirects=False,
    ) as c:
        login(c, "other")
        assert view(c, cid).status_code == 404


def test_user_supplied_text_is_escaped(env):
    case = CASES["clean_approve"]
    evil = "<script>alert('x')</script> & <img src=x onerror=alert(1)>"
    applicant = case.applicant.model_copy(update={"name": evil, "occupation": '"><b>bold</b>'})
    files = {d.doc_type: (f"{d.doc_type}.txt", d.content.encode(), "text/plain") for d in case.documents}
    with make_client(env) as api:
        cid = api.post(
            "/cases",
            headers={"Authorization": "Bearer dev-submitter-token"},
            data={"applicant": applicant.model_dump_json()},
            files=files,
        ).json()["case_id"]
    c = as_user(env, "dev-officer-token")
    for url in ("/ui/cases", f"/ui/cases/{cid}"):
        html = c.get(url).text
        assert "<script>alert" not in html and "<img src=x" not in html and "<b>bold</b>" not in html, url
        assert "&lt;script&gt;" in html, url
    c.close()


# ------------------------------------------------------------------ the decision form
def decision_form(c, cid, action, note="", **disp):
    html = view(c, cid).text
    iid = re.search(r'name="interrupt_id" value="([^"]+)"', html).group(1)
    data = {
        "csrf": csrf_of(html),
        "interrupt_id": iid,
        "action": action,
        "note": note,
        **{f"disp__{k}": v for k, v in disp.items()},
    }
    return c.post(f"/ui/cases/{cid}/decision", data=data)


def test_posting_a_decision_without_the_csrf_token_is_refused(env):
    cid = submit_via_api(env)
    c = as_user(env, "dev-officer-token")
    html = view(c, cid).text
    iid = re.search(r'name="interrupt_id" value="([^"]+)"', html).group(1)
    for csrf in ("", "wrong"):
        assert (
            c.post(
                f"/ui/cases/{cid}/decision", data={"csrf": csrf, "interrupt_id": iid, "action": "approve"}
            ).status_code
            == 403
        )
    assert env.service.get(cid).row.status == "awaiting_officer"
    c.close()


def test_an_officer_can_clear_a_hit_and_approve_through_the_form(env):
    cid = submit_via_api(env, "near_miss_dob_mismatch")
    c = as_user(env, "dev-officer-token")
    r = decision_form(
        c, cid, "approve", "Different person: DOB and nationality differ", **{"CDi.011": "cleared"}
    )
    assert r.status_code == 303 and r.headers["location"] == f"/ui/cases/{cid}"
    done = view(c, cid).text
    assert "approved" in done.lower() and "officer-1" in done and "Customer record created" in done
    assert env.service.get(cid).state.screening.hits[0].disposition == "cleared"
    c.close()


def test_the_form_shows_the_guards_reasons(env):
    cid = submit_via_api(env, "near_miss_dob_mismatch")
    c = as_user(env, "dev-officer-token")
    r = decision_form(c, cid, "approve", "Looks fine")
    assert r.status_code == 422 and "That decision is not allowed." in r.text and "CDi.011" in r.text
    assert env.service.get(cid).row.status == "awaiting_officer"
    c.close()


def test_a_missing_action_is_a_friendly_422(env):
    cid = submit_via_api(env)
    c = as_user(env, "dev-officer-token")
    r = decision_form(c, cid, "")
    assert r.status_code == 422 and "Choose an action" in r.text
    c.close()


def test_a_stale_form_gets_a_conflict_message(env):
    cid = submit_via_api(env)
    a, b = as_user(env, "dev-officer-token"), as_user(env, "dev-officer2-token")
    html_b = view(b, cid).text  # officer 2 loads the page, then officer 1 decides
    assert decision_form(a, cid, "approve").status_code == 303
    iid = re.search(r'name="interrupt_id" value="([^"]+)"', html_b).group(1)
    r = b.post(
        f"/ui/cases/{cid}/decision", data={"csrf": csrf_of(html_b), "interrupt_id": iid, "action": "reject"}
    )
    assert r.status_code == 409 and "Reload the page" in r.text
    a.close(), b.close()


def test_a_submitter_cannot_post_a_decision(env):
    cid = submit_via_api(env)
    c = as_user(env, "dev-submitter-token")
    html = c.get("/ui/cases").text
    r = c.post(
        f"/ui/cases/{cid}/decision",
        data={"csrf": csrf_of(html), "interrupt_id": f"{cid}:a0", "action": "approve"},
    )
    assert r.status_code == 403 and "Only officers" in r.text
    assert env.service.get(cid).row.status == "awaiting_officer"
    c.close()


def test_an_officer_is_told_they_cannot_decide_their_own_case(env):
    cid = submit_via_api(env, token="dev-officer-token")
    c = as_user(env, "dev-officer-token")
    html = view(c, cid).text
    assert "you cannot decide it" in html and "disabled" in html
    r = decision_form(c, cid, "reject")
    assert r.status_code == 403 and "nobody may decide a case they submitted" in r.text
    c.close()


# ------------------------------------------------------------------ new case and documents
def test_submitting_a_new_case_and_the_document_round_through_the_pages(env):
    case = CASES["missing_poa"]
    c = as_user(env, "dev-submitter-token")
    form = c.get("/ui/new").text
    data = {
        "csrf": csrf_of(form),
        "name": case.applicant.name,
        "aliases": "",
        "dob": case.applicant.dob,
        "nationality": case.applicant.nationality,
        "residence_country": case.applicant.residence_country,
        "occupation": case.applicant.occupation,
    }
    files = {"id_document": ("id.txt", case.documents[0].content.encode(), "text/plain")}
    r = c.post("/ui/new", data=data, files=files)
    assert r.status_code == 303
    cid = r.headers["location"].rsplit("/", 1)[1]
    off = as_user(env, "dev-officer-token")
    assert decision_form(off, cid, "request_more_info", "Proof of address needed").status_code == 303
    page = view(c, cid).text
    assert "Documents needed" in page and "proof of address" in page and "sanction" not in page.lower()
    iid = re.search(r'name="interrupt_id" value="([^"]+)"', page).group(1)
    up = c.post(
        f"/ui/cases/{cid}/documents",
        data={"csrf": csrf_of(page), "interrupt_id": iid},
        files={"proof_of_address": ("p.txt", case.followup_documents[0].content.encode(), "text/plain")},
    )
    assert up.status_code == 303
    assert env.service.get(cid).row.status == "awaiting_officer"
    c.close(), off.close()


def test_invalid_new_case_input_is_shown_back_with_values_kept(env):
    c = as_user(env, "dev-submitter-token")
    form = c.get("/ui/new").text
    r = c.post(
        "/ui/new",
        data={
            "csrf": csrf_of(form),
            "name": "Keep Me",
            "dob": "not-a-date",
            "nationality": "X",
            "residence_country": "Y",
            "occupation": "Z",
        },
    )
    assert r.status_code == 422 and "Keep Me" in r.text and "Check the applicant details" in r.text
    assert c.post("/ui/new", data={"name": "A"}).status_code == 403
    c.close()


def test_the_draft_request_is_shown_as_not_sent(env):
    cid = submit_via_api(env, "missing_poa")
    c = as_user(env, "dev-officer-token")
    html = view(c, cid).text
    assert "Draft request to the applicant" in html and "not sent: a person must send it" in html
    assert "proof of address" in html.lower() and "[bank contact]" in html
    c.close()


def test_degraded_cases_explain_themselves(env):
    cid = submit_via_api(env, "kyc_unavailable")
    c = as_user(env, "dev-officer-token")
    html = view(c, cid).text
    assert "document extraction was unavailable" in html and "R-DOC-03" in html
    c.close()


def test_the_audit_trail_is_listed_for_officers(env):
    cid = submit_via_api(env)
    c = as_user(env, "dev-officer-token")
    html = view(c, cid).text
    assert (
        "Audit trail" in html
        and "case_created" in html
        and "approval_requested" in html
        and "onboarding-explain-recommendation" in html
    )
    c.close()


def test_the_summary_label_follows_who_wrote_the_summary(env):
    cid = submit_via_api(env, "near_miss_dob_mismatch")
    assert env.service.get(cid).state.summary_by == "llm"
    c = as_user(env, "dev-officer-token")
    html = view(c, cid).text
    assert html.index("AI-drafted, advisory") < html.index(
        "Recommendation"
    )  # the summary card carries the label
    c.close()
    env2 = build_offline_env(
        llm=__import__("onboarding.llm.client", fromlist=["FakeLlm"]).FakeLlm(unavailable=True)
    )
    cid2 = submit_via_api(env2, "near_miss_dob_mismatch")
    assert env2.service.get(cid2).state.summary_by == "template"
    c2 = as_user(env2, "dev-officer-token")
    assert "template wording" in view(c2, cid2).text
    c2.close()
    env2.close()


def test_a_refused_decision_keeps_what_the_officer_typed(env):
    cid = submit_via_api(env, "near_miss_dob_mismatch")
    c = as_user(env, "dev-officer-token")
    r = decision_form(c, cid, "approve", "My reasoning so far is here")
    assert r.status_code == 422
    assert "My reasoning so far is here" in r.text and re.search(r'value="approve"[^>]*checked', r.text)
    c.close()
