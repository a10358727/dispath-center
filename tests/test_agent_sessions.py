"""DG-AGENT-SESSION-V1 P1: AgentSession domain + `agent_session_open`
approval kind (docs/DECISIONS.md 2026-08-24).

Domain-level tests (`db`/`audit_path` fixtures) exercise migration v11, the
request/approve validation chain, and the DB helpers directly — the same
`SimpleNamespace(config=...)` `app_state` convention used in
`tests/test_engineering_tasks.py`. Route-level tests use `api_client`
(`tests/conftest.py`), flags flipped after the fact on
`main_module.app_state.config.*` (same convention as
`tests/test_project_conversation.py`). No turn execution, no SSH — this
packet is pure DB/approval plumbing."""

from __future__ import annotations

import uuid
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.approvals import (
    InvalidAgentSessionRequestError,
    approve,
    maybe_auto_approve,
    request_agent_session_open_approval,
)
from app.config import AppConfig, ServerConfig
from app.db import (
    AGENT_SESSION_MIGRATION_CHECKSUM,
    AGENT_SESSION_MIGRATION_NAME,
    AGENT_SESSION_MIGRATION_VERSION,
    Database,
    apply_agent_session_migration,
)
from app.migrations import Migration

COMMIT = "a" * 40


def _server() -> ServerConfig:
    return ServerConfig(
        name="server-a",
        host="10.0.0.1",
        user="train",
        key="~/.ssh/id_rsa",
        port=32221,
        enabled=True,
    )


def _config(**overrides) -> AppConfig:
    base = dict(
        servers=[_server()],
        agent_session_v1_enabled=True,
    )
    base.update(overrides)
    return AppConfig(**base)


def _runner_id(db: Database) -> str:
    """The enrolled runner agent on server-a (INV-AGENT-1), enrolled on first use."""
    for runner in db.list_agent_runners(server_name="server-a"):
        if runner.is_active:
            return runner.id
    approval_id = db.insert_approval(kind="agent_runner_enroll", payload={"server": "server-a"})
    return db.apply_agent_runner_enroll_decision(
        approval_id=approval_id,
        runner_id=str(uuid.uuid4()),
        server_name="server-a",
        secret_hash="0" * 64,
        decision_actor_id=None,
        decision_actor_kind=None,
        decision_mechanism="human",
        approval_note="test",
    ).id


def _project_with_version(db: Database, name: str = "proj1"):
    db.insert_project(name, f"https://example.invalid/{name}.git")
    return db.get_or_create_project_version(name, COMMIT, git_ref="main")


# ---------------------------------------------------------------------------
# Migration v11 dual-track: version/name/checksum pinned, purely additive.
# ---------------------------------------------------------------------------


def test_migration_v11_is_registered_and_source_pinned(db):
    # DG-AGENT-SESSION-V1 P2 (migration 12) is now checked in above this
    # one — `CURRENT_SCHEMA_VERSION` is no longer 11, so that assertion
    # moved to `tests/test_migrations.py`; this test stays scoped to
    # migration 11's own registered, source-pinned properties.
    assert AGENT_SESSION_MIGRATION_VERSION == 11
    record = db._conn.execute(
        "SELECT name, checksum, content_checksum FROM schema_migrations WHERE version = 11"
    ).fetchone()
    assert record["name"] == AGENT_SESSION_MIGRATION_NAME == "agent_sessions"
    source_checksum = Migration(
        11, AGENT_SESSION_MIGRATION_NAME, apply_agent_session_migration
    ).content_checksum()
    assert record["checksum"] == record["content_checksum"] == source_checksum
    assert source_checksum == AGENT_SESSION_MIGRATION_CHECKSUM

    columns = [row["name"] for row in db._conn.execute("PRAGMA table_info(agent_sessions)")]
    assert columns == [
        "id",
        "project_id",
        "conversation_id",
        "provider_id",
        "workspace_branch",
        "base_version_id",
        "cli_session_id",
        "status",
        "turn_count",
        "max_turns",
        "turn_timeout_sec",
        "created_at",
        "last_used_at",
        "closed_at",
        # DG-AGENT-SESSION-V1 P2, migration 12: additive columns appended
        # after migration 11's original set (see
        # `tests/test_migrations.py::test_representative_v11_upgrade_installs_active_turn_tracking_without_backfill`).
        "active_turn_no",
        "active_turn_started_at",
    ]
    indexes = {
        row["name"]: row["unique"]
        for row in db._conn.execute("PRAGMA index_list(agent_sessions)")
    }
    assert indexes["idx_agent_sessions_one_open_per_project"] == 1
    assert indexes["idx_agent_sessions_project_created"] == 0


