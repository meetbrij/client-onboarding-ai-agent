"""CaseService: pauses, decisions, refusals, recovery and retention (in-memory checkpointer, SQLite projection)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from onboarding.audit import AuditEvent, MemoryAuditLog
from onboarding.decision import DecisionRequest
from onboarding.llm.client import FakeLlm
from onboarding.runner import OFFICER, SUBMITTER, build_offline_env
from onboarding.service import CaseNotFound, Conflict, Forbidden, NotAllowed, Unavailable, UploadedDoc
from onboarding.store import utcnow
from tests.helpers import CASES


def docs(case, followup=False):
    items = case.followup_documents if followup else case.documents
    return [UploadedDoc(d.doc_type, d.content.encode(), "specimen.txt") for d in items]


def submit(case_id, env=None, **kw):
    env = env or build_offline_env(**kw)
    case = CASES[case_id]
    view = env.service.create_case(case.applicant, docs(case), SUBMITTER, case_id=case_id)
    return env, case, view


def decision(view, action="approve", note=None, **disp):
    return DecisionRequest(
        interrupt_id=view.pending["interrupt_id"], action=action, note=note, dispositions=disp
    )


def events(env, kind):
    return [r for r in env.audit.rows() if r.event_type == kind]


# ------------------------------------------------------------------ creation and pause
def test_a_new_case_pauses_for_the_officer_with_a_stable_interrupt_id():
    env, _, view = submit("clean_approve")
    assert view.row.status == "awaiting_officer" and view.row.waiting_on == "approve"
    assert (
        view.pending["kind"] == "approve"
        and view.row.interrupt_id == "clean_approve:a0" == view.pending["interrupt_id"]
    )
    assert len(events(env, "approval_requested")) == 1
    assert view.row.risk_rating == "low" and view.row.recommendation == "approve"


def test_unknown_case_is_not_found():
    env = build_offline_env()
    with pytest.raises(CaseNotFound):
        env.service.get("nope")
    with pytest.raises(CaseNotFound):
        env.service.decide("nope", DecisionRequest(interrupt_id="x", action="reject"), OFFICER)


def test_document_bytes_are_consumed_by_extract_and_never_stored():
    env, _, view = submit("clean_approve")
    assert len(env.deps.buffer) == 0
    assert "SYNTHETIC TEST DOCUMENT" not in view.state.model_dump_json()


# ------------------------------------------------------------------ the happy path
def test_approval_executes_exactly_once_and_records_the_officer():
    env, _, view = submit("clean_approve")
    view = env.service.decide("clean_approve", decision(view), OFFICER)
    assert view.row.status == "approved" and view.state.execution.customer_id.startswith("CUST-")
    assert view.state.decision.officer == OFFICER and view.row.waiting_on is None
    assert len(events(env, "execute_completed")) == 1
    assert [r.event_type for r in env.audit.rows() if r.event_type.startswith("execute")] == [
        "execute_requested",
        "execute_completed",
    ]
    assert view.row.final["customer_id"] == view.state.execution.customer_id


def test_rejection_ends_the_case_without_a_customer():
    env, _, view = submit("true_sanctions_hit")
    view = env.service.decide(
        "true_sanctions_hit", decision(view, "reject", "Confirmed match", **{"CDi.009": "confirmed"}), OFFICER
    )
    assert view.row.status == "rejected" and view.state.execution is None
    assert (
        view.state.screening.hits[0].disposition == "confirmed"
        and view.state.screening.hits[0].disposition_by == OFFICER
    )
    assert not events(env, "execute_requested")


# ------------------------------------------------------------------ refusals
def test_a_stale_or_wrong_interrupt_id_is_refused_and_audited():
    env, _, view = submit("clean_approve")
    with pytest.raises(Conflict):
        env.service.decide(
            "clean_approve", DecisionRequest(interrupt_id="clean_approve:a7", action="approve"), OFFICER
        )
    refused = events(env, "action_refused")
    assert refused and refused[-1].actor == OFFICER and "stale" in refused[-1].payload["reason"]
    assert env.service.get("clean_approve").row.status == "awaiting_officer"  # nothing changed


def test_a_duplicate_decision_is_refused_after_the_first_is_applied():
    env, _, view = submit("clean_approve")
    first = decision(view)
    env.service.decide("clean_approve", first, OFFICER)
    with pytest.raises(Conflict):
        env.service.decide("clean_approve", first, OFFICER)
    assert len(events(env, "execute_completed")) == 1 and len(events(env, "decision_received")) == 1


def test_nobody_decides_a_case_they_submitted():
    env, _, view = submit("clean_approve")
    with pytest.raises(Forbidden):
        env.service.decide("clean_approve", decision(view), SUBMITTER)
    assert "separation of duties" in events(env, "action_refused")[-1].payload["reason"]
    assert env.service.get("clean_approve").row.status == "awaiting_officer"


def test_the_guard_blocks_an_approval_with_an_undisposed_hit():
    env, _, view = submit("near_miss_dob_mismatch")
    with pytest.raises(NotAllowed) as e:
        env.service.decide(
            "near_miss_dob_mismatch", decision(view, "approve", "Looks like a different person"), OFFICER
        )
    assert any("CDi.011" in p for p in e.value.problems)
    assert env.service.get("near_miss_dob_mismatch").row.status == "awaiting_officer"
    assert not events(env, "decision_received")  # a refused decision is not "received"


def test_the_guard_blocks_approval_while_extraction_was_unavailable():
    env, _, view = submit("kyc_unavailable")
    with pytest.raises(NotAllowed) as e:
        env.service.decide(
            "kyc_unavailable", decision(view, "approve", "I checked the originals by hand"), OFFICER
        )
    assert any("extraction was unavailable" in p for p in e.value.problems)


def test_a_confirmed_hit_cannot_be_approved_through_the_service():
    env, _, view = submit("true_sanctions_hit")
    with pytest.raises(NotAllowed):
        env.service.decide(
            "true_sanctions_hit",
            decision(view, "approve", "Overriding the system", **{"CDi.009": "confirmed"}),
            OFFICER,
        )


def test_a_failing_audit_write_means_the_decision_is_not_applied():
    class Flaky(MemoryAuditLog):
        def append(self, event: AuditEvent):
            if event.event_type == "decision_received":
                raise RuntimeError("audit database down")
            return super().append(event)

    env = build_offline_env(audit=Flaky())
    _, _, view = submit("clean_approve", env)
    with pytest.raises(Unavailable, match="audit log is unavailable"):
        env.service.decide("clean_approve", decision(view), OFFICER)
    after = env.service.get("clean_approve")
    assert after.row.status == "awaiting_officer" and after.row.waiting_on == "approve"
    assert after.state.execution is None and not events(env, "execute_requested")


# ------------------------------------------------------------------ information round
def test_requesting_more_information_waits_for_documents_then_loops_back():
    env, case, view = submit("missing_poa")
    assert view.row.recommendation == "request_info" and view.state.missing_documents == ["proof_of_address"]
    view = env.service.decide(
        "missing_poa", decision(view, "request_more_info", "Proof of address needed"), OFFICER
    )
    assert view.row.status == "awaiting_documents" and view.pending["kind"] == "await_docs"
    assert view.pending["needed"] == ["proof_of_address"] and len(events(env, "documents_requested")) == 1
    view = env.service.add_documents(
        "missing_poa", view.pending["interrupt_id"], docs(case, followup=True), SUBMITTER
    )
    assert view.row.status == "awaiting_officer" and view.row.interrupt_id == "missing_poa:a1"
    assert view.state.info_rounds == 1 and view.state.missing_documents == [] and view.state.decision is None
    assert view.row.recommendation == "approve" and view.state.extraction.available
    assert len(events(env, "tool_call")) == 2  # the first document was not sent to the KYC service again
    view = env.service.decide("missing_poa", decision(view, "approve"), OFFICER)
    assert view.row.status == "approved"


def test_documents_for_the_wrong_pause_or_nothing_at_all_are_refused():
    env, case, view = submit("missing_poa")
    with pytest.raises(Conflict):
        env.service.add_documents(
            "missing_poa", view.pending["interrupt_id"], docs(case, True), SUBMITTER
        )  # not waiting for docs
    view = env.service.decide(
        "missing_poa", decision(view, "request_more_info", "Proof of address needed"), OFFICER
    )
    iid = view.pending["interrupt_id"]
    with pytest.raises(Conflict):
        env.service.add_documents("missing_poa", "missing_poa:d9", docs(case, True), SUBMITTER)
    with pytest.raises(NotAllowed):
        env.service.add_documents("missing_poa", iid, [], SUBMITTER)
    assert env.service.get("missing_poa").row.status == "awaiting_documents"


def test_a_refused_upload_leaves_no_bytes_behind():
    env, case, view = submit("missing_poa")
    with pytest.raises(Conflict):
        env.service.add_documents("missing_poa", "wrong", docs(case, True), SUBMITTER)
    assert len(env.deps.buffer) == 0  # the pause check happens before anything is staged
    env2, case2, view2 = submit("missing_poa")
    view2 = env2.service.decide(
        "missing_poa", decision(view2, "request_more_info", "Proof of address needed"), OFFICER
    )
    env2.service.add_documents("missing_poa", view2.pending["interrupt_id"], docs(case2, True), SUBMITTER)
    assert len(env2.deps.buffer) == 0


def test_the_information_round_cap_is_enforced():
    env, case, view = submit("missing_poa", max_info_rounds=1)
    view = env.service.decide(
        "missing_poa", decision(view, "request_more_info", "Proof of address needed"), OFFICER
    )
    # the client sends nothing useful (the same document again); the officer cannot ask a second time
    view = env.service.add_documents("missing_poa", view.pending["interrupt_id"], docs(case), SUBMITTER)
    with pytest.raises(NotAllowed) as e:
        env.service.decide(
            "missing_poa", decision(view, "request_more_info", "Still missing the proof"), OFFICER
        )
    assert any("limit of 1" in p for p in e.value.problems)
    view = env.service.decide("missing_poa", decision(view, "reject", "Documents never arrived"), OFFICER)
    assert view.row.status == "rejected"


def test_officer_dispositions_survive_a_new_round():
    env, case, view = submit("near_miss_dob_mismatch")
    view = env.service.decide(
        "near_miss_dob_mismatch",
        decision(view, "request_more_info", "Please confirm the date of birth", **{"CDi.011": "cleared"}),
        OFFICER,
    )
    view = env.service.add_documents(
        "near_miss_dob_mismatch", view.pending["interrupt_id"], docs(case), SUBMITTER
    )
    hit = view.state.screening.hits[0]
    assert (hit.entry_id, hit.disposition, hit.disposition_by) == ("CDi.011", "cleared", OFFICER)
    view = env.service.decide(
        "near_miss_dob_mismatch", decision(view, "approve", "Different person, DOB confirmed"), OFFICER
    )
    assert view.row.status == "approved"


# ------------------------------------------------------------------ execute safety
def test_execute_is_idempotent_at_the_bank_even_if_the_node_runs_again():
    env, _, view = submit("clean_approve")
    view = env.service.decide("clean_approve", decision(view), OFFICER)
    first = view.state.execution.customer_id
    again = env.deps.bank.create_customer(
        "clean_approve",
        {
            "case_id": "clean_approve",
            "full_name": view.state.applicant.name,
            "date_of_birth": view.state.applicant.dob,
            "nationality": view.state.applicant.nationality,
            "residence_country": view.state.applicant.residence_country,
            "occupation": view.state.applicant.occupation,
            "risk_rating": "low",
        },
    )
    assert again.customer_id == first and again.replayed is True


def test_the_execute_node_refuses_to_run_without_an_officers_approval():
    from onboarding.graph.nodes import make_nodes
    from onboarding.models import CaseState
    from tests.helpers import applicant

    env = build_offline_env()
    execute = make_nodes(env.deps)["execute"]
    with pytest.raises(PermissionError):
        execute(CaseState(case_id="x", applicant=applicant()))
    from onboarding.models import Decision

    with pytest.raises(PermissionError):
        execute(
            CaseState(
                case_id="x", applicant=applicant(), decision=Decision(action="reject", officer="o", at="t")
            )
        )
    assert not events(env, "execute_requested")


def test_the_graph_has_no_path_to_execute_except_through_approve():
    env = build_offline_env()
    edges = env.service.graph.get_graph().edges
    into_execute = {e.source for e in edges if e.target == "execute"}
    assert into_execute == {"approve"}
    into_approve = {e.source for e in edges if e.target == "approve"}
    assert into_approve == {"assess"}


# ------------------------------------------------------------------ recovery
class Boom(RuntimeError):
    pass


def test_a_crash_between_the_claim_and_the_resume_is_finished_at_recovery():
    env, _, view = submit("clean_approve")
    real = env.service.graph.invoke
    env.service.graph.invoke = lambda *a, **k: (_ for _ in ()).throw(Boom("process killed"))  # type: ignore[method-assign]
    view = env.service.decide("clean_approve", decision(view), OFFICER)
    assert view.row.status == "resuming" and view.row.pending and "Boom" in view.row.last_error
    assert view.state.execution is None
    env.service.graph.invoke = real  # type: ignore[method-assign]
    assert env.service.recover() == ["clean_approve"]
    done = env.service.get("clean_approve")
    assert done.row.status == "approved" and done.row.pending is None and done.row.last_error is None
    assert len(events(env, "execute_completed")) == 1 and len(events(env, "decision_received")) == 1


def test_recovery_continues_from_the_checkpoint_without_repeating_completed_nodes():
    env = build_offline_env()
    case = CASES["clean_approve"]
    calls = {"n": 0}
    real_extract = env.deps.kyc.extract

    def flaky(content, doc_type, filename="document"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Boom("unexpected failure mid-extract")
        return real_extract(content, doc_type, filename)

    env.deps.kyc.extract = flaky  # type: ignore[method-assign]
    view = env.service.create_case(case.applicant, docs(case), SUBMITTER, case_id="clean_approve")
    assert view.row.status == "running" and "Boom" in view.row.last_error
    # the bytes were taken before the failure: they are gone, so recovery degrades rather than repeating work
    assert env.service.recover() == ["clean_approve"]
    view = env.service.get("clean_approve")
    assert view.row.status == "awaiting_officer"
    done = [r.node for r in env.audit.rows() if r.event_type == "node_completed"]
    assert done.count("intake") == 1 and done == ["intake", "extract", "screen", "assess"]
    assert "extraction_unavailable" in view.state.degraded  # honest outcome after losing the bytes
    assert events(env, "run_failed") and events(env, "case_recovered")


def test_recovery_of_a_case_with_no_checkpoint_degrades_instead_of_failing():
    env = build_offline_env()
    case = CASES["clean_approve"]
    from onboarding.models import CaseState, DocumentRef

    state = CaseState(
        case_id="lost",
        applicant=case.applicant,
        submitted_by=SUBMITTER,
        documents=[DocumentRef(doc_ref="lost-1", doc_type="id_document", sha256="0" * 64)],
    )
    env.service.store.insert("lost", SUBMITTER, case.applicant.name, state.model_dump(mode="json"))
    assert env.service.recover() == ["lost"]
    view = env.service.get("lost")
    assert view.row.status == "awaiting_officer" and view.state.degraded == ["extraction_unavailable"]
    assert view.row.recommendation == "request_info"  # only one document was on record: the other is missing
    assert {r.rule_id for r in view.state.risk.fired_rules} == {"R-DOC-01", "R-DOC-03"}


def test_recovery_leaves_waiting_and_finished_cases_alone():
    env, _, view = submit("clean_approve")
    _, _, _ = submit("true_sanctions_hit", env)
    assert env.service.recover() == []


def test_a_case_being_run_elsewhere_is_left_alone():
    env, _, view = submit("clean_approve")
    with env.service.locks.hold("clean_approve"):
        with pytest.raises(Conflict, match="another runner"):
            env.service.decide("clean_approve", decision(view), OFFICER)
    # the decision was claimed before the run was refused: recovery finishes it
    assert env.service.get("clean_approve").row.status == "resuming"
    assert env.service.recover() == ["clean_approve"]
    assert env.service.get("clean_approve").row.status == "approved"


# ------------------------------------------------------------------ retention
def test_purging_removes_the_checkpoint_but_keeps_the_outcome_and_the_audit_trail():
    env, _, view = submit("clean_approve")
    env.service.decide("clean_approve", decision(view), OFFICER)
    _, _, other = submit("near_miss_dob_mismatch", env)  # still waiting: must not be purged
    env.service.store.project("clean_approve", updated_at=utcnow() - timedelta(days=45))
    assert env.service.purge_checkpoints(30) == 1
    purged = env.service.get("clean_approve")
    assert purged.state is None and purged.row.status == "approved" and purged.row.final["customer_id"]
    assert env.service.get("near_miss_dob_mismatch").state is not None
    assert events(env, "checkpoint_purged") and env.audit.verify().ok
    assert env.service.purge_checkpoints(30) == 0


def test_recent_cases_are_not_purged():
    env, _, view = submit("clean_approve")
    env.service.decide("clean_approve", decision(view), OFFICER)
    assert env.service.purge_checkpoints(30) == 0


def test_llm_outage_does_not_stop_the_workflow_end_to_end():
    env, _, view = submit("clean_approve", llm=FakeLlm(unavailable=True))
    assert view.state.degraded == ["llm_unavailable"] and view.state.recommendation.drafted_by == "template"
    view = env.service.decide("clean_approve", decision(view), OFFICER)
    assert view.row.status == "approved"
