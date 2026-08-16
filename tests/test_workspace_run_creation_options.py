"""Safe Project Workspace options used by the Product Run creation form."""

from __future__ import annotations

import inspect
import json

from app.db import Database
from tests.test_execution_plan_v2_api import (
    _enable_execution_plan_v2,
    _seed_execution_context,
)
from tests.test_run_templates_v2 import OPERATOR_ID, _session_for


def test_project_workspace_run_creation_options_are_safe_and_reconcile_aware(
    api_client,
):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    main_module.app_state.config.project_bootstrap_v2_enabled = True
    seed = _seed_execution_context(main_module)
    _session_for(client, main_module, OPERATOR_ID)

    response = client.get(f"/api/v2/projects/{seed['project_id']}/workspace")

    assert response.status_code == 200, response.json()
    options = response.json()["run_creation_options"]
    assert options["state"] == "preview_required"
    assert options["project_version_candidates"] == [
        {
            "id": seed["version"]["id"],
            "created_at": seed["version"]["created_at"],
            "state": "promoted",
        }
    ]
    assert options["ssh_target_candidates"] == [
        {
            "id": seed["revision"]["id"],
            "server_name": "pilot-117",
            "revision": seed["revision"]["revision"],
            "preflight_state": "eligible",
            "instance_state": "available",
            "clean": True,
            "registered_instance_id": seed["instance_id"],
            "update_available": True,
            "matching_promoted_version_ids": [seed["version"]["id"]],
            "ready": True,
            "readiness_reasons": [],
        }
    ]
    serialized = json.dumps(options, sort_keys=True)
    for forbidden in (
        "/srv/projects/product-v2",
        "a" * 40,
        "credential_ref",
        "normalized_target",
        "setup_command",
    ):
        assert forbidden not in serialized

    duplicate_instance_id = main_module.app_state.db.insert_project_instance(
        project_name=seed["project_name"],
        server="pilot-117",
        path="/srv/projects/product-v2-duplicate",
        git_branch="main",
        git_commit="a" * 40,
        dirty=False,
    )
    main_module.app_state.db.update_instance_reconcile(
        duplicate_instance_id,
        state="available",
        git_branch="main",
        git_commit="a" * 40,
        dirty=False,
        touch_last_seen=True,
    )
    ambiguous = client.get(
        f"/api/v2/projects/{seed['project_id']}/workspace"
    ).json()["run_creation_options"]["ssh_target_candidates"]

    assert len(ambiguous) == 1
    assert ambiguous[0]["matching_promoted_version_ids"] == []
    assert ambiguous[0]["ready"] is False
    assert ambiguous[0]["instance_state"] == "ambiguous"
    assert ambiguous[0]["clean"] is None
    assert "project_instance_ambiguous" in ambiguous[0]["readiness_reasons"]

    with main_module.app_state.db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM project_instances WHERE id = ?", (duplicate_instance_id,)
        )
    unique_again = client.get(
        f"/api/v2/projects/{seed['project_id']}/workspace"
    ).json()["run_creation_options"]["ssh_target_candidates"]

    assert len(unique_again) == 1
    assert unique_again[0]["matching_promoted_version_ids"] == [seed["version"]["id"]]
    assert unique_again[0]["ready"] is True

    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"],
        state="unknown",
    )
    reconciled = client.get(
        f"/api/v2/projects/{seed['project_id']}/workspace"
    ).json()["run_creation_options"]["ssh_target_candidates"]

    assert reconciled[0]["ready"] is False
    assert reconciled[0]["instance_state"] == "unknown"
    assert reconciled[0]["readiness_reasons"] == [
        "no_matching_promoted_version",
        "project_instance_not_available",
    ]

    source = inspect.getsource(Database.get_project_workspace_v2)
    assert "creator.kind IN ('server_add', 'server_update')" in source
    assert "creator.payload_sha256 IS NOT NULL" in source
    with main_module.app_state.db.cursor() as cursor:
        cursor.execute(
            "UPDATE approvals SET kind = 'apply_patch' WHERE id = ?",
            (seed["revision"]["created_by_approval_id"],),
        )
    rejected_creator = client.get(
        f"/api/v2/projects/{seed['project_id']}/workspace"
    ).json()["run_creation_options"]["ssh_target_candidates"]
    assert rejected_creator == []