def test_one_active_or_pending_session_per_project_db_constraint(db):
    """Partial unique index is the final defense (INV-APPROVAL-3 precedent):
    even bypassing the domain layer with raw SQL, a second open row for the
    same project cannot exist."""

    project = _project_with_version(db)
    conversation = db.get_or_create_project_conversation("proj1")
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_sessions
                (id, project_id, conversation_id, provider_id, workspace_branch,
                 base_version_id, status, turn_count, max_turns, turn_timeout_sec,
                 created_at, last_used_at)
            VALUES ('11111111-1111-1111-1111-111111111111', ?, ?, 'claude-code',
                    'ai-session-1', ?, 'active', 0, 200, 600, '2026-08-24T00:00:00Z',
                    '2026-08-24T00:00:00Z')
            """,
            (project.project_id, conversation.id, project.id),
        )
        with pytest.raises(sqlite3.IntegrityError):
            cur.execute(
                """
                INSERT INTO agent_sessions
                    (id, project_id, conversation_id, provider_id, workspace_branch,
                     base_version_id, status, turn_count, max_turns, turn_timeout_sec,
                     created_at, last_used_at)
                VALUES ('22222222-2222-2222-2222-222222222222', ?, ?, 'claude-code',
                        'ai-session-2', ?, 'pending', 0, 200, 600, '2026-08-24T00:00:01Z',
                        '2026-08-24T00:00:01Z')
                """,
                (project.project_id, conversation.id, project.id),
            )


# ---------------------------------------------------------------------------
# request_agent_session_open_approval(, runner_id=_runner_id(db)): validation
# ---------------------------------------------------------------------------


def test_request_rejects_when_flag_disabled(db, audit_path):
    version = _project_with_version(db)
    config = _config(agent_session_v1_enabled=False)
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_open_approval(
            db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))
    assert db.list_approvals(kind="agent_session_open") == []


def test_request_rejects_unknown_project(db, audit_path):
    config = _config()
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_open_approval(
            db, "does-not-exist", base_version_id="nope", config=config, audit_path=audit_path, runner_id=_runner_id(db))
    assert db.list_approvals(kind="agent_session_open") == []


def test_request_rejects_unknown_or_foreign_version(db, audit_path):
    _project_with_version(db, "proj1")
    other_version = _project_with_version(db, "proj2")
    config = _config()

    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_open_approval(
            db, "proj1", base_version_id="not-a-real-id", config=config, audit_path=audit_path, runner_id=_runner_id(db))
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_open_approval(
            db, "proj1", base_version_id=other_version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))
    assert db.list_approvals(kind="agent_session_open") == []


def test_request_rejects_without_runner_id(db, audit_path):
    version = _project_with_version(db)
    config = _config()
    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_open_approval(
            db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path)
    assert db.list_approvals(kind="agent_session_open") == []


def test_request_rejects_duplicate_open_session(db, audit_path):
    version = _project_with_version(db)
    config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))
    assert approval.status == "pending"

    with pytest.raises(InvalidAgentSessionRequestError):
        request_agent_session_open_approval(
            db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))


def test_request_creates_pending_approval_with_expected_payload(db, audit_path):
    version = _project_with_version(db)
    config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))
    assert approval.kind == "agent_session_open"
    assert approval.status == "pending"
    assert approval.payload["project"] == "proj1"
    assert approval.payload["base_version_id"] == version.id
    assert approval.payload["agent_provider_id"] == "claude-code"
    assert approval.payload["max_turns"] == 200
    assert approval.payload["turn_timeout_sec"] == 600
    assert approval.payload["workspace_branch"].startswith("ai-session-")


# ---------------------------------------------------------------------------
# approve(): happy path, atomic session creation, INV-APPROVAL-3 revalidation
# ---------------------------------------------------------------------------


def test_approve_creates_active_session_atomically(db, audit_path):
    version = _project_with_version(db)
    config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))

    result = asyncio.run(
        approve(db, approval.id, app_state=SimpleNamespace(config=config), audit_path=audit_path)
    )

    assert result["approval"].status == "approved"
    session = result["agent_session"]
    assert session.status == "active"
    assert session.project_id == version.project_id
    assert session.base_version_id == version.id
    assert session.provider_id == "claude-code"
    assert session.max_turns == 200
    assert session.turn_timeout_sec == 600
    assert session.turn_count == 0

    fetched = db.get_active_or_pending_agent_session(version.project_id)
    assert fetched is not None and fetched.id == session.id


def test_approve_revalidates_project_still_resolves_by_name(db, audit_path):
    """INV-APPROVAL-3: the approve-time re-check must not trust the
    request-time payload snapshot. The project row itself cannot be deleted
    while a ProjectVersion references it (FK RESTRICT) — a renamed project is
    the realistic way the payload's `project` name can stop resolving between
    request and approve."""

    version = _project_with_version(db)
    config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))

    with db.cursor() as cur:
        cur.execute(
            "UPDATE projects SET name = ? WHERE id = ?", ("proj1-renamed", version.project_id)
        )

    result = asyncio.run(
        approve(db, approval.id, app_state=SimpleNamespace(config=config), audit_path=audit_path)
    )
    assert result["approval"].status == "rejected"
    assert db.get_active_or_pending_agent_session(version.project_id) is None


def test_approve_no_ops_when_a_concurrent_session_won_the_race(db, audit_path):
    """A second request for the same project cannot itself be created while
    one is pending (`InvalidAgentSessionRequestError` at request time), so the
    race is exercised directly at the DB layer: another session is created
    for the project between request and approve, and the approve-time
    revalidation inside `apply_agent_session_open_decision()` must reject the
    stale approval rather than silently creating a second open session."""

    version = _project_with_version(db)
    config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))

    conversation = db.get_or_create_project_conversation("proj1")
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_sessions
                (id, project_id, conversation_id, provider_id, workspace_branch,
                 base_version_id, status, turn_count, max_turns, turn_timeout_sec,
                 created_at, last_used_at)
            VALUES ('33333333-3333-3333-3333-333333333333', ?, ?, 'claude-code',
                    'ai-session-raced', ?, 'active', 0, 200, 600,
                    '2026-08-24T00:00:00Z', '2026-08-24T00:00:00Z')
            """,
            (version.project_id, conversation.id, version.id),
        )

    result = asyncio.run(
        approve(db, approval.id, app_state=SimpleNamespace(config=config), audit_path=audit_path)
    )

    # Clean reject (INV-APPROVAL-3 precedent, same as `node_enroll`'s
    # already-active-node check): the raced approval is rejected, not
    # silently approved, and no second session was created alongside the one
    # that won the race.
    assert result["approval"].status == "rejected"
    sessions = db.list_agent_sessions(version.project_id, limit=10)
    assert len(sessions) == 1
    assert sessions[0].id == "33333333-3333-3333-3333-333333333333"


