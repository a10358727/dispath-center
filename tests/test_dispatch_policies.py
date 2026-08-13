"""Goal 2 Slice 3 Dispatch Policy v1 tests
(docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md §Slice 3).

Clones the D5 Run Profile v1 test structure (tests/test_run_profiles.py):
additive immutable-revision schema, approval-gated create/update/archive
lifecycle, auto-approval exclusion, the `DISPATCH_POLICY_V1_ENABLED` rollback
switch, and the API surface. **This slice's policy object has zero runtime
effect**: scheduler_tick/is_idle/pick_job/enqueue are untouched and never
read `dispatch_policies` (that is Slice 4).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.approvals import (
    DispatchPolicyAdministrationDisabledError,
    IdentityTargetNotFoundError,
    InvalidDispatchPolicyRequestError,
    approve,
    maybe_auto_approve,
    request_dispatch_policy_archive_approval,
    request_dispatch_policy_create_approval,
    request_dispatch_policy_update_approval,
    request_run_profile_create_approval,
)
from app.identity import ActorType, RequestContext


def _dispatch_policy_app_state(enabled: bool = True):
    return SimpleNamespace(config=SimpleNamespace(dispatch_policy_v1_enabled=enabled))


def _human_context(db, name: str = "Ada") -> RequestContext:
    actor = db.insert_actor(
        actor_type=ActorType.HUMAN,
        display_name=name,
        platform_admin=True,
    )
    return RequestContext(actor=actor, authentication_method="session")


def _project(db, name="proj1"):
    db.insert_project(name, "https://example.invalid/proj1.git")
    return db.get_project(name)


def _approve(db, approval_id, audit_path, context, *, enabled=True):
    return asyncio.run(
        approve(
            db,
            approval_id,
            app_state=_dispatch_policy_app_state(enabled),
            audit_path=audit_path,
            request_context=context,
        )
    )


def _future_iso(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


_CONFIG = SimpleNamespace(dispatch_policy_v1_enabled=True)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_request_and_approval_produce_the_first_revision(db, audit_path):
    project = _project(db)
    context = _human_context(db)

    approval = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "nightly-idle",
        allowed_servers=["worker-a", "worker-b"],
        require_tag="gpu",
        dataset_required=True,
        max_concurrent_placements=2,
        valid_until=_future_iso(),
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    assert approval.kind == "dispatch_policy_create"
    assert approval.status == "pending"
    assert approval.payload["project_id"] == project.id
    assert approval.payload["name"] == "nightly-idle"
    assert approval.payload["allowed_servers"] == ["worker-a", "worker-b"]
    assert approval.payload["require_tag"] == "gpu"
    assert approval.payload["dataset_required"] is True
    assert approval.payload["max_concurrent_placements"] == 2
    assert approval.payload["run_profile_id"] is None

    result = _approve(db, approval.id, audit_path, context)

    policy = result["dispatch_policy"]
    assert policy.project_id == project.id
    assert policy.name == "nightly-idle"
    assert policy.revision == 1
    assert policy.status == "approved"
    assert policy.allowed_servers == ["worker-a", "worker-b"]
    assert policy.approval_id == approval.id
    assert result["approval"].status == "approved"

    events = db.list_durable_audit_events(limit=20)
    mutation = next(
        event
        for event in events
        if event["action"] == "dispatch_policy_revision_created"
    )
    assert mutation["result"] == "approved"
    assert mutation["resource_type"] == "dispatch_policy"
    assert mutation["resource_id"] == policy.id
    assert mutation["approval_id"] == approval.id
    assert mutation["params"] == {
        "name": "nightly-idle",
        "operation": "create",
        "project_id": project.id,
        "revision": 1,
    }
    assert sum(
        event["action"] == "approval_decided" for event in events
    ) == 1

    head = db.get_dispatch_policy_head(project.id, "nightly-idle")
    assert head == policy


def test_create_request_rejects_duplicate_name(db, audit_path):
    _project(db)
    context = _human_context(db)
    approval = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "dup",
        allowed_servers=["worker-a"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    _approve(db, approval.id, audit_path, context)

    with pytest.raises(InvalidDispatchPolicyRequestError, match="already exists"):
        request_dispatch_policy_create_approval(
            db,
            "proj1",
            "dup",
            allowed_servers=["worker-a"],
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )


def test_dispatch_policy_revision_and_decision_roll_back_together_on_audit_failure(
    db, audit_path, monkeypatch
):
    project = _project(db)
    context = _human_context(db)
    approval = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "rollback",
        allowed_servers=["worker-a"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    original = db.append_durable_audit_event_in_transaction

    def fail_revision(*args, **kwargs):
        if kwargs.get("action") == "dispatch_policy_revision_created":
            raise RuntimeError("policy revision audit fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_revision)
    with pytest.raises(RuntimeError, match="policy revision audit fault"):
        _approve(db, approval.id, audit_path, context)

    assert db.get_dispatch_policy_head(project.id, "rollback") is None
    pending = db.get_approval(approval.id)
    assert pending is not None and pending.status == "pending"


def test_create_request_rejects_unknown_project(db, audit_path):
    context = _human_context(db)
    with pytest.raises(IdentityTargetNotFoundError):
        request_dispatch_policy_create_approval(
            db,
            "no-such-project",
            "policy-a",
            allowed_servers=["worker-a"],
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )


def test_create_request_rejects_invalid_name_charset(db, audit_path):
    _project(db)
    context = _human_context(db)
    with pytest.raises(Exception):
        request_dispatch_policy_create_approval(
            db,
            "proj1",
            "not a valid name!",
            allowed_servers=["worker-a"],
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )


def test_create_request_rejects_empty_allowed_servers(db, audit_path):
    _project(db)
    context = _human_context(db)
    with pytest.raises(InvalidDispatchPolicyRequestError):
        request_dispatch_policy_create_approval(
            db,
            "proj1",
            "p",
            allowed_servers=[],
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )


def test_create_request_rejects_max_concurrent_placements_out_of_range(db, audit_path):
    _project(db)
    context = _human_context(db)
    with pytest.raises(InvalidDispatchPolicyRequestError):
        request_dispatch_policy_create_approval(
            db,
            "proj1",
            "p",
            allowed_servers=["worker-a"],
            max_concurrent_placements=0,
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )
    with pytest.raises(InvalidDispatchPolicyRequestError):
        request_dispatch_policy_create_approval(
            db,
            "proj1",
            "p2",
            allowed_servers=["worker-a"],
            max_concurrent_placements=11,
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )


def test_create_request_rejects_past_valid_until(db, audit_path):
    _project(db)
    context = _human_context(db)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with pytest.raises(InvalidDispatchPolicyRequestError):
        request_dispatch_policy_create_approval(
            db,
            "proj1",
            "p",
            allowed_servers=["worker-a"],
            valid_until=past,
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )


def test_create_request_rejects_run_profile_id_from_another_project(db, audit_path):
    _project(db, "proj1")
    _project(db, "proj2")
    context = _human_context(db)

    other_profile_approval = request_run_profile_create_approval(
        db,
        "proj2",
        "other-profile",
        config=SimpleNamespace(run_profile_v1_enabled=True),
        audit_path=audit_path,
        request_context=context,
    )
    other_profile = asyncio.run(
        approve(
            db,
            other_profile_approval.id,
            app_state=SimpleNamespace(
                config=SimpleNamespace(run_profile_v1_enabled=True)
            ),
            audit_path=audit_path,
            request_context=context,
        )
    )["run_profile"]

    with pytest.raises(InvalidDispatchPolicyRequestError):
        request_dispatch_policy_create_approval(
            db,
            "proj1",
            "p",
            allowed_servers=["worker-a"],
            run_profile_id=other_profile.id,
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )


def test_create_approval_rejects_concurrently_created_name(db, audit_path):
    project = _project(db)
    context = _human_context(db)

    approval = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "race",
        allowed_servers=["worker-a"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    other = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "race-2",
        allowed_servers=["worker-a"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    db.insert_dispatch_policy_revision(
        project_id=project.id,
        project_name="proj1",
        name="race",
        status="approved",
        allowed_servers=["worker-b"],
        require_tag=None,
        run_profile_id=None,
        dataset_required=False,
        max_concurrent_placements=1,
        valid_until=None,
        approval_id=other.id,
        created_by_actor_id=None,
    )

    result = _approve(db, approval.id, audit_path, context)
    assert result["approval"].status == "rejected"
    assert "already exists" in result["approval"].note


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_creates_a_new_revision_superseding_the_head(db, audit_path):
    project = _project(db)
    context = _human_context(db)

    create = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "p",
        allowed_servers=["worker-a"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    v1 = _approve(db, create.id, audit_path, context)["dispatch_policy"]

    update = request_dispatch_policy_update_approval(
        db,
        "proj1",
        "p",
        allowed_servers=["worker-a", "worker-b"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    assert update.payload["based_on_revision"] == 1
    v2_result = _approve(db, update.id, audit_path, context)
    v2 = v2_result["dispatch_policy"]

    assert v2.revision == 2
    assert v2.allowed_servers == ["worker-a", "worker-b"]
    assert v2.status == "approved"

    revisions = db.list_dispatch_policy_revisions(project.id, "p")
    assert [r.revision for r in revisions] == [2, 1]
    assert revisions[1].allowed_servers == ["worker-a"]
    assert db.get_dispatch_policy_head(project.id, "p") == v2
    assert v1.id != v2.id


def test_update_request_rejects_missing_policy(db, audit_path):
    _project(db)
    context = _human_context(db)
    with pytest.raises(IdentityTargetNotFoundError):
        request_dispatch_policy_update_approval(
            db,
            "proj1",
            "nope",
            allowed_servers=["worker-a"],
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )


def test_update_approval_rejects_stale_based_on_revision(db, audit_path):
    project = _project(db)
    context = _human_context(db)
    create = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "p",
        allowed_servers=["worker-a"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    _approve(db, create.id, audit_path, context)

    stale_update = request_dispatch_policy_update_approval(
        db,
        "proj1",
        "p",
        allowed_servers=["worker-stale"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    fresh_update = request_dispatch_policy_update_approval(
        db,
        "proj1",
        "p",
        allowed_servers=["worker-fresh"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    _approve(db, fresh_update.id, audit_path, context)

    result = _approve(db, stale_update.id, audit_path, context)
    assert result["approval"].status == "rejected"
    assert "changed since" in result["approval"].note
    assert [
        r.allowed_servers for r in db.list_dispatch_policy_revisions(project.id, "p")
    ] == [["worker-fresh"], ["worker-a"]]


def test_update_request_rejects_archived_policy(db, audit_path):
    _project(db)
    context = _human_context(db)
    create = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "p",
        allowed_servers=["worker-a"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    _approve(db, create.id, audit_path, context)
    archive = request_dispatch_policy_archive_approval(
        db, "proj1", "p", config=_CONFIG, audit_path=audit_path, request_context=context
    )
    _approve(db, archive.id, audit_path, context)

    with pytest.raises(InvalidDispatchPolicyRequestError, match="archived"):
        request_dispatch_policy_update_approval(
            db,
            "proj1",
            "p",
            allowed_servers=["worker-a"],
            config=_CONFIG,
            audit_path=audit_path,
            request_context=context,
        )


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def test_archive_creates_a_tombstone_revision_preserving_content(db, audit_path):
    project = _project(db)
    context = _human_context(db)
    create = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "p",
        allowed_servers=["worker-a"],
        require_tag="gpu",
        max_concurrent_placements=3,
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    v1 = _approve(db, create.id, audit_path, context)["dispatch_policy"]

    archive = request_dispatch_policy_archive_approval(
        db, "proj1", "p", config=_CONFIG, audit_path=audit_path, request_context=context
    )
    result = _approve(db, archive.id, audit_path, context)
    tombstone = result["dispatch_policy"]

    assert tombstone.revision == 2
    assert tombstone.status == "archived"
    assert tombstone.allowed_servers == ["worker-a"]
    assert tombstone.require_tag == "gpu"
    assert tombstone.max_concurrent_placements == 3
    assert tombstone.id != v1.id
    assert db.get_dispatch_policy_head(project.id, "p").status == "archived"


def test_archive_request_rejects_already_archived_policy(db, audit_path):
    _project(db)
    context = _human_context(db)
    create = request_dispatch_policy_create_approval(
        db,
        "proj1",
        "p",
        allowed_servers=["worker-a"],
        config=_CONFIG,
        audit_path=audit_path,
        request_context=context,
    )
    _approve(db, create.id, audit_path, context)
    archive = request_dispatch_policy_archive_approval(
        db, "proj1", "p", config=_CONFIG, audit_path=audit_path, request_context=context
    )
    _approve(db, archive.id, audit_path, context)

    with pytest.raises(InvalidDispatchPolicyRequestError, match="already archived"):
        request_dispatch_policy_archive_approval(
            db, "proj1", "p", config=_CONFIG, audit_path=audit_path,
            request_context=context,
        )


# ---------------------------------------------------------------------------
# Rollback switch and auto-approval exclusion
# ---------------------------------------------------------------------------


def test_request_functions_fail_closed_when_disabled(db, audit_path):
    _project(db)
    context = _human_context(db)
    disabled = SimpleNamespace(dispatch_policy_v1_enabled=False)

    with pytest.raises(DispatchPolicyAdministrationDisabledError):
        request_dispatch_policy_create_approval(
            db, "proj1", "p", allowed_servers=["worker-a"], config=disabled,
            audit_path=audit_path, request_context=context,
        )
    with pytest.raises(DispatchPolicyAdministrationDisabledError):
        request_dispatch_policy_update_approval(
            db, "proj1", "p", allowed_servers=["worker-a"], config=disabled,
            audit_path=audit_path, request_context=context,
        )
    with pytest.raises(DispatchPolicyAdministrationDisabledError):
        request_dispatch_policy_archive_approval(
            db, "proj1", "p", config=disabled, audit_path=audit_path,
            request_context=context,
        )


def test_approve_fails_closed_when_disabled_even_if_request_time_was_enabled(
    db, audit_path
):
    _project(db)
    context = _human_context(db)
    approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", allowed_servers=["worker-a"], config=_CONFIG,
        audit_path=audit_path, request_context=context,
    )

    with pytest.raises(DispatchPolicyAdministrationDisabledError):
        _approve(db, approval.id, audit_path, context, enabled=False)

    assert db.get_approval(approval.id).status == "pending"


def test_dispatch_policy_kinds_are_never_auto_approved():
    from app.db import VALID_APPROVAL_KINDS

    for kind in (
        "dispatch_policy_create",
        "dispatch_policy_update",
        "dispatch_policy_archive",
    ):
        assert kind in VALID_APPROVAL_KINDS

    for kind in (
        "dispatch_policy_create",
        "dispatch_policy_update",
        "dispatch_policy_archive",
    ):
        result = asyncio.run(
            maybe_auto_approve(
                SimpleNamespace(),
                SimpleNamespace(kind=kind, id=1),
                source="web",
                rules=[{"kind": "any", "action": "approve"}],
            )
        )
        assert result is None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/projects/proj1/dispatch-policies", None),
        (
            "post",
            "/projects/proj1/dispatch-policies/request",
            {"name": "p", "allowed_servers": ["worker-a"]},
        ),
        (
            "post",
            "/projects/proj1/dispatch-policies/p/update-request",
            {"allowed_servers": ["worker-a"]},
        ),
        ("post", "/projects/proj1/dispatch-policies/p/archive-request", None),
    ],
)
def test_dispatch_policy_routes_are_hidden_by_default(api_client, method, path, body):
    client, _ = api_client

    response = (
        getattr(client, method)(path, json=body)
        if body is not None
        else getattr(client, method)(path)
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Dispatch Policy administration is disabled"}


def test_dispatch_policy_api_create_update_archive_round_trip(api_client):
    client, main_module = api_client
    main_module.app_state.config.dispatch_policy_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    create_resp = client.post(
        "/projects/proj1/dispatch-policies/request",
        json={
            "name": "nightly",
            "allowed_servers": ["worker-a", "worker-b"],
            "require_tag": "gpu",
            "max_concurrent_placements": 2,
        },
    )
    assert create_resp.status_code == 200
    approval_id = create_resp.json()["id"]

    approve_resp = client.post(f"/approve/{approval_id}")
    assert approve_resp.status_code == 200

    listed = client.get("/projects/proj1/dispatch-policies")
    assert listed.status_code == 200
    body = listed.json()
    assert body == [
        {
            "id": body[0]["id"],
            "project_id": body[0]["project_id"],
            "project": "proj1",
            "name": "nightly",
            "revision": 1,
            "status": "approved",
            "allowed_servers": ["worker-a", "worker-b"],
            "require_tag": "gpu",
            "run_profile_id": None,
            "dataset_required": False,
            "max_concurrent_placements": 2,
            "valid_until": None,
            "approval_id": approval_id,
            "created_by_actor_id": None,
            "created_at": body[0]["created_at"],
        }
    ]

    update_resp = client.post(
        "/projects/proj1/dispatch-policies/nightly/update-request",
        json={"allowed_servers": ["worker-c"]},
    )
    assert update_resp.status_code == 200
    client.post(f"/approve/{update_resp.json()['id']}")

    listed_after_update = client.get("/projects/proj1/dispatch-policies").json()
    assert len(listed_after_update) == 1
    assert listed_after_update[0]["revision"] == 2
    assert listed_after_update[0]["allowed_servers"] == ["worker-c"]

    archive_resp = client.post(
        "/projects/proj1/dispatch-policies/nightly/archive-request"
    )
    assert archive_resp.status_code == 200
    client.post(f"/approve/{archive_resp.json()['id']}")

    listed_after_archive = client.get("/projects/proj1/dispatch-policies").json()
    assert listed_after_archive[0]["status"] == "archived"
    assert listed_after_archive[0]["revision"] == 3


def test_dispatch_policy_api_rejects_unknown_project(api_client):
    client, main_module = api_client
    main_module.app_state.config.dispatch_policy_v1_enabled = True

    response = client.post(
        "/projects/does-not-exist/dispatch-policies/request",
        json={"name": "p", "allowed_servers": ["worker-a"]},
    )
    assert response.status_code == 404


def test_dispatch_policy_api_enforces_require_tag_length(api_client):
    client, main_module = api_client
    main_module.app_state.config.dispatch_policy_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    response = client.post(
        "/projects/proj1/dispatch-policies/request",
        json={
            "name": "p",
            "allowed_servers": ["worker-a"],
            "require_tag": "x" * 129,
        },
    )
    assert response.status_code == 422


def test_dispatch_policy_api_rejects_empty_allowed_servers(api_client):
    client, main_module = api_client
    main_module.app_state.config.dispatch_policy_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    response = client.post(
        "/projects/proj1/dispatch-policies/request",
        json={"name": "p", "allowed_servers": []},
    )
    assert response.status_code == 400
