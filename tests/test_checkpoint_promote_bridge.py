"""整頓 U3: the v2 decision response surfaces the checkpoint→promote bridge id.

`approve()` already returns `bridge_engineering_task_id` for an approved
`agent_session_checkpoint`; the compatibility decision route now forwards it
as `engineering_task_id` so the Studio can offer the separate, human-only
`engineering_task_promote` step without parsing the approval `note`.
"""

from app.audit import now_iso
from app.identity import ActorType, generate_session_token
from dispatch_center.api.routers import compatibility_approvals_v2 as compat

ADMIN_ID = "20000000-0000-0000-0000-0000000000a1"
REQUESTER_ID = "20000000-0000-0000-0000-0000000000a2"


def _login(client, main_module, actor_id: str) -> None:
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor_id,
        oidc_identity_id=None,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.set(main_module.app_state.config.session_cookie_name, issued.raw_token)


def test_checkpoint_decision_response_carries_the_bridge_engineering_task_id(api_client, monkeypatch):
    client, main_module = api_client
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.authorization_mode = "enforce"
    database = main_module.app_state.db
    database.insert_actor(actor_id=ADMIN_ID, actor_type=ActorType.HUMAN, display_name="Admin", platform_admin=True)
    database.insert_actor(actor_id=REQUESTER_ID, actor_type=ActorType.HUMAN, display_name="Requester", platform_admin=False)
    project_id = database.insert_project("demo", "/tmp/demo")
    approval_id = database.insert_approval(
        "agent_session_checkpoint",
        #: `app.authorization` resolves this kind's project through `project_name`.
        {"session_id": "sess-1", "project_name": "demo", "project_id": project_id},
        requester_actor_id=REQUESTER_ID,
    )
    _login(client, main_module, ADMIN_ID)

    seen: dict[str, object] = {}

    async def fake_approve(db, approval_id_, **kwargs):
        # The real approve() runs the bundle pipeline over SSH; here only the
        # response plumbing is under test, so mark the row decided directly.
        seen["approved_by"] = kwargs.get("approved_by")
        db.update_approval(
            approval_id_,
            status="approved",
            decided_at=now_iso(),
            note="checkpoint ok",
            decision_actor_id=ADMIN_ID,
            decision_mechanism="manual",
        )
        return {"approval": db.get_approval(approval_id_), "bridge_engineering_task_id": "task-bridge-1"}

    monkeypatch.setattr(compat.approvals_module, "approve", fake_approve)

    detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert detail.status_code == 200
    assert detail.json()["title"] == "存檔工作區變更"
    decided = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve"},
        headers={
            "Idempotency-Key": "checkpoint-bridge-decision",
            "X-Approval-Payload-Digest": detail.json()["payload_digest"],
        },
    )
    assert decided.status_code == 202, decided.text
    assert decided.json()["status"] == "approved"
    assert decided.json()["engineering_task_id"] == "task-bridge-1"
    assert seen["approved_by"] == "human"

    # A decision without a bridge task never invents the field.
    other = database.insert_approval("server_disable", {"name": "x"}, requester_actor_id=REQUESTER_ID)

    async def fake_plain(db, approval_id_, **kwargs):
        db.update_approval(approval_id_, status="approved", decided_at=now_iso(), note="", decision_actor_id=ADMIN_ID, decision_mechanism="manual")
        return {"approval": db.get_approval(approval_id_)}

    monkeypatch.setattr(compat.approvals_module, "approve", fake_plain)
    plain_detail = client.get(f"/api/v2/approvals/{other}").json()
    plain = client.post(
        f"/api/v2/approvals/{other}/decisions",
        json={"decision": "approve"},
        headers={"Idempotency-Key": "plain-decision", "X-Approval-Payload-Digest": plain_detail["payload_digest"]},
    )
    assert plain.status_code == 202, plain.text
    assert "engineering_task_id" not in plain.json()