def test_approve_rejects_when_runner_missing_at_decision_time(db, audit_path):
    version = _project_with_version(db)
    request_config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=request_config, audit_path=audit_path, runner_id=_runner_id(db))

    #: the hosting runner is revoked between request and decision
    revoke_id = db.insert_approval(kind="agent_runner_revoke", payload={"runner_id": _runner_id(db)})
    db.apply_agent_runner_revoke_decision(
        approval_id=revoke_id,
        runner_id=_runner_id(db),
        decision_actor_id=None,
        decision_actor_kind=None,
        decision_mechanism="human",
        approval_note="test",
    )
    decision_config = _config()
    result = asyncio.run(
        approve(
            db,
            approval.id,
            app_state=SimpleNamespace(config=decision_config),
            audit_path=audit_path,
        )
    )
    assert result["approval"].status == "rejected"
    assert db.get_active_or_pending_agent_session(version.project_id) is None


# ---------------------------------------------------------------------------
# Never auto-approved (INV-APPROVAL-4 precedent pin).
# ---------------------------------------------------------------------------


def test_agent_session_open_is_never_auto_approved(db, audit_path):
    version = _project_with_version(db)
    config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))
    rules = [{"kind": "any"}]
    result = asyncio.run(
        maybe_auto_approve(db, approval, source="api", rules=rules, audit_path=audit_path)
    )
    assert result is None
    assert db.get_approval(approval.id).status == "pending"


