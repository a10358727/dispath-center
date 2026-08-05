from __future__ import annotations

from app.audit_adoption import adoption_for_action
from app.db import VALID_APPROVAL_KINDS
from scripts.audit_adoption_gate import _legacy_writer_violations


def test_literal_legacy_audit_actions_are_explicitly_catalogued():
    assert _legacy_writer_violations() == []
    assert adoption_for_action("reject") == "legacy"
    assert adoption_for_action("project_deploy") == "legacy"


def test_closed_dynamic_audit_action_families_are_catalogued():
    """Dynamic writers must be as explicit as literal writers.

    ``approve()`` emits ``approval.kind`` for several compatibility branches;
    server removal uses a closed ``action_name`` set and authorization shadow
    uses a closed evidence-action set.  The static gate expands those sets and
    therefore every member must have an owner/issue in the catalog.
    """

    expected_kinds = {
        "auto_placement",
        "dataset_prewarm",
        "dataset_snapshot_build",
        "engineering_command",
        "engineering_task_discard",
        "engineering_task_promote",
        "engineering_task_retry",
        "ignore_nested_candidates",
        "node_enroll",
        "node_retire",
        "node_revoke",
        "node_rotate",
        "plan_run",
        "service_account_create",
        "service_token_issue",
        "service_token_revoke",
    }
    assert expected_kinds <= VALID_APPROVAL_KINDS
    assert all(adoption_for_action(kind) is not None for kind in VALID_APPROVAL_KINDS)
    assert adoption_for_action("authorization_shadow_denied") == "legacy"
