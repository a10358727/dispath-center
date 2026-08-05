"""D5 Run Profile v1 tests (docs/DECISIONS.md, docs/AI_ENGINEERING_DECISION_GATE.md).

Covers: additive immutable-revision schema, approval-gated create/update/
archive lifecycle, auto-approval exclusion, the `RUN_PROFILE_V1_ENABLED`
rollback switch, and the API surface. No SSH/worker/scheduler behavior is
touched by this slice.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.approvals import (
    IdentityTargetNotFoundError,
    InvalidRunProfileRequestError,
    RunProfileAdministrationDisabledError,
    approve,
    maybe_auto_approve,
    request_run_profile_archive_approval,
    request_run_profile_create_approval,
    request_run_profile_update_approval,
)
from app.identity import ActorType, RequestContext


def _run_profile_app_state(enabled: bool = True):
    return SimpleNamespace(config=SimpleNamespace(run_profile_v1_enabled=enabled))


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
            app_state=_run_profile_app_state(enabled),
            audit_path=audit_path,
            request_context=context,
        )
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_request_and_approval_produce_the_first_revision(db, audit_path):
    project = _project(db)
    context = _human_context(db)

    approval = request_run_profile_create_approval(
        db,
        "proj1",
        "smoke-test",
        command="pytest -q",
        setup_cmd="pip install -r requirements.txt",
        require_tag="gpu",
        config=SimpleNamespace(run_profile_v1_enabled=True),
        audit_path=audit_path,
        request_context=context,
    )
    assert approval.kind == "run_profile_create"
    assert approval.status == "pending"
    assert approval.payload == {
        "project_id": project.id,
        "project_name": "proj1",
        "name": "smoke-test",
        "command": "pytest -q",
        "setup_cmd": "pip install -r requirements.txt",
        "require_tag": "gpu",
    }

    result = _approve(db, approval.id, audit_path, context)

    profile = result["run_profile"]
    assert profile.project_id == project.id
    assert profile.name == "smoke-test"
    assert profile.revision == 1
    assert profile.status == "approved"
    assert profile.supersedes_id is None
    assert profile.approval_id == approval.id
    assert result["approval"].status == "approved"

    events = db.list_durable_audit_events(limit=20)
    mutation = next(
        event
        for event in events
        if event["action"] == "run_profile_revision_created"
    )
    assert mutation["result"] == "approved"
    assert mutation["resource_type"] == "run_profile"
    assert mutation["resource_id"] == profile.id
    assert mutation["approval_id"] == approval.id
    assert mutation["params"] == {
        "name": "smoke-test",
        "operation": "create",
        "project_id": project.id,
        "revision": 1,
        "supersedes_id": None,
    }
    assert sum(
        event["action"] == "approval_decided" for event in events
    ) == 1

    head = db.get_run_profile_head(project.id, "smoke-test")
    assert head == profile


def test_create_request_rejects_duplicate_name(db, audit_path):
    _project(db)
    context = _human_context(db)
    config = SimpleNamespace(run_profile_v1_enabled=True)
    approval = request_run_profile_create_approval(
        db, "proj1", "dup", config=config, audit_path=audit_path, request_context=context
    )
    _approve(db, approval.id, audit_path, context)

    with pytest.raises(InvalidRunProfileRequestError, match="already exists"):
        request_run_profile_create_approval(
            db, "proj1", "dup", config=config, audit_path=audit_path, request_context=context
        )


def test_create_request_rejects_unknown_project(db, audit_path):
    context = _human_context(db)
    with pytest.raises(IdentityTargetNotFoundError):
        request_run_profile_create_approval(
            db,
            "no-such-project",
            "profile-a",
            config=SimpleNamespace(run_profile_v1_enabled=True),
            audit_path=audit_path,
            request_context=context,
        )


def test_create_request_rejects_invalid_name_charset(db, audit_path):
    _project(db)
    context = _human_context(db)
    with pytest.raises(Exception):
        request_run_profile_create_approval(
            db,
            "proj1",
            "not a valid name!",
            config=SimpleNamespace(run_profile_v1_enabled=True),
            audit_path=audit_path,
            request_context=context,
        )


def test_create_approval_rejects_concurrently_created_name(db, audit_path):
    project = _project(db)
    context = _human_context(db)
    config = SimpleNamespace(run_profile_v1_enabled=True)

    approval = request_run_profile_create_approval(
        db, "proj1", "race", config=config, audit_path=audit_path, request_context=context
    )
    # Someone else's request for the same name is approved first.
    other = request_run_profile_create_approval(
        db, "proj1", "race-2", config=config, audit_path=audit_path, request_context=context
    )
    db.insert_run_profile_revision(
        project_id=project.id,
        project_name="proj1",
        name="race",
        status="approved",
        command=None,
        setup_cmd=None,
        require_tag=None,
        supersedes_id=None,
        approval_id=other.id,
        created_by_actor_id=None,
    )

    result = _approve(db, approval.id, audit_path, context)
    assert result["approval"].status == "rejected"
    assert "already exists" in result["approval"].note


def test_run_profile_revision_and_decision_roll_back_together_on_audit_failure(
    db, audit_path, monkeypatch
):
    project = _project(db)
    context = _human_context(db)
    approval = request_run_profile_create_approval(
        db,
        "proj1",
        "rollback",
        config=SimpleNamespace(run_profile_v1_enabled=True),
        audit_path=audit_path,
        request_context=context,
    )
    original = db.append_durable_audit_event_in_transaction

    def fail_revision(*args, **kwargs):
        if kwargs.get("action") == "run_profile_revision_created":
            raise RuntimeError("revision audit fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "append_durable_audit_event_in_transaction", fail_revision)
    with pytest.raises(RuntimeError, match="revision audit fault"):
        _approve(db, approval.id, audit_path, context)

    assert db.get_run_profile_head(project.id, "rollback") is None
    pending = db.get_approval(approval.id)
    assert pending is not None and pending.status == "pending"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_creates_a_new_revision_superseding_the_head(db, audit_path):
    project = _project(db)
    context = _human_context(db)
    config = SimpleNamespace(run_profile_v1_enabled=True)

    create = request_run_profile_create_approval(
        db, "proj1", "p", command="v1", config=config, audit_path=audit_path,
        request_context=context,
    )
    v1 = _approve(db, create.id, audit_path, context)["run_profile"]

    update = request_run_profile_update_approval(
        db, "proj1", "p", command="v2", config=config, audit_path=audit_path,
        request_context=context,
    )
    assert update.payload["based_on_revision"] == 1
    v2_result = _approve(db, update.id, audit_path, context)
    v2 = v2_result["run_profile"]

    assert v2.revision == 2
    assert v2.command == "v2"
    assert v2.supersedes_id == v1.id
    assert v2.status == "approved"

    # v1 remains unchanged and readable; head now points to v2.
    revisions = db.list_run_profile_revisions(project.id, "p")
    assert [r.revision for r in revisions] == [2, 1]
    assert revisions[1].command == "v1"
    assert db.get_run_profile_head(project.id, "p") == v2


def test_update_request_rejects_missing_profile(db, audit_path):
    _project(db)
    context = _human_context(db)
    with pytest.raises(IdentityTargetNotFoundError):
        request_run_profile_update_approval(
            db, "proj1", "nope", config=SimpleNamespace(run_profile_v1_enabled=True),
            audit_path=audit_path, request_context=context,
        )


def test_update_approval_rejects_stale_based_on_revision(db, audit_path):
    project = _project(db)
    context = _human_context(db)
    config = SimpleNamespace(run_profile_v1_enabled=True)
    create = request_run_profile_create_approval(
        db, "proj1", "p", config=config, audit_path=audit_path, request_context=context
    )
    _approve(db, create.id, audit_path, context)

    stale_update = request_run_profile_update_approval(
        db, "proj1", "p", command="stale", config=config, audit_path=audit_path,
        request_context=context,
    )
    # A second update is requested and approved first, moving the head.
    fresh_update = request_run_profile_update_approval(
        db, "proj1", "p", command="fresh", config=config, audit_path=audit_path,
        request_context=context,
    )
    _approve(db, fresh_update.id, audit_path, context)

    result = _approve(db, stale_update.id, audit_path, context)
    assert result["approval"].status == "rejected"
    assert "changed since" in result["approval"].note
    # The stale approval must not have created a phantom revision.
    assert [r.command for r in db.list_run_profile_revisions(project.id, "p")] == [
        "fresh",
        None,
    ]


def test_update_request_rejects_archived_profile(db, audit_path):
    _project(db)
    context = _human_context(db)
    config = SimpleNamespace(run_profile_v1_enabled=True)
    create = request_run_profile_create_approval(
        db, "proj1", "p", config=config, audit_path=audit_path, request_context=context
    )
    _approve(db, create.id, audit_path, context)
    archive = request_run_profile_archive_approval(
        db, "proj1", "p", config=config, audit_path=audit_path, request_context=context
    )
    _approve(db, archive.id, audit_path, context)

    with pytest.raises(InvalidRunProfileRequestError, match="archived"):
        request_run_profile_update_approval(
            db, "proj1", "p", config=config, audit_path=audit_path,
            request_context=context,
        )


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def test_archive_creates_a_tombstone_revision_preserving_content(db, audit_path):
    project = _project(db)
    context = _human_context(db)
    config = SimpleNamespace(run_profile_v1_enabled=True)
    create = request_run_profile_create_approval(
        db, "proj1", "p", command="do-work", require_tag="gpu", config=config,
        audit_path=audit_path, request_context=context,
    )
    v1 = _approve(db, create.id, audit_path, context)["run_profile"]

    archive = request_run_profile_archive_approval(
        db, "proj1", "p", config=config, audit_path=audit_path, request_context=context
    )
    result = _approve(db, archive.id, audit_path, context)
    tombstone = result["run_profile"]

    assert tombstone.revision == 2
    assert tombstone.status == "archived"
    assert tombstone.command == "do-work"
    assert tombstone.require_tag == "gpu"
    assert tombstone.supersedes_id == v1.id
    assert db.get_run_profile_head(project.id, "p").status == "archived"


def test_archive_request_rejects_already_archived_profile(db, audit_path):
    _project(db)
    context = _human_context(db)
    config = SimpleNamespace(run_profile_v1_enabled=True)
    create = request_run_profile_create_approval(
        db, "proj1", "p", config=config, audit_path=audit_path, request_context=context
    )
    _approve(db, create.id, audit_path, context)
    archive = request_run_profile_archive_approval(
        db, "proj1", "p", config=config, audit_path=audit_path, request_context=context
    )
    _approve(db, archive.id, audit_path, context)

    with pytest.raises(InvalidRunProfileRequestError, match="already archived"):
        request_run_profile_archive_approval(
            db, "proj1", "p", config=config, audit_path=audit_path,
            request_context=context,
        )


# ---------------------------------------------------------------------------
# Rollback switch and auto-approval exclusion
# ---------------------------------------------------------------------------


def test_request_functions_fail_closed_when_disabled(db, audit_path):
    _project(db)
    context = _human_context(db)
    disabled = SimpleNamespace(run_profile_v1_enabled=False)

    with pytest.raises(RunProfileAdministrationDisabledError):
        request_run_profile_create_approval(
            db, "proj1", "p", config=disabled, audit_path=audit_path,
            request_context=context,
        )
    with pytest.raises(RunProfileAdministrationDisabledError):
        request_run_profile_update_approval(
            db, "proj1", "p", config=disabled, audit_path=audit_path,
            request_context=context,
        )
    with pytest.raises(RunProfileAdministrationDisabledError):
        request_run_profile_archive_approval(
            db, "proj1", "p", config=disabled, audit_path=audit_path,
            request_context=context,
        )


def test_approve_fails_closed_when_disabled_even_if_request_time_was_enabled(
    db, audit_path
):
    _project(db)
    context = _human_context(db)
    approval = request_run_profile_create_approval(
        db, "proj1", "p", config=SimpleNamespace(run_profile_v1_enabled=True),
        audit_path=audit_path, request_context=context,
    )

    with pytest.raises(RunProfileAdministrationDisabledError):
        _approve(db, approval.id, audit_path, context, enabled=False)

    # Never silently approved; still pending for a retry once re-enabled.
    assert db.get_approval(approval.id).status == "pending"


def test_run_profile_kinds_are_never_auto_approved():
    from app.db import VALID_APPROVAL_KINDS

    for kind in ("run_profile_create", "run_profile_update", "run_profile_archive"):
        assert kind in VALID_APPROVAL_KINDS

    result = asyncio.run(
        maybe_auto_approve(
            SimpleNamespace(),
            SimpleNamespace(kind="run_profile_create", id=1),
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
        ("get", "/projects/proj1/run-profiles", None),
        ("post", "/projects/proj1/run-profiles/request", {"name": "p"}),
        (
            "post",
            "/projects/proj1/run-profiles/p/update-request",
            {"command": "x"},
        ),
        ("post", "/projects/proj1/run-profiles/p/archive-request", None),
    ],
)
def test_run_profile_routes_are_hidden_by_default(api_client, method, path, body):
    client, _ = api_client

    response = (
        getattr(client, method)(path, json=body)
        if body is not None
        else getattr(client, method)(path)
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Run Profile administration is disabled"}


def test_run_profile_api_create_update_archive_round_trip(api_client):
    client, main_module = api_client
    main_module.app_state.config.run_profile_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    create_resp = client.post(
        "/projects/proj1/run-profiles/request",
        json={"name": "nightly", "command": "make test", "require_tag": "gpu"},
    )
    assert create_resp.status_code == 200
    approval_id = create_resp.json()["id"]

    approve_resp = client.post(f"/approve/{approval_id}")
    assert approve_resp.status_code == 200

    listed = client.get("/projects/proj1/run-profiles")
    assert listed.status_code == 200
    assert listed.json() == [
        {
            "id": listed.json()[0]["id"],
            "project_id": listed.json()[0]["project_id"],
            "project": "proj1",
            "name": "nightly",
            "revision": 1,
            "status": "approved",
            "command": "make test",
            "setup_cmd": None,
            "require_tag": "gpu",
            "supersedes_id": None,
            "approval_id": approval_id,
            "created_by_actor_id": None,
            "created_at": listed.json()[0]["created_at"],
        }
    ]

    update_resp = client.post(
        "/projects/proj1/run-profiles/nightly/update-request",
        json={"command": "make test-fast"},
    )
    assert update_resp.status_code == 200
    client.post(f"/approve/{update_resp.json()['id']}")

    listed_after_update = client.get("/projects/proj1/run-profiles").json()
    assert len(listed_after_update) == 1
    assert listed_after_update[0]["revision"] == 2
    assert listed_after_update[0]["command"] == "make test-fast"

    archive_resp = client.post(
        "/projects/proj1/run-profiles/nightly/archive-request"
    )
    assert archive_resp.status_code == 200
    client.post(f"/approve/{archive_resp.json()['id']}")

    listed_after_archive = client.get("/projects/proj1/run-profiles").json()
    assert listed_after_archive[0]["status"] == "archived"
    assert listed_after_archive[0]["revision"] == 3


def test_run_profile_api_rejects_unknown_project(api_client):
    client, main_module = api_client
    main_module.app_state.config.run_profile_v1_enabled = True

    response = client.post(
        "/projects/does-not-exist/run-profiles/request", json={"name": "p"}
    )
    assert response.status_code == 404


def test_run_profile_api_enforces_require_tag_length(api_client):
    client, main_module = api_client
    main_module.app_state.config.run_profile_v1_enabled = True
    main_module.app_state.db.insert_project("proj1", "https://example.invalid/p.git")

    response = client.post(
        "/projects/proj1/run-profiles/request",
        json={"name": "p", "require_tag": "x" * 129},
    )
    assert response.status_code == 422
