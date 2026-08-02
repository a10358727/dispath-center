from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

import app.audit as audit_module
from app.audit import (
    SYSTEM_AUDIT_ACTOR,
    AuditActor,
    append_audit,
    audit_actor_from_request_context,
    audit_health_snapshot,
    read_audit,
    tail_audit,
)
from app.identity import Actor, ActorType, RequestContext


def test_legacy_append_keeps_exact_record_shape_and_json_bytes(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(
        audit_module, "now_iso", lambda: "2026-07-12T01:02:03+00:00"
    )

    record = append_audit(
        "enqueue",
        {"job_id": 7, "note": "保留"},
        result="ok",
        path=path,
    )

    assert record == {
        "ts": "2026-07-12T01:02:03+00:00",
        "action": "enqueue",
        "params": {"job_id": 7, "note": "保留"},
        "result": "ok",
    }
    assert path.read_bytes() == (
        b'{"ts": "2026-07-12T01:02:03+00:00", "action": "enqueue", '
        b'"params": {"job_id": 7, "note": "\xe4\xbf\x9d\xe7\x95\x99"}, '
        b'"result": "ok"}\n'
    )


@pytest.mark.parametrize(
    ("actor_type", "authentication"),
    [
        (ActorType.HUMAN, "session"),
        (ActorType.SERVICE, "service_token"),
        (ActorType.LEGACY, "legacy_shared_token"),
    ],
)
def test_request_context_projects_only_safe_actor_fields(
    tmp_path, actor_type, authentication
):
    path = tmp_path / "audit.jsonl"
    actor_id = f"{actor_type.value}-actor-id"
    context = RequestContext(
        actor=Actor(
            id=actor_id,
            actor_type=actor_type,
            display_name="web-source-must-not-be-identity",
            email="private-email@example.invalid",
        ),
        authentication_method=authentication,
        service_token_id="private-service-token-row-id",
        service_scopes=frozenset({"private.scope"}),
    )

    audit_actor = audit_actor_from_request_context(context)
    record = append_audit(
        "approval_requested", {"kind": "enqueue"}, path=path, actor=audit_actor
    )

    assert record["actor"] == {
        "id": actor_id,
        "kind": actor_type.value,
        "authentication": authentication,
    }
    serialized = path.read_text(encoding="utf-8")
    assert "private-email" not in serialized
    assert "web-source" not in serialized
    assert "private-service-token" not in serialized
    assert "private.scope" not in serialized


def test_anonymous_context_omits_actor_envelope(tmp_path):
    path = tmp_path / "audit.jsonl"

    audit_actor = audit_actor_from_request_context(RequestContext())
    record = append_audit("read_only_probe", path=path, actor=audit_actor)

    assert audit_actor is None
    assert "actor" not in record
    assert "actor" not in read_audit(path)[0]


def test_explicit_system_actor_has_stable_safe_envelope(tmp_path):
    path = tmp_path / "audit.jsonl"

    record = append_audit(
        "dispatch", {"job_id": 9}, path=path, actor=SYSTEM_AUDIT_ACTOR
    )

    assert record["actor"] == {
        "id": "system",
        "kind": "system",
        "authentication": "system",
    }
    assert SYSTEM_AUDIT_ACTOR == AuditActor("system", "system", "system")


def test_append_does_not_mutate_caller_inputs(tmp_path):
    path = tmp_path / "audit.jsonl"
    params = {"nested": [{"value": "original"}]}
    actor = AuditActor("actor-1", "human", "session")

    record = append_audit("approval_requested", params, path=path, actor=actor)

    assert params == {"nested": [{"value": "original"}]}
    assert actor == AuditActor("actor-1", "human", "session")
    assert record["params"] is params
    assert record["actor"] == {
        "id": "actor-1",
        "kind": "human",
        "authentication": "session",
    }


def test_historical_and_actor_aware_jsonl_records_read_together(tmp_path):
    path = tmp_path / "audit.jsonl"
    historical = {
        "ts": "2025-01-02T03:04:05+00:00",
        "action": "historical",
        "params": {"kept": True},
        "result": "ok",
    }
    path.write_text(json.dumps(historical) + "\n", encoding="utf-8")

    append_audit("new", path=path, actor=SYSTEM_AUDIT_ACTOR)

    records = read_audit(path)
    assert records[0] == historical
    assert "actor" not in records[0]
    assert records[1]["actor"] == SYSTEM_AUDIT_ACTOR.to_envelope()
    assert [record["action"] for record in tail_audit(path)] == ["new", "historical"]


def test_concurrent_appends_remain_complete_jsonl_records(tmp_path):
    path = tmp_path / "audit.jsonl"

    def append_one(index: int) -> None:
        append_audit(
            "concurrent",
            {"index": index},
            path=path,
            actor=SYSTEM_AUDIT_ACTOR,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append_one, range(48)))

    records = read_audit(path)
    assert len(records) == 48
    assert {record["params"]["index"] for record in records} == set(range(48))
    assert all(record["actor"] == SYSTEM_AUDIT_ACTOR.to_envelope() for record in records)


def test_audit_write_failure_is_best_effort(tmp_path, monkeypatch):
    before = audit_health_snapshot()
    def fail_open(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("builtins.open", fail_open)

    record = append_audit(
        "still_returns", path=tmp_path / "audit.jsonl", actor=SYSTEM_AUDIT_ACTOR
    )

    assert record["action"] == "still_returns"
    assert record["actor"] == SYSTEM_AUDIT_ACTOR.to_envelope()
    after = audit_health_snapshot()
    assert after["write_failures"] == before["write_failures"] + 1
    assert after["consecutive_failures"] >= 1
    assert after["last_failure_category"] == "OSError"
    assert after["delivery"] == "best_effort"
    assert after["domain_action_aborted_on_failure"] is False
