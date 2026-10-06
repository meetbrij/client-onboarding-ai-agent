from __future__ import annotations

import threading
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from onboarding.audit import AuditEvent, AuditPayloadError, MemoryAuditLog, verify_rows
from onboarding.audit.__main__ import main as audit_cli
from onboarding.audit.chain import GENESIS_HASH, compute_row_hash
from onboarding.audit.postgres import PostgresAuditLog, install_schema


def ev(n: int = 0, **kw) -> AuditEvent:
    return AuditEvent(
        case_id=kw.pop("case_id", "case-1"),
        event_type=kw.pop("event_type", "node_completed"),
        node=kw.pop("node", "intake"),
        payload=kw.pop("payload", {"n": n, "score": 97.3}),
        **kw,
    )


def filled(n: int = 5) -> MemoryAuditLog:
    log = MemoryAuditLog()
    for i in range(n):
        log.append(ev(i))
    return log


# ---------------- chain logic (no database) ----------------
def test_first_row_links_to_genesis_and_rows_chain():
    rows = filled(3).rows()
    assert rows[0].prev_hash == GENESIS_HASH and rows[0].seq == 1
    assert rows[1].prev_hash == rows[0].row_hash and rows[2].prev_hash == rows[1].row_hash
    assert all(compute_row_hash(r.prev_hash, r) == r.row_hash for r in rows)


def test_intact_chain_verifies():
    v = filled(5).verify()
    assert v.ok and v.rows_checked == 5 and v.head_seq == 5 and v.first_broken_seq is None


def test_empty_chain_verifies():
    assert MemoryAuditLog().verify().ok


def test_altered_row_is_detected_at_that_row():
    rows = filled(5).rows()
    rows[2] = replace(rows[2], event_type="forged")
    v = verify_rows(rows)
    assert not v.ok and v.first_broken_seq == 3 and "altered" in v.reason


def test_altered_payload_is_detected():
    rows = filled(4).rows()
    rows[1] = replace(rows[1], payload={"n": 1, "score": 10.0})
    assert verify_rows(rows).first_broken_seq == 2


def test_deleted_row_is_detected():
    rows = filled(5).rows()
    del rows[2]
    v = verify_rows(rows)
    assert not v.ok and v.first_broken_seq == 4 and "missing" in v.reason


def test_reordered_rows_are_detected():
    rows = filled(4).rows()
    rows[1], rows[2] = rows[2], rows[1]
    assert not verify_rows(rows).ok


def test_relinked_row_is_detected():
    rows = filled(4).rows()
    rows[2] = replace(rows[2], prev_hash=GENESIS_HASH)
    v = verify_rows(rows)
    assert v.first_broken_seq == 3 and "prev_hash" in v.reason


def test_replaced_tail_with_recomputed_hashes_still_verifies_documenting_the_limit():
    """Truncating the tail is invisible to the chain alone; the head hash must be recorded elsewhere."""
    rows = filled(5).rows()
    assert verify_rows(rows[:3]).ok


def test_case_filter():
    log = MemoryAuditLog()
    log.append(ev(case_id="a"))
    log.append(ev(case_id="b"))
    log.append(ev(case_id="a"))
    assert [r.seq for r in log.rows("a")] == [1, 3] and log.verify().ok


@pytest.mark.parametrize("key", ["dob", "date_of_birth", "id_number", "address", "value", "DOB"])
def test_payload_with_personal_data_keys_is_rejected(key):
    with pytest.raises(AuditPayloadError):
        ev(payload={"ok": 1, "nested": {key: "x"}})


def test_oversized_payload_string_is_rejected():
    with pytest.raises(AuditPayloadError):
        ev(payload={"note": "x" * 1001})


def test_concurrent_appends_in_memory_keep_one_chain():
    log = MemoryAuditLog()
    threads = [threading.Thread(target=lambda: [log.append(ev(i)) for i in range(25)]) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(log.rows()) == 150 and log.verify().ok


# ---------------- Postgres: enforcement in the database ----------------
@pytest.fixture
def pg(scratch_db):
    install_schema(scratch_db.owner, app_role=scratch_db.app_role)
    return scratch_db


def test_postgres_append_and_verify_roundtrip(pg):
    log = PostgresAuditLog(pg.owner)
    for i in range(5):
        log.append(ev(i, prompt_name="p", prompt_version="3", model_id="m"))
    v = log.verify()
    assert v.ok and v.rows_checked == 5
    assert log.rows("case-1")[0].prompt_version == "3"


def test_install_schema_is_idempotent(pg):
    install_schema(pg.owner, app_role=pg.app_role)
    PostgresAuditLog(pg.owner).append(ev())


def test_trigger_rejects_update_delete_and_truncate_even_for_the_owner(pg):
    PostgresAuditLog(pg.owner).append(ev())
    for stmt in ("UPDATE audit_log SET event_type = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(DBAPIError, match="append-only"), pg.owner.begin() as c:
            c.execute(text(stmt))
    assert len(PostgresAuditLog(pg.owner).rows()) == 1


def test_application_role_can_insert_and_select_but_nothing_else(pg):
    app_engine = create_engine(pg.app_url)
    log = PostgresAuditLog(app_engine)
    log.append(ev())
    assert len(log.rows()) == 1
    for stmt in ("UPDATE audit_log SET event_type = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(ProgrammingError, match="permission denied"), app_engine.begin() as c:
            c.execute(text(stmt))
    with pytest.raises(ProgrammingError, match="must be owner"), app_engine.begin() as c:
        c.execute(text("DROP TRIGGER audit_log_no_update_delete ON audit_log"))
    app_engine.dispose()


def test_superuser_tampering_is_caught_by_verify(pg):
    log = PostgresAuditLog(pg.owner)
    for i in range(5):
        log.append(ev(i))
    with pg.owner.begin() as c:
        c.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        c.execute(text("UPDATE audit_log SET event_type = 'forged' WHERE seq = 3"))
    v = log.verify()
    assert not v.ok and v.first_broken_seq == 3 and "altered" in v.reason


def test_superuser_row_deletion_is_caught_by_verify(pg):
    log = PostgresAuditLog(pg.owner)
    for i in range(5):
        log.append(ev(i))
    with pg.owner.begin() as c:
        c.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        c.execute(text("DELETE FROM audit_log WHERE seq = 2"))
    v = log.verify()
    assert not v.ok and v.first_broken_seq == 3


def test_concurrent_appends_on_postgres_make_one_linear_chain(pg):
    log = PostgresAuditLog(pg.owner)

    def work(worker: int) -> None:
        for i in range(10):
            log.append(ev(i, case_id=f"case-{worker}"))

    threads = [threading.Thread(target=work, args=(w,)) for w in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    v = log.verify()
    assert v.ok and v.rows_checked == 80 and v.head_seq == 80


def test_cli_verify_exit_codes(pg, capsys):
    url = pg.owner.url.render_as_string(hide_password=False)
    log = PostgresAuditLog(pg.owner)
    for i in range(3):
        log.append(ev(i))
    assert audit_cli(["verify", "--database-url", url]) == 0
    assert "audit chain OK" in capsys.readouterr().out
    with pg.owner.begin() as c:
        c.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        c.execute(text("UPDATE audit_log SET actor = 'mallory' WHERE seq = 2"))
    assert audit_cli(["verify", "--database-url", url]) == 1
    out = capsys.readouterr().out
    assert "BROKEN at seq 2" in out


def test_cli_without_database_url_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert audit_cli(["verify"]) == 2
