from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.approvals import (
    JobNotFoundError,
    approval_to_dict,
    approve,
    maybe_auto_approve,
    reject,
    request_enqueue_approval,
    request_stop_approval,
)
from app.audit import read_audit
from app.identity import Actor, ActorType, RequestContext
from app.jobqueue import DangerousCommandError


VALID_DIFF = (
    "diff --git a/train.py b/train.py\n"
    "--- a/train.py\n"
    "+++ b/train.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


class FakeResult:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        exit_status: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


async def _empty_ssh(server: str, command: str, timeout: int) -> FakeResult:
    return FakeResult()


async def _deploy_local_run(command: str, timeout: int) -> FakeResult:
    if "symbolic-ref --short HEAD" in command:
        return FakeResult(stdout="main\n")
    if "rev-parse HEAD" in command:
        return FakeResult(stdout="cafef00d1234567890\n")
    return FakeResult()


async def _deploy_ssh(server: str, command: str, timeout: int) -> FakeResult:
    if "test -e" in command:
        return FakeResult(stdout="NOT_EXIST\n")
    return FakeResult()


def _context(
    actor_id: str,
    *,
    actor_type: ActorType = ActorType.HUMAN,
    authentication: str = "session",
) -> RequestContext:
    return RequestContext(
        actor=Actor(
            id=actor_id,
            actor_type=actor_type,
            display_name=f"Display {actor_id}",
            email=f"{actor_id}@example.invalid",
        ),
        authentication_method=authentication,
        service_token_id=(
            "service-token-row-must-not-be-audited"
            if actor_type is ActorType.SERVICE
            else None
        ),
        service_scopes=(
            frozenset({"private.scope.must-not-be-audited"})
            if actor_type is ActorType.SERVICE
            else frozenset()
        ),
    )


def _raw_payload(db, approval_id: int) -> str:
    with db.cursor() as cur:
        cur.execute("SELECT payload FROM approvals WHERE id = ?", (approval_id,))
        return cur.fetchone()["payload"]




def test_source_metadata_cannot_spoof_service_requester_identity(db, audit_path):
    context = _context(
        "service-account-actor",
        actor_type=ActorType.SERVICE,
        authentication="service_token",
    )

    approval = request_enqueue_approval(
        db,
        command="echo source",
        source="web",
        audit_path=audit_path,
        request_context=context,
    )

    assert approval.payload["source"] == "web"
    assert approval.requester_actor_id == "service-account-actor"
    record = read_audit(audit_path)[-1]
    assert record["actor"] == {
        "id": "service-account-actor",
        "kind": "service",
        "authentication": "service_token",
    }
    serialized = Path(audit_path).read_text(encoding="utf-8")
    assert "service-token-row-must-not-be-audited" not in serialized
    assert "private.scope.must-not-be-audited" not in serialized


def test_validation_reject_audit_uses_request_actor_without_creating_approval(
    db, audit_path
):
    context = _context("requester-actor")

    with pytest.raises(DangerousCommandError):
        request_enqueue_approval(
            db,
            command="rm -rf /tmp/forbidden",
            audit_path=audit_path,
            request_context=context,
        )

    assert db.list_approvals() == []
    record = read_audit(audit_path)[-1]
    assert record["action"] == "reject"
    assert record["actor"]["id"] == "requester-actor"


@pytest.mark.parametrize(
    ("approved_by", "expected_mechanism"),
    [
        ("human", "manual"),
        ("web-direct", "web-direct"),
        ("auto-rule-not-an-index", "manual"),
    ],
)
def test_approve_records_decision_actor_mechanism_and_payload_bytes_atomically(
    db,
    audit_path,
    monkeypatch,
    approved_by,
    expected_mechanism,
):
    requester = _context("requester-actor")
    decider = _context("decision-actor")
    approval = request_enqueue_approval(
        db,
        command="echo approved",
        source="web" if approved_by == "web-direct" else "api",
        audit_path=audit_path,
        request_context=requester,
    )
    payload_before = _raw_payload(db, approval.id).encode("utf-8")

    update_calls: list[dict] = []
    real_update = db.update_approval

    def recording_update(approval_id: int, **fields):
        update_calls.append(dict(fields))
        return real_update(approval_id, **fields)

    monkeypatch.setattr(db, "update_approval", recording_update)
    result = asyncio.run(
        approve(
            db,
            approval.id,
            audit_path=audit_path,
            approved_by=approved_by,
            note="decision note",
            request_context=decider,
        )
    )

    decided = result["approval"]
    assert decided.status == "approved"
    assert decided.requester_actor_id == "requester-actor"
    assert decided.decision_actor_id == "decision-actor"
    assert decided.decision_mechanism == expected_mechanism
    assert _raw_payload(db, approval.id).encode("utf-8") == payload_before
    terminal_call = [call for call in update_calls if call.get("status") == "approved"]
    assert terminal_call == [
        {
            "status": "approved",
            "decided_at": decided.decided_at,
            "note": "decision note",
            "decision_actor_id": "decision-actor",
            "decision_actor_kind": "human",
            "decision_mechanism": expected_mechanism,
        }
    ]

    durable_events = db.list_durable_audit_events(limit=20)
    materialized = next(
        event
        for event in durable_events
        if event["action"] == "execution_job_materialized"
    )
    decision = next(
        event
        for event in durable_events
        if event["action"] == "approval_decided"
        and event["approval_id"] == approval.id
    )
    assert materialized["actor"]["id"] == "decision-actor"
    assert decision["actor"]["id"] == "decision-actor"
    assert decision["actor"]["authentication"] == expected_mechanism
    assert not any(
        record["action"] in {"enqueue", "approve"}
        for record in read_audit(audit_path)
    )

    serialized = approval_to_dict(decided)
    assert list(serialized) == [
        "id",
        "kind",
        "payload",
        "status",
        "created_at",
        "decided_at",
        "note",
        "requester_actor_id",
        "decision_actor_id",
        "decision_mechanism",
    ]


def test_reject_records_manual_decider_without_mutating_payload(db, audit_path):
    approval = request_enqueue_approval(
        db,
        command="echo reject",
        audit_path=audit_path,
        request_context=_context("requester-actor"),
    )
    payload_before = _raw_payload(db, approval.id).encode("utf-8")

    decided = reject(
        db,
        approval.id,
        note="not now",
        audit_path=audit_path,
        request_context=_context("rejecting-actor"),
    )

    assert decided.status == "rejected"
    assert decided.decision_actor_id == "rejecting-actor"
    assert decided.decision_mechanism == "manual"
    assert _raw_payload(db, approval.id).encode("utf-8") == payload_before
    events = db.list_durable_audit_events(limit=20)
    record = next(
        event
        for event in events
        if event["action"] == "approval_decided"
        and event["approval_id"] == approval.id
    )
    assert record["result"] == "rejected"
    assert record["actor"]["id"] == "rejecting-actor"
    assert record["actor"]["authentication"] == "manual"
    assert "not now" not in str(record)


def test_auto_rule_uses_initiating_service_actor_and_never_fabricates_human(
    db, audit_path
):
    context = _context(
        "automation-service",
        actor_type=ActorType.SERVICE,
        authentication="service_token",
    )
    approval = request_enqueue_approval(
        db,
        command="echo auto",
        source="chatgpt",
        audit_path=audit_path,
        request_context=context,
    )
    payload_before = _raw_payload(db, approval.id).encode("utf-8")

    result = asyncio.run(
        maybe_auto_approve(
            db,
            approval,
            source="chatgpt",
            rules=[{"source": "chatgpt", "command_regex": "^echo "}],
            audit_path=audit_path,
            request_context=context,
        )
    )

    decided = result["approval"]
    assert decided.status == "approved"
    assert decided.decision_actor_id == "automation-service"
    assert decided.decision_mechanism == "auto-rule-0"
    assert _raw_payload(db, approval.id).encode("utf-8") == payload_before
    durable_events = db.list_durable_audit_events(limit=20)
    decision_records = [
        event
        for event in durable_events
        if event["action"] in {"execution_job_materialized", "approval_decided"}
        and event["approval_id"] == approval.id
    ]
    assert all(record["actor"]["kind"] == "service" for record in decision_records)
    decision = next(
        record
        for record in decision_records
        if record["action"] == "approval_decided"
    )
    assert decision["actor"]["authentication"] == "auto-rule-0"
    assert not any(
        record["action"] in {"enqueue", "approve"}
        for record in read_audit(audit_path)
    )
    serialized = Path(audit_path).read_text(encoding="utf-8")
    assert "service-token-row-must-not-be-audited" not in serialized
    assert "private.scope.must-not-be-audited" not in serialized


def test_anonymous_compatibility_keeps_nullable_actor_fields_and_actorless_audit(
    db, audit_path
):
    approval = request_enqueue_approval(
        db,
        command="echo anonymous",
        audit_path=audit_path,
    )
    result = asyncio.run(approve(db, approval.id, audit_path=audit_path))

    decided = result["approval"]
    assert decided.requester_actor_id is None
    assert decided.decision_actor_id is None
    assert decided.decision_mechanism == "manual"
    assert all("actor" not in record for record in read_audit(audit_path))


def test_stale_stop_decision_attributes_skipped_terminal_update(db, audit_path):
    job_id = db.insert_job(command="sleep 60", status="running")
    approval = request_stop_approval(
        db,
        job_id,
        audit_path=audit_path,
        request_context=_context("requester-actor"),
    )
    payload_before = _raw_payload(db, approval.id)
    db.update_job(job_id, status="done")

    result = asyncio.run(
        approve(
            db,
            approval.id,
            audit_path=audit_path,
            request_context=_context("decision-actor"),
        )
    )

    decided = result["approval"]
    assert decided.status == "approved"
    assert decided.decision_actor_id == "decision-actor"
    assert decided.decision_mechanism == "manual"
    assert result["job"].status == "done"
    assert _raw_payload(db, approval.id) == payload_before
    stop_record = read_audit(audit_path)[-1]
    assert stop_record["result"] == "skipped"
    assert stop_record["actor"]["id"] == "decision-actor"


def test_missing_stop_target_attributes_terminal_update_before_existing_error(
    db, audit_path
):
    job_id = db.insert_job(command="sleep 60", status="running")
    approval = request_stop_approval(db, job_id, audit_path=audit_path)
    db.delete_job(job_id)

    with pytest.raises(JobNotFoundError):
        asyncio.run(
            approve(
                db,
                approval.id,
                audit_path=audit_path,
                request_context=_context("decision-actor"),
            )
        )

    decided = db.get_approval(approval.id)
    assert decided.status == "approved"
    assert decided.decision_actor_id == "decision-actor"
    assert decided.decision_mechanism == "manual"
    assert read_audit(audit_path)[-1]["actor"]["id"] == "decision-actor"


def test_stale_inventory_forbidden_root_records_rejected_web_direct_decision(
    db, audit_path
):
    approval_id = db.insert_approval(
        "inventory_scan",
        {"server": "server-a", "project_roots": ["/etc"]},
        requester_actor_id="requester-actor",
    )

    result = asyncio.run(
        approve(
            db,
            approval_id,
            audit_path=audit_path,
            approved_by="web-direct",
            request_context=_context("decision-actor"),
        )
    )

    decided = result["approval"]
    assert decided.status == "rejected"
    assert decided.decision_actor_id == "decision-actor"
    assert decided.decision_mechanism == "web-direct"
    record = read_audit(audit_path)[-1]
    assert record["result"] == "rejected"
    assert record["actor"]["id"] == "decision-actor"


def test_server_running_job_rejection_records_decider(db, audit_path):
    approval_id = db.insert_approval("server_disable", {"name": "server-a"})
    job_id = db.insert_job(command="sleep 60", status="running")
    db.update_job(job_id, server="server-a")

    result = asyncio.run(
        approve(
            db,
            approval_id,
            audit_path=audit_path,
            request_context=_context("decision-actor"),
        )
    )

    decided = result["approval"]
    assert decided.status == "rejected"
    assert decided.decision_actor_id == "decision-actor"
    assert decided.decision_mechanism == "manual"


def test_precondition_exception_leaves_approval_pending_without_decision_attribution(
    db, audit_path
):
    approval_id = db.insert_approval(
        "inventory_scan",
        {"server": "server-a", "project_roots": ["/projects"]},
    )

    with pytest.raises(ValueError, match="ssh_run"):
        asyncio.run(
            approve(
                db,
                approval_id,
                audit_path=audit_path,
                request_context=_context("decision-actor"),
            )
        )

    approval = db.get_approval(approval_id)
    assert approval.status == "pending"
    assert approval.decided_at is None
    assert approval.decision_actor_id is None
    assert approval.decision_mechanism is None