# ---------------------------------------------------------------------------
# close_agent_session() + lazy 7-day idle expiry.
# ---------------------------------------------------------------------------


def test_close_agent_session_is_idempotent(db, audit_path):
    version = _project_with_version(db)
    config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))
    result = asyncio.run(
        approve(db, approval.id, app_state=SimpleNamespace(config=config), audit_path=audit_path)
    )
    session = result["agent_session"]

    first_close = db.close_agent_session(session.id)
    assert first_close.status == "closed"
    assert first_close.closed_at is not None

    second_close = db.close_agent_session(session.id)
    assert second_close.status == "closed"
    assert second_close.closed_at == first_close.closed_at

    assert db.get_active_or_pending_agent_session(version.project_id) is None


def test_close_agent_session_unknown_id_returns_none(db):
    assert db.close_agent_session("does-not-exist") is None


def test_lazy_idle_expiry_closes_active_session_older_than_seven_days(db, audit_path):
    version = _project_with_version(db)
    config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))
    result = asyncio.run(
        approve(db, approval.id, app_state=SimpleNamespace(config=config), audit_path=audit_path)
    )
    session = result["agent_session"]

    stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agent_sessions SET last_used_at = ? WHERE id = ?", (stale, session.id)
        )

    # No background loop: the idle-close happens lazily on the next read.
    current = db.get_active_or_pending_agent_session(version.project_id)
    assert current is None

    expired = db.get_agent_session(session.id)
    assert expired.status == "closed"
    assert expired.closed_at is not None


def test_lazy_idle_expiry_leaves_recently_used_sessions_active(db, audit_path):
    version = _project_with_version(db)
    config = _config()
    approval = request_agent_session_open_approval(
        db, "proj1", base_version_id=version.id, config=config, audit_path=audit_path, runner_id=_runner_id(db))
    result = asyncio.run(
        approve(db, approval.id, app_state=SimpleNamespace(config=config), audit_path=audit_path)
    )
    session = result["agent_session"]

    current = db.get_active_or_pending_agent_session(version.project_id)
    assert current is not None
    assert current.status == "active"
    assert current.id == session.id


# ---------------------------------------------------------------------------
# HTTP routes: flag gating, project-scoped open-request, list, close.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("legacy_posture")
def test_routes_404_when_flag_disabled(api_client):
    client, _main = api_client

    assert client.get("/projects/proj1/agent-sessions").status_code == 404
    assert (
        client.post(
            "/projects/proj1/agent-sessions/open-request",
            json={"base_version_id": "whatever"},
        ).status_code
        == 404
    )
    assert client.post("/agent-sessions/some-id/close").status_code == 404


def test_close_endpoint_unknown_session_404(api_client):
    client, main_module = api_client
    main_module.app_state.config.agent_session_v1_enabled = True

    resp = client.post("/agent-sessions/does-not-exist/close")
    assert resp.status_code == 404
