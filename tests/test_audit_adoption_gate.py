from __future__ import annotations

from app.audit_adoption import (
    AUDIT_ADOPTION,
    adoption_for_action,
    validate_audit_catalog,
)
from app.db import TRANSACTION_ONLY_APPROVAL_KINDS, VALID_APPROVAL_KINDS
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
    generic_legacy_kinds = VALID_APPROVAL_KINDS - TRANSACTION_ONLY_APPROVAL_KINDS
    assert all(adoption_for_action(kind) is not None for kind in generic_legacy_kinds)
    assert TRANSACTION_ONLY_APPROVAL_KINDS == {
        "environment_change_v2",
        "project_defaults_change_v2",
        "project_bootstrap_v2",
        "project_role_change",
        "run_template_change_v2",
        "dataset_asset_adoption_v2",
        "dataset_alias_change_v2",
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
        "dataset_grant_revoke_v2",
        "dataset_publish_v2",
        "execution_plan_v2",
        "project_instance_update_v2",
        "experiment_create_v2",
        "hardware_action_v2",
    }
    assert all(adoption_for_action(kind) is None for kind in TRANSACTION_ONLY_APPROVAL_KINDS)
    assert adoption_for_action("authorization_shadow_denied") == "legacy"


def test_project_role_mutation_is_durable_without_claiming_kind_as_action():
    entry = AUDIT_ADOPTION["identity.project_role_binding"]

    assert validate_audit_catalog() == ()
    assert entry.durability == "durable"
    assert entry.owner == "platform-security"
    assert entry.emitted_actions == ()
    assert adoption_for_action("project_role_change") is None


def test_project_bootstrap_materialization_has_explicit_durable_ownership():
    entry = AUDIT_ADOPTION["project.bootstrap"]

    assert entry.durability == "durable"
    assert entry.owner == "product-platform"
    assert entry.emitted_actions == ("project_bootstrap_materialized",)
    assert adoption_for_action("project_bootstrap_v2") is None


def test_environment_mutation_has_explicit_durable_ownership():
    entry = AUDIT_ADOPTION["environment.mutate"]

    assert entry.durability == "durable"
    assert entry.owner == "product-platform"
    assert entry.emitted_actions == ("environment_revision_created",)
    assert adoption_for_action("environment_change_v2") is None
    assert adoption_for_action("environment_revision_created") == "durable"


def test_template_and_defaults_mutations_have_durable_ownership():
    template = AUDIT_ADOPTION["run_template.mutate"]
    defaults = AUDIT_ADOPTION["project_defaults.mutate"]

    assert template.emitted_actions == ("run_profile_spec_created",)
    assert defaults.emitted_actions == ("project_defaults_revision_created",)
    assert template.owner == defaults.owner == "product-platform"
    assert adoption_for_action("run_profile_spec_created") == "durable"
    assert adoption_for_action("project_defaults_revision_created") == "durable"


def test_execution_plan_v2_events_have_explicit_durable_ownership():
    entry = AUDIT_ADOPTION["execution.plan"]

    assert {
        "execution_plan_v2_materialized",
        "execution_plan_v2_rejected_stale",
        "execution_plan_v2_job_materialized",
    } <= set(entry.emitted_actions)
    assert all(
        adoption_for_action(action) == "durable"
        for action in (
            "execution_plan_v2_materialized",
            "execution_plan_v2_rejected_stale",
            "execution_plan_v2_job_materialized",
        )
    )


def test_literal_transaction_only_kind_still_fails_legacy_writer_gate(tmp_path):
    source_root = tmp_path / "app"
    source_root.mkdir()
    (source_root / "leak.py").write_text(
        'append_audit("project_role_change", {})\n',
        encoding="utf-8",
    )

    violations = _legacy_writer_violations((source_root,))

    assert len(violations) == 1
    assert "project_role_change" in violations[0]
    assert "missing from the audit adoption catalog" in violations[0]
