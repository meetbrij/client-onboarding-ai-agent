"""The same service on real Postgres: LangGraph's PostgresSaver, advisory locks, the audit trigger and the
database roles. Each "process" is a fresh set of connections, buffers and fakes, so resuming in a new one is
a genuine restart. Needs TEST_POSTGRES_ADMIN_URL (see conftest)."""

from __future__ import annotations

import threading
from contextlib import ExitStack
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from onboarding import db
from onboarding.audit.postgres import PostgresAuditLog
from onboarding.decision import DecisionRequest
from onboarding.llm.client import FakeLlm
from onboarding.locks import PostgresAdvisoryLocks
from onboarding.runner import OFFICER, SUBMITTER, build_deps
from onboarding.service import CaseService, Conflict, UploadedDoc
from onboarding.store import CaseStore, utcnow
from tests.helpers import CASES


@pytest.fixture
def pg(scratch_db):
    owner_url = scratch_db.owner.url.render_as_string(hide_password=False)
    db.migrate(owner_url, scratch_db.app_role)
    return scratch_db


class Process:
    """One running instance of the service: its own connections, thread pool, buffer and fake dependencies."""

    def __init__(self, pg, llm: FakeLlm | None = None) -> None:
        self.stack = ExitStack()
        self.engine = create_engine(pg.app_url, pool_size=10)
        self.audit = PostgresAuditLog(self.engine)
        saver = self.stack.enter_context(db.postgres_checkpointer(pg.app_url))
        self.deps = build_deps(self.audit, llm or FakeLlm(), self.stack)
        self.service = CaseService(
            self.deps, CaseStore(self.engine), saver, PostgresAdvisoryLocks(self.engine)
        )

    def stop(self) -> None:
        self.stack.close()
        self.engine.dispose()

    def events(self, kind: str, case_id: str | None = None):
        return [r for r in self.audit.rows(case_id) if r.event_type == kind]


def docs(case_id):
    return [UploadedDoc(d.doc_type, d.content.encode()) for d in CASES[case_id].documents]


def submit(proc: Process, case_id: str):
    return proc.service.create_case(CASES[case_id].applicant, docs(case_id), SUBMITTER, case_id=case_id)


def approve(view, note=None, **disp):
    return DecisionRequest(
        interrupt_id=view.pending["interrupt_id"], action="approve", note=note, dispositions=disp
    )


def test_migrate_is_idempotent_and_the_app_role_is_not_an_owner(pg):
    db.migrate(pg.owner.url.render_as_string(hide_password=False), pg.app_role)
    app = create_engine(pg.app_url)
    with pytest.raises(ProgrammingError, match="must be owner"), app.begin() as c:
        c.execute(text("DROP TABLE cases"))
    with pytest.raises(ProgrammingError, match="permission denied"), app.begin() as c:
        c.execute(text("DELETE FROM audit_log"))
    app.dispose()


def test_a_case_survives_a_restart_and_resumes_without_repeating_work(pg):
    one = Process(pg)
    view = submit(one, "clean_approve")
    assert view.row.status == "awaiting_officer"
    one.stop()  # the process is gone; so are its document buffer and its in-memory state

    two = Process(pg)
    try:
        view = two.service.get("clean_approve")
        assert view.pending["interrupt_id"] == "clean_approve:a0" and view.state.risk.rating == "low"
        view = two.service.decide("clean_approve", approve(view), OFFICER)
        assert view.row.status == "approved" and view.state.execution.customer_id.startswith("CUST-")
        done = [r.node for r in two.audit.rows("clean_approve") if r.event_type == "node_completed"]
        assert done == [
            "intake",
            "extract",
            "screen",
            "assess",
            "execute",
        ]  # each node once, across both processes
        assert len(two.events("tool_call", "clean_approve")) == 2  # the KYC service was not called again
        assert len(two.events("approval_requested", "clean_approve")) == 1
        assert two.audit.verify().ok
    finally:
        two.stop()


def test_checkpoints_hold_state_but_never_document_content(pg):
    proc = Process(pg)
    try:
        submit(proc, "clean_approve")
        with pg.owner.connect() as c:
            n = c.execute(text("SELECT count(*) FROM langgraph.checkpoints")).scalar()
            leaks = 0
            for table, column in (
                ("checkpoint_blobs", "blob"),
                ("checkpoint_writes", "blob"),
                ("checkpoints", "checkpoint::text::bytea"),
            ):
                leaks += c.execute(
                    text(
                        f"SELECT count(*) FROM langgraph.{table} WHERE position('SYNTHETIC TEST DOCUMENT'::bytea in {column}) > 0"  # noqa: S608 - constants in this test
                    )
                ).scalar()
        assert n >= 4 and leaks == 0
    finally:
        proc.stop()


