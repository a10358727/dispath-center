"""Goal 1 Slice 6 service-account and token lifecycle tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from app import approvals as approvals_module
from app.approvals import (
    ApprovalNotPendingError,
    IdentityAdministrationDisabledError,
    InvalidIdentityAdminRequestError,
    approve,
    maybe_auto_approve,
    request_service_account_create_approval,
    request_service_token_issue_approval,
    request_service_token_revoke_approval,
)
from app.audit import now_iso
from app.authentication import resolve_service_token_context
from app.authorization import Action
from app.authorization_shadow import collect_shadow_evidence
from app.identity import ActorType, RequestContext, verify_secret


def _identity_app_state(enabled: bool = True):
    return SimpleNamespace(
        config=SimpleNamespace(identity_admin_enabled=enabled),
    )


def _human_context(db, name: str = "Ada") -> RequestContext:
    actor = db.insert_actor(
        actor_type=ActorType.HUMAN,
        display_name=name,
        platform_admin=True,
    )
    return RequestContext(actor=actor, authentication_method="session")


def _future_expiry(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _create_service_account(db, audit_path, context, name="automation"):
    request = request_service_account_create_approval(
        db,
        name,
        "CI automation",
        audit_path=audit_path,
        request_context=context,
    )
    result = asyncio.run(
        approve(
            db,
            request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    return result["service_account"]


def _issue_service_token(
    db,
    audit_path,
    context,
    actor_id,
    *,
    label="primary",
    scopes=None,
    expires_at=None,
):
    request = request_service_token_issue_approval(
        db,
        actor_id,
        label=label,
        scopes=[Action.PROJECT_VIEW.value] if scopes is None else scopes,
        expires_at=expires_at or _future_expiry(),
        audit_path=audit_path,
        request_context=context,
    )
    result = asyncio.run(
        approve(
            db,
            request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    return request, result


def _database_text_values(db):
    tables = [
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        rows = db._conn.execute(f'SELECT * FROM "{table}"').fetchall()
        for row in rows:
            for value in row:
                if isinstance(value, bytes):
                    yield value.decode()
                elif isinstance(value, str):
                    yield value


def test_service_account_request_is_canonical_attributed_and_side_effect_free(
    db,
    audit_path,
):
    context = _human_context(db)

    approval = request_service_account_create_approval(
        db,
        "  build-bot  ",
        "  package builder  ",
        audit_path=audit_path,
        request_context=context,
    )

    assert approval.payload == {
        "actor_id": approval.payload["actor_id"],
        "name": "build-bot",
        "description": "package builder",
    }
    assert approval.requester_actor_id == context.actor_id
    assert db.get_actor(approval.payload["actor_id"]) is None
    assert db.list_service_accounts() == []
    assert "token" not in json.dumps(approval.payload).lower()


def test_service_account_create_revalidates_name_and_attributes_decision(
    db,
    audit_path,
):
    requester = _human_context(db, "Requester")
    decider = _human_context(db, "Decider")
    approval = request_service_account_create_approval(
        db,
        "deploy-bot",
        audit_path=audit_path,
        request_context=requester,
    )

    result = asyncio.run(
        approve(
            db,
            approval.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=decider,
        )
    )

    account = result["service_account"]
    actor = db.get_actor(account.actor_id)
    decided = result["approval"]
    assert actor.actor_type is ActorType.SERVICE
    assert actor.display_name == "deploy-bot"
    assert actor.platform_admin is False
    assert account.created_by_actor_id == decider.actor_id
    assert decided.requester_actor_id == requester.actor_id
    assert decided.decision_actor_id == decider.actor_id
    assert decided.decision_mechanism == "manual"

    events = [json.loads(line) for line in open(audit_path, encoding="utf-8")]
    assert events[-1]["action"] == "service_account_create"
    assert events[-1]["actor"] == {
        "id": decider.actor_id,
        "kind": "human",
        "authentication": "session",
    }


def test_service_account_name_is_revalidated_before_approval(db, audit_path):
    context = _human_context(db)
    approval = request_service_account_create_approval(
        db,
        "same-name",
        audit_path=audit_path,
        request_context=context,
    )
    other_actor = db.insert_actor(
        actor_type=ActorType.SERVICE,
        display_name="same-name",
    )
    db.insert_service_account(name="same-name", actor_id=other_actor.id)

    result = asyncio.run(
        approve(
            db,
            approval.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )

    assert result["approval"].status == "rejected"
    assert db.get_actor(approval.payload["actor_id"]) is None
    assert len(db.list_service_accounts()) == 1


def test_scope_validation_is_exact_and_permissionless_tokens_are_allowed(
    db,
    audit_path,
):
    context = _human_context(db)
    account = _create_service_account(db, audit_path, context)

    permissionless = request_service_token_issue_approval(
        db,
        account.actor_id,
        label=None,
        scopes=[],
        expires_at=_future_expiry(),
        audit_path=audit_path,
        request_context=context,
    )
    canonical = request_service_token_issue_approval(
        db,
        account.actor_id,
        label=" duplicate scopes ",
        scopes=[Action.PROJECT_VIEW.value, Action.AUDIT_VIEW.value, Action.PROJECT_VIEW.value],
        expires_at=_future_expiry(),
        audit_path=audit_path,
        request_context=context,
    )

    assert permissionless.payload["scopes"] == []
    assert canonical.payload["label"] == "duplicate scopes"
    assert canonical.payload["scopes"] == [
        Action.AUDIT_VIEW.value,
        Action.PROJECT_VIEW.value,
    ]

    for invalid in (
        ["PROJECT.VIEW"],
        ["project.view", ""],
        ["unknown.action"],
        "project.view",
        None,
    ):
        with pytest.raises(InvalidIdentityAdminRequestError):
            request_service_token_issue_approval(
                db,
                account.actor_id,
                label=None,
                scopes=invalid,
                expires_at=_future_expiry(),
                audit_path=audit_path,
                request_context=context,
            )


@pytest.mark.parametrize(
    "expires_at",
    [
        "",
        "not-a-date",
        "2030-01-01T00:00:00",
        "2000-01-01T00:00:00+00:00",
    ],
)
def test_service_token_expiry_requires_aware_future_timestamp(
    db,
    audit_path,
    expires_at,
):
    context = _human_context(db)
    account = _create_service_account(db, audit_path, context)

    with pytest.raises(InvalidIdentityAdminRequestError):
        request_service_token_issue_approval(
            db,
            account.actor_id,
            label=None,
            scopes=[],
            expires_at=expires_at,
            audit_path=audit_path,
            request_context=context,
        )


def test_raw_service_token_is_returned_once_and_redacted_everywhere_else(
    db,
    audit_path,
    caplog,
):
    context = _human_context(db)
    account = _create_service_account(db, audit_path, context)
    request, result = _issue_service_token(
        db,
        audit_path,
        context,
        account.actor_id,
        label="rotation-1",
        scopes=[Action.PROJECT_VIEW.value, Action.PROJECT_OPERATE.value],
    )

    issued = result["issued_service_token"]
    stored = result["service_token"]
    assert stored.id == issued.id
    assert verify_secret(issued.raw_token, stored.secret_hash)
    assert stored.secret_hash == issued.secret_hash
    assert "raw_token" not in request.payload
    assert "secret_hash" not in request.payload
    assert "token_id" not in request.payload
    assert issued.raw_token not in repr(result)
    assert issued.secret_hash not in repr(result)
    assert issued.raw_token not in "\n".join(_database_text_values(db))

    audit_text = open(audit_path, encoding="utf-8").read()
    assert issued.raw_token not in audit_text
    assert issued.secret_hash not in audit_text
    assert issued.raw_token not in caplog.text
    assert issued.secret_hash not in caplog.text

    with pytest.raises(ApprovalNotPendingError):
        asyncio.run(
            approve(
                db,
                request.id,
                app_state=_identity_app_state(),
                audit_path=audit_path,
                request_context=context,
            )
        )


def test_service_token_expiry_and_revocation_take_effect_immediately(db, audit_path):
    context = _human_context(db)
    account = _create_service_account(db, audit_path, context)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    _, result = _issue_service_token(
        db,
        audit_path,
        context,
        account.actor_id,
        expires_at=expiry.isoformat(),
    )
    issued = result["issued_service_token"]

    assert resolve_service_token_context(
        db,
        issued.raw_token,
        enabled=True,
        now=expiry - timedelta(microseconds=1),
    ) is not None
    assert resolve_service_token_context(
        db,
        issued.raw_token,
        enabled=True,
        now=expiry,
    ) is None

    revoke = request_service_token_revoke_approval(
        db,
        issued.id,
        audit_path=audit_path,
        request_context=context,
    )
    revoke_result = asyncio.run(
        approve(
            db,
            revoke.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    assert revoke_result["service_token"].revoked_at is not None
    assert resolve_service_token_context(
        db,
        issued.raw_token,
        enabled=True,
    ) is None


def test_rotation_issues_and_verifies_new_before_revoking_old(db, audit_path):
    context = _human_context(db)
    account = _create_service_account(db, audit_path, context)
    _, old_result = _issue_service_token(
        db,
        audit_path,
        context,
        account.actor_id,
        label="old",
    )
    _, new_result = _issue_service_token(
        db,
        audit_path,
        context,
        account.actor_id,
        label="new",
    )
    old = old_result["issued_service_token"]
    new = new_result["issued_service_token"]

    assert resolve_service_token_context(db, old.raw_token, enabled=True) is not None
    assert resolve_service_token_context(db, new.raw_token, enabled=True) is not None

    revoke = request_service_token_revoke_approval(
        db,
        old.id,
        audit_path=audit_path,
        request_context=context,
    )
    asyncio.run(
        approve(
            db,
            revoke.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )

    assert resolve_service_token_context(db, old.raw_token, enabled=True) is None
    assert resolve_service_token_context(db, new.raw_token, enabled=True) is not None


def test_token_issue_revalidates_disabled_account_and_elapsed_expiry(
    db,
    audit_path,
    monkeypatch,
):
    context = _human_context(db)
    account = _create_service_account(db, audit_path, context)
    disabled_request = request_service_token_issue_approval(
        db,
        account.actor_id,
        label=None,
        scopes=[],
        expires_at=_future_expiry(),
        audit_path=audit_path,
        request_context=context,
    )
    db.update_actor(account.actor_id, disabled_at=now_iso())

    disabled_result = asyncio.run(
        approve(
            db,
            disabled_request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    assert disabled_result["approval"].status == "rejected"
    assert db.list_service_account_tokens(account.actor_id) == []

    db.update_actor(account.actor_id, disabled_at=None)
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    elapsed_request = request_service_token_issue_approval(
        db,
        account.actor_id,
        label=None,
        scopes=[],
        expires_at=expiry.isoformat(),
        audit_path=audit_path,
        request_context=context,
    )
    monkeypatch.setattr(
        approvals_module,
        "_utc_now",
        lambda: expiry + timedelta(seconds=1),
    )

    elapsed_result = asyncio.run(
        approve(
            db,
            elapsed_request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    assert elapsed_result["approval"].status == "rejected"
    assert db.list_service_account_tokens(account.actor_id) == []


def test_identity_approval_execution_honors_rollback_switch(db, audit_path):
    context = _human_context(db)
    approval = request_service_account_create_approval(
        db,
        "disabled-admin",
        audit_path=audit_path,
        request_context=context,
    )

    with pytest.raises(
        IdentityAdministrationDisabledError,
        match="identity administration is disabled",
    ):
        asyncio.run(
            approve(
                db,
                approval.id,
                app_state=_identity_app_state(enabled=False),
                audit_path=audit_path,
                request_context=context,
            )
        )

    assert db.get_approval(approval.id).status == "pending"
    assert db.get_actor(approval.payload["actor_id"]) is None


def test_service_actor_approval_denial_is_shadow_only(db, audit_path):
    human_context = _human_context(db)
    account = _create_service_account(db, audit_path, human_context)
    _, credential_result = _issue_service_token(
        db,
        audit_path,
        human_context,
        account.actor_id,
        scopes=[Action.APPROVAL_DECIDE.value],
    )
    credential = credential_result["issued_service_token"]
    service_context = resolve_service_token_context(
        db,
        credential.raw_token,
        enabled=True,
    )
    assert service_context is not None

    pending = request_service_token_issue_approval(
        db,
        account.actor_id,
        label="shadow-only",
        scopes=[],
        expires_at=_future_expiry(),
        audit_path=audit_path,
        request_context=human_context,
    )
    evidence = collect_shadow_evidence(
        mode="shadow",
        db=db,
        context=service_context,
        action=Action.APPROVAL_DECIDE,
        resource_kind="approval",
        values={"approval_id": pending.id},
        interface_kind="route",
        interface_name="POST /approve/{approval_id}",
    )

    assert len(evidence) == 1
    assert evidence[0].audit_action == "authorization_shadow_denied"
    assert evidence[0].params["reason"] == "denied_service_action"

    result = asyncio.run(
        approve(
            db,
            pending.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=service_context,
        )
    )
    assert result["approval"].status == "approved"
    assert result["approval"].decision_actor_id == service_context.actor_id
    assert result["service_token"].created_by_actor_id == service_context.actor_id


def test_membership_same_role_and_missing_remove_are_idempotent(db, audit_path):
    context = _human_context(db)
    member = db.insert_actor(actor_type=ActorType.HUMAN, display_name="Member")
    project_id = db.insert_project("member-project", "/tmp/member-project")

    first_request = approvals_module.request_project_membership_upsert_approval(
        db,
        project_id,
        member.id,
        "operator",
        audit_path=audit_path,
        request_context=context,
    )
    first = asyncio.run(
        approve(
            db,
            first_request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )["membership"]

    repeated_request = approvals_module.request_project_membership_upsert_approval(
        db,
        "member-project",
        member.id,
        "operator",
        audit_path=audit_path,
        request_context=context,
    )
    repeated_result = asyncio.run(
        approve(
            db,
            repeated_request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    repeated = repeated_result["membership"]
    assert repeated_result["approval"].status == "approved"
    assert repeated.created_at == first.created_at
    assert repeated.updated_at == first.updated_at
    assert repeated.created_by_actor_id == first.created_by_actor_id

    remove_request = approvals_module.request_project_membership_remove_approval(
        db,
        project_id,
        member.id,
        audit_path=audit_path,
        request_context=context,
    )
    assert db.delete_project_membership(project_id, member.id) is True
    removed = asyncio.run(
        approve(
            db,
            remove_request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    assert removed["approval"].status == "approved"
    assert removed["membership_removed"] is False


def test_membership_approval_revalidates_project_and_actor(db, audit_path):
    context = _human_context(db)
    member = db.insert_actor(actor_type=ActorType.HUMAN, display_name="Member")
    deleted_project_id = db.insert_project("deleted-project", "/tmp/deleted")
    deleted_project_request = (
        approvals_module.request_project_membership_upsert_approval(
            db,
            deleted_project_id,
            member.id,
            "viewer",
            audit_path=audit_path,
            request_context=context,
        )
    )
    db.delete_project("deleted-project")

    deleted_project_result = asyncio.run(
        approve(
            db,
            deleted_project_request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    assert deleted_project_result["approval"].status == "rejected"

    active_project_id = db.insert_project("active-project", "/tmp/active")
    disabled_actor_request = (
        approvals_module.request_project_membership_upsert_approval(
            db,
            active_project_id,
            member.id,
            "viewer",
            audit_path=audit_path,
            request_context=context,
        )
    )
    db.update_actor(member.id, disabled_at=now_iso())

    disabled_actor_result = asyncio.run(
        approve(
            db,
            disabled_actor_request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    assert disabled_actor_result["approval"].status == "rejected"
    assert db.get_project_membership(active_project_id, member.id) is None


def test_revoke_is_idempotent_when_token_changes_while_pending(db, audit_path):
    context = _human_context(db)
    account = _create_service_account(db, audit_path, context)
    _, issue_result = _issue_service_token(
        db,
        audit_path,
        context,
        account.actor_id,
    )
    token = issue_result["service_token"]
    revoke_request = request_service_token_revoke_approval(
        db,
        token.id,
        audit_path=audit_path,
        request_context=context,
    )
    assert db.revoke_service_account_token(token.id) is True

    result = asyncio.run(
        approve(
            db,
            revoke_request.id,
            app_state=_identity_app_state(),
            audit_path=audit_path,
            request_context=context,
        )
    )
    assert result["approval"].status == "approved"
    assert result["approval"].note == "service token was already revoked"
    assert result["service_token"].revoked_at is not None


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (
            "service_account_create",
            {"actor_id": "00000000-0000-0000-0000-000000000010", "name": "bot", "description": None},
        ),
        (
            "service_token_issue",
            {
                "service_account_actor_id": "00000000-0000-0000-0000-000000000011",
                "label": None,
                "scopes": [],
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        ),
        ("service_token_revoke", {"token_id": "00000000-0000-0000-0000-000000000012"}),
        (
            "project_membership_upsert",
            {
                "project_id": "00000000-0000-0000-0000-000000000013",
                "actor_id": "00000000-0000-0000-0000-000000000014",
                "role": "viewer",
            },
        ),
        (
            "project_membership_remove",
            {
                "project_id": "00000000-0000-0000-0000-000000000013",
                "actor_id": "00000000-0000-0000-0000-000000000014",
            },
        ),
    ],
)
def test_identity_approval_kinds_are_never_autoapproved(db, kind, payload):
    approval_id = db.insert_approval(kind, payload)
    approval = db.get_approval(approval_id)

    result = asyncio.run(
        maybe_auto_approve(
            db,
            approval,
            source="web",
            rules=[{"source": "any", "kind": "any"}],
            app_state=_identity_app_state(),
        )
    )

    assert result is None
    assert db.get_approval(approval_id).status == "pending"
