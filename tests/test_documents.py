"""Retention and officer access to the original documents (DECISIONS D-24)."""

from __future__ import annotations

import json
from datetime import timedelta

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from onboarding.api.main import create_app
from onboarding.audit import MemoryAuditLog
from onboarding.config import ConfigError, Settings
from onboarding.decision import DecisionRequest
from onboarding.documents import (
    DocumentStoreError,
    LocalDocumentStore,
    MemoryDocumentStore,
    S3DocumentStore,
    sniff_content_type,
)
from onboarding.runner import OFFICER, SUBMITTER, build_offline_env
from onboarding.service import CaseNotFound, Conflict, Unavailable, UploadedDoc
from onboarding.store import utcnow
from tests.helpers import CASES

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PDF = b"%PDF-1.4\n%fake\n"
HTML = b"<html><script>alert(1)</script></html>"
BINARY = bytes(range(256))
SECRET_TEXT = "SPECIMEN scan of the passport, page 1"


# ------------------------------------------------------------------ type detection and identifiers
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (b"\xff\xd8\xff\xe0abc", "image/jpeg"),
        (PDF, "application/pdf"),
        (b"GIF89a....", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"plain text\nmore", "text/plain"),
        (BINARY, "application/octet-stream"),
        (b"", "application/octet-stream"),
    ],
)
def test_content_type_comes_from_the_bytes(data, expected):
    assert sniff_content_type(data) == expected


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b", "", "a b", ".hidden", "x" * 200, "a\\b", "a\x00b"])
def test_identifiers_with_path_characters_are_refused_by_every_store(bad, tmp_path):
    for store in (MemoryDocumentStore(), LocalDocumentStore(tmp_path)):
        with pytest.raises(DocumentStoreError):
            store.put(bad, "ref-1", b"x", "text/plain")
        with pytest.raises(DocumentStoreError):
            store.get("case-1", bad)
    with pytest.raises(DocumentStoreError):
        LocalDocumentStore(tmp_path).delete_case(bad)


# ------------------------------------------------------------------ the stores
def exercise(store):
    store.put("case-1", "ref-1", PNG, "image/png")
    store.put("case-1", "ref-2", b"hello", "text/plain")
    store.put("case-2", "ref-1", b"other", "text/plain")
    got = store.get("case-1", "ref-1")
    assert got is not None and got.content == PNG and got.content_type == "image/png"
    assert store.get("case-1", "missing") is None and store.get("nope", "ref-1") is None
    store.delete("case-1", "ref-2")
    assert store.get("case-1", "ref-2") is None and store.get("case-1", "ref-1") is not None
    assert store.delete_case("case-1") == 1
    assert store.get("case-1", "ref-1") is None and store.get("case-2", "ref-1") is not None
    assert store.delete_case("case-1") == 0


def test_memory_store():
    exercise(MemoryDocumentStore())


def test_local_store_and_its_layout(tmp_path):
    store = LocalDocumentStore(tmp_path)
    exercise(store)
    store.put("case-9", "ref-1", b"x", "text/plain")
    assert sorted(p.name for p in (tmp_path / "case-9").iterdir()) == ["ref-1", "ref-1.meta"]