def test_concurrent_decisions_on_one_pause_apply_exactly_once(pg):
    proc = Process(pg)
    try:
        view = submit(proc, "clean_approve")
        results: list[object] = []
        gate = threading.Barrier(8)

        def decide() -> None:
            gate.wait()
            try:
                results.append(proc.service.decide("clean_approve", approve(view), OFFICER))
            except Conflict as exc:
                results.append(exc)

        threads = [threading.Thread(target=decide) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners = [r for r in results if not isinstance(r, Conflict)]
        assert len(winners) == 1 and len(results) == 8
        assert len(proc.events("execute_completed")) == 1 and len(proc.events("execute_requested")) == 1
        assert proc.service.get("clean_approve").row.status == "approved"
        assert proc.audit.verify().ok
    finally:
        proc.stop()


def test_a_second_process_cannot_run_a_case_that_the_first_is_running(pg):
    one, two = Process(pg), Process(pg)
    try:
        view = submit(one, "clean_approve")
        with one.service.locks.hold("clean_approve"):
            with pytest.raises(Conflict, match="another runner"):
                two.service.decide("clean_approve", approve(view), OFFICER)
        assert two.service.recover() == [
            "clean_approve"
        ]  # the claimed decision is finished once the lock is free
        assert two.service.get("clean_approve").row.status == "approved"
        assert len(two.events("execute_completed")) == 1
    finally:
        one.stop()
        two.stop()


def test_a_crash_after_the_claim_is_finished_by_the_next_process(pg):
    one = Process(pg)
    view = submit(one, "clean_approve")
    one.service.graph.invoke = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("killed"))  # type: ignore[method-assign]
    crashed = one.service.decide("clean_approve", approve(view), OFFICER)
    assert crashed.row.status == "resuming"
    one.stop()
    two = Process(pg)
    try:
        assert two.service.recover() == ["clean_approve"]
        assert two.service.get("clean_approve").row.status == "approved"
        assert len(two.events("execute_completed")) == 1 and len(two.events("decision_received")) == 1
    finally:
        two.stop()


def test_the_information_round_survives_a_restart(pg):
    one = Process(pg)
    view = submit(one, "missing_poa")
    view = one.service.decide(
        "missing_poa",
        DecisionRequest(
            interrupt_id=view.pending["interrupt_id"],
            action="request_more_info",
            note="Proof of address needed",
        ),
        OFFICER,
    )
    assert view.row.status == "awaiting_documents"
    one.stop()
    two = Process(pg)
    try:
        view = two.service.get("missing_poa")
        follow = [
            UploadedDoc(d.doc_type, d.content.encode()) for d in CASES["missing_poa"].followup_documents
        ]
        view = two.service.add_documents("missing_poa", view.pending["interrupt_id"], follow, SUBMITTER)
        assert view.row.status == "awaiting_officer" and view.state.info_rounds == 1
        view = two.service.decide("missing_poa", approve(view), OFFICER)
        assert view.row.status == "approved"
        assert two.audit.verify().ok
    finally:
        two.stop()


def test_purge_works_for_the_app_role_and_leaves_the_audit_log_alone(pg):
    proc = Process(pg)
    try:
        view = submit(proc, "clean_approve")
        proc.service.decide("clean_approve", approve(view), OFFICER)
        proc.service.store.project("clean_approve", updated_at=utcnow() - timedelta(days=45))
        before = proc.audit.verify().rows_checked
        assert proc.service.purge_checkpoints(30) == 1
        with pg.owner.connect() as c:
            assert (
                c.execute(
                    text("SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = 'clean_approve'")
                ).scalar()
                == 0
            )
        view = proc.service.get("clean_approve")
        assert view.state is None and view.row.final["customer_id"]
        assert proc.audit.verify().ok and proc.audit.verify().rows_checked == before + 1
    finally:
        proc.stop()


def test_the_db_cli_requires_its_settings(monkeypatch, capsys):
    monkeypatch.delenv("OWNER_DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DB_ROLE", raising=False)
    assert db.main(["migrate"]) == 2