@mock_aws
def test_s3_store_encrypts_objects_and_deletes_by_case():
    client = boto3.client("s3", region_name="ap-south-1")
    client.create_bucket(Bucket="docs", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
    store = S3DocumentStore(client, "docs", prefix="qa")
    exercise(store)
    store.put("case-3", "ref-1", PNG, "image/png")
    head = client.head_object(Bucket="docs", Key="qa/case-3/ref-1")
    assert head["ServerSideEncryption"] == "AES256" and head["ContentType"] == "application/octet-stream"
    assert head["Metadata"]["detected-type"] == "image/png"


@mock_aws
def test_s3_store_uses_a_kms_key_when_configured():
    client = boto3.client("s3", region_name="ap-south-1")
    client.create_bucket(Bucket="docs", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
    S3DocumentStore(client, "docs", kms_key_id="arn:aws:kms:ap-south-1:123456789012:key/abc").put(
        "c", "r", b"x", "text/plain"
    )
    assert client.head_object(Bucket="docs", Key="c/r")["ServerSideEncryption"] == "aws:kms"


@mock_aws
def test_s3_failures_become_store_errors_without_leaking_details():
    client = boto3.client("s3", region_name="ap-south-1")  # the bucket does not exist
    store = S3DocumentStore(client, "missing-bucket")
    with pytest.raises(DocumentStoreError) as e:
        store.put("c", "r", b"secret bytes", "text/plain")
    assert "secret bytes" not in str(e.value)
    with pytest.raises(DocumentStoreError):
        store.get("c", "r")


# ------------------------------------------------------------------ the service
def submit(env, case_id="clean_approve", content=None):
    case = CASES[case_id]
    docs = [
        UploadedDoc(d.doc_type, content if content is not None else d.content.encode())
        for d in case.documents
    ]
    return env.service.create_case(case.applicant, docs, SUBMITTER, case_id=case_id)


def test_originals_are_kept_when_a_case_is_created_and_the_state_holds_only_references():
    env = build_offline_env()
    view = submit(env)
    refs = view.state.documents
    assert all(d.stored and d.content_type == "text/plain" for d in refs)
    for d in refs:
        stored = env.service.documents.get("clean_approve", d.doc_ref)
        assert stored is not None and b"SPECIMEN" in stored.content
    assert "SYNTHETIC TEST DOCUMENT" not in view.state.model_dump_json()
    stored_events = [r for r in env.audit.rows() if r.event_type == "document_stored"]
    assert len(stored_events) == 2 and all(len(r.payload["sha256"]) == 64 for r in stored_events)
    assert all(
        "SPECIMEN" not in json.dumps(r.payload) for r in stored_events
    )  # hashes and sizes, never content


def test_the_original_file_name_is_never_kept():
    env = build_offline_env()
    case = CASES["clean_approve"]
    docs = [
        UploadedDoc(d.doc_type, d.content.encode(), filename="Jane_Doe_passport.pdf") for d in case.documents
    ]
    view = env.service.create_case(case.applicant, docs, SUBMITTER, case_id="c-names")
    blob = view.state.model_dump_json() + json.dumps([r.payload for r in env.audit.rows()])
    assert "Jane_Doe" not in blob and "passport.pdf" not in blob


def test_a_store_outage_degrades_the_case_but_does_not_stop_it():
    class Down(MemoryDocumentStore):
        def put(self, *a, **k):
            raise DocumentStoreError("down")

    env = build_offline_env(documents=Down())
    view = submit(env)
    assert view.row.status == "awaiting_officer" and "documents_not_retained" in view.state.degraded
    assert all(not d.stored for d in view.state.documents)
    assert any(r.event_type == "document_store_failed" for r in env.audit.rows())
    with pytest.raises(CaseNotFound, match="not retained"):
        env.service.get_document("clean_approve", view.state.documents[0].doc_ref, OFFICER)


def test_follow_up_documents_are_kept_too_and_a_refused_upload_leaves_nothing_behind():
    env = build_offline_env()
    view = submit(env, "missing_poa")
    view = env.service.decide(
        "missing_poa",
        DecisionRequest(
            interrupt_id=view.pending["interrupt_id"],
            action="request_more_info",
            note="Proof of address needed",
        ),
        OFFICER,
    )
    follow = [UploadedDoc(d.doc_type, d.content.encode()) for d in CASES["missing_poa"].followup_documents]
    store = env.service.documents
    before = {k for k in store._items}  # type: ignore[attr-defined]
    with pytest.raises(Conflict):
        env.service.add_documents("missing_poa", "wrong-id", follow, SUBMITTER)
    assert {k for k in store._items} == before  # type: ignore[attr-defined]
    view = env.service.add_documents("missing_poa", view.pending["interrupt_id"], follow, SUBMITTER)
    assert len(view.state.documents) == 2 and all(d.stored for d in view.state.documents)


def test_viewing_is_audited_first_and_returns_the_bytes():
    env = build_offline_env()
    view = submit(env, content=SECRET_TEXT.encode())
    ref = view.state.documents[0]
    doc = env.service.get_document("clean_approve", ref.doc_ref, OFFICER)
    assert doc.content == SECRET_TEXT.encode() and doc.inline and doc.sha256 == ref.sha256
    viewed = [r for r in env.audit.rows() if r.event_type == "document_viewed"]
    assert len(viewed) == 1 and viewed[0].actor == OFFICER and viewed[0].payload["doc_ref"] == ref.doc_ref
    assert SECRET_TEXT not in json.dumps(viewed[0].payload)


def test_nothing_is_shown_if_the_view_cannot_be_audited():
    class Flaky(MemoryAuditLog):
        def append(self, event):
            if event.event_type == "document_viewed":
                raise RuntimeError("audit down")
            return super().append(event)

    env = build_offline_env(audit=Flaky())
    view = submit(env)
    with pytest.raises(Unavailable, match="audit log is unavailable"):
        env.service.get_document("clean_approve", view.state.documents[0].doc_ref, OFFICER)


def test_a_changed_object_is_detected_and_withheld():
    env = build_offline_env()
    view = submit(env)
    ref = view.state.documents[0]
    env.service.documents.put("clean_approve", ref.doc_ref, b"tampered", "text/plain")
    with pytest.raises(Unavailable, match="does not match its recorded hash"):
        env.service.get_document("clean_approve", ref.doc_ref, OFFICER)
    assert any(r.event_type == "document_integrity_failed" for r in env.audit.rows())
    assert not any(r.event_type == "document_viewed" for r in env.audit.rows())


@pytest.mark.parametrize("doc_ref", ["unknown-ref", "../clean_approve", "clean_approve-x"])
def test_unknown_documents_are_not_found(doc_ref):
    env = build_offline_env()
    submit(env)
    with pytest.raises(CaseNotFound):
        env.service.get_document("clean_approve", doc_ref, OFFICER)
    with pytest.raises(CaseNotFound):
        env.service.get_document("no-such-case", doc_ref, OFFICER)


def test_a_document_of_another_case_cannot_be_reached_through_this_one():
    env = build_offline_env()
    a = submit(env, "clean_approve")
    submit(env, "true_sanctions_hit")
    with pytest.raises(CaseNotFound):
        env.service.get_document("true_sanctions_hit", a.state.documents[0].doc_ref, OFFICER)


def test_originals_are_deleted_with_the_checkpoint_after_the_retention_window():
    env = build_offline_env()
    view = submit(env)
    env.service.decide(
        "clean_approve", DecisionRequest(interrupt_id=view.pending["interrupt_id"], action="approve"), OFFICER
    )
    refs = view.state.documents
    env.service.store.project("clean_approve", updated_at=utcnow() - timedelta(days=45))
    assert env.service.purge_checkpoints(30) == 1
    assert all(env.service.documents.get("clean_approve", r.doc_ref) is None for r in refs)
    purged = [r for r in env.audit.rows() if r.event_type == "checkpoint_purged"][0]
    assert purged.payload["documents_deleted"] == 2
    with pytest.raises(CaseNotFound):
        env.service.get_document("clean_approve", refs[0].doc_ref, OFFICER)


def test_open_cases_keep_their_documents_when_others_are_purged():
    env = build_offline_env()
    keep = submit(env, "near_miss_dob_mismatch")
    env.service.store.project("near_miss_dob_mismatch", updated_at=utcnow() - timedelta(days=90))
    assert env.service.purge_checkpoints(30) == 0
    assert env.service.documents.get("near_miss_dob_mismatch", keep.state.documents[0].doc_ref) is not None


# ------------------------------------------------------------------ configuration
def test_qa_and_prod_require_the_s3_document_store():
    base = {"ONBOARDING_TOKENS": "[]", "SESSION_SECRET": "x"}
    with pytest.raises(ConfigError, match="DOCUMENT_STORE must be s3"):
        Settings.from_env({**base, "ENVIRONMENT": "qa"})
    with pytest.raises(ConfigError, match="DOCUMENT_BUCKET"):
        Settings.from_env({**base, "ENVIRONMENT": "qa", "DOCUMENT_STORE": "s3"})
    ok = Settings.from_env({**base, "ENVIRONMENT": "qa", "DOCUMENT_STORE": "s3", "DOCUMENT_BUCKET": "b"})
    assert ok.document_bucket == "b"
    with pytest.raises(ConfigError, match="none, local or s3"):
        Settings.from_env({"DOCUMENT_STORE": "ftp"})


# ------------------------------------------------------------------ the API and the pages
OFFICER_H = {"Authorization": "Bearer dev-officer-token"}
SUBMITTER_H = {"Authorization": "Bearer dev-submitter-token"}


@pytest.fixture
def api():
    env = build_offline_env()
    with TestClient(create_app(Settings(environment="test"), service=env.service)) as c:
        yield env, c
    env.close()


def create_via_api(c, files):
    case = CASES["clean_approve"]
    r = c.post(
        "/cases", headers=SUBMITTER_H, data={"applicant": case.applicant.model_dump_json()}, files=files
    )
    assert r.status_code == 201, r.text
    return r.json()["case_id"]


def test_officers_can_open_originals_and_each_view_is_audited(api):
    env, c = api
    cid = create_via_api(
        c,
        {
            "id_document": ("a.bin", PNG, "application/octet-stream"),
            "proof_of_address": ("b.txt", SECRET_TEXT.encode(), "text/plain"),
        },
    )
    docs = c.get(f"/cases/{cid}", headers=OFFICER_H).json()["documents"]
    assert {d["doc_type"] for d in docs} == {"id_document", "proof_of_address"} and all(
        d["stored"] for d in docs
    )
    assert '"content":' not in json.dumps(docs) and all(len(d["sha256"]) == 64 for d in docs)
    png_ref = next(d["doc_ref"] for d in docs if d["doc_type"] == "id_document")
    r = c.get(f"/cases/{cid}/documents/{png_ref}", headers=OFFICER_H)
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"] == 'inline; filename="id_document"'
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert (
        "default-src 'none'" in r.headers["content-security-policy"]
        and "sandbox" in r.headers["content-security-policy"]
    )
    viewed = [x for x in env.audit.rows() if x.event_type == "document_viewed"]
    assert len(viewed) == 1 and viewed[0].actor == "officer-1"


def test_submitters_cannot_open_documents_or_see_the_list(api):
    _, c = api
    cid = create_via_api(c, {"id_document": ("a.txt", b"hello", "text/plain")})
    ref = c.get(f"/cases/{cid}", headers=OFFICER_H).json()["documents"][0]["doc_ref"]
    assert c.get(f"/cases/{cid}/documents/{ref}", headers=SUBMITTER_H).status_code == 403
    assert c.get(f"/cases/{cid}/documents/{ref}").status_code == 401
    assert c.get(f"/cases/{cid}", headers=SUBMITTER_H).json()["documents"] is None


@pytest.mark.parametrize(
    ("payload", "served_as", "disposition"),
    [
        (HTML, "text/plain", "inline"),  # looks like HTML, is shown as plain text and never rendered
        (BINARY, "application/octet-stream", "attachment"),
        (PDF, "application/pdf", "inline"),
    ],
)
def test_untrusted_bytes_are_never_served_as_something_the_browser_would_run(
    api, payload, served_as, disposition
):
    _, c = api
    cid = create_via_api(
        c, {"id_document": ("evil.html", payload, "text/html")}
    )  # the client's claim is ignored
    ref = c.get(f"/cases/{cid}", headers=OFFICER_H).json()["documents"][0]["doc_ref"]
    r = c.get(f"/cases/{cid}/documents/{ref}", headers=OFFICER_H)
    assert r.headers["content-type"].split(";")[0] == served_as and r.headers[
        "content-disposition"
    ].startswith(disposition)
    assert r.headers["x-content-type-options"] == "nosniff" and "text/html" not in r.headers["content-type"]
    if served_as == "application/pdf":
        assert (
            "sandbox" not in r.headers["content-security-policy"]
        )  # a PDF viewer cannot run inside a sandbox
    else:
        assert "sandbox" in r.headers["content-security-policy"]


def test_unknown_documents_are_404_over_http(api):
    _, c = api
    cid = create_via_api(c, {"id_document": ("a.txt", b"hello", "text/plain")})
    assert c.get(f"/cases/{cid}/documents/nope", headers=OFFICER_H).status_code == 404
    assert c.get("/cases/no-case/documents/x", headers=OFFICER_H).status_code == 404


def test_the_officer_page_links_to_originals_and_the_submitter_page_does_not(api):
    import re

    env, c = api
    cid = create_via_api(c, {"id_document": ("a.txt", b"hello", "text/plain")})

    def login(token):
        page = c.get("/ui/login").text
        ft = re.search(r'name="form_token" value="([^"]+)"', page).group(1)
        c.post("/ui/login", data={"token": token, "form_token": ft}, follow_redirects=False)

    login("dev-officer-token")
    html = c.get(f"/ui/cases/{cid}").text
    ref = env.service.get(cid).state.documents[0].doc_ref
    assert (
        f'href="/ui/cases/{cid}/documents/{ref}"' in html
        and 'rel="noopener"' in html
        and "access is audited" in html
    )
    opened = c.get(f"/ui/cases/{cid}/documents/{ref}")
    assert opened.status_code == 200 and opened.content == b"hello"
    assert c.get(f"/ui/cases/{cid}/documents/unknown").status_code == 404
    c.cookies.clear()
    login("dev-submitter-token")
    assert "Open original" not in c.get(f"/ui/cases/{cid}").text
    assert c.get(f"/ui/cases/{cid}/documents/{ref}").status_code == 403
