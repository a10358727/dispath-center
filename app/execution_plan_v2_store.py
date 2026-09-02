"""SQLite resolution and materialisation for the Product ExecutionPlan v2.

The public preview and submit paths share the same resolver.  It reads only
reviewed local evidence; it never probes a host or touches the filesystem.
Submit and approval functions receive the caller's active transaction cursor
so idempotency, approval, plan, audit, and Job state commit together.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence, TYPE_CHECKING, cast

from app.execution_contract import canonical_json, canonical_json_sha256, utf8_sha256
from app.config import DeviceSpec, parse_device_specs
from app.execution_plan_v2 import (
    COMMAND_BRIDGE_VERSION,
    EXECUTION_PLAN_V2_APPROVAL_CONTRACT_VERSION,
    EXECUTION_PLAN_V2_APPROVAL_KIND,
    EXECUTION_PLAN_V2_CONTRACT_VERSION,
    OBSERVATION_MAX_AGE_SECONDS,
    DatasetBindingsSelection,
    DispatchPolicyTargetSelection,
    ExecutionPlanV2ApprovalPayload,
    ExecutionPlanV2Request,
    ExecutionPlanV2Spec,
    ProjectDefaultsTemplateSelection,
    ResolvedDatasetBinding,
    RunProfileTemplateSelection,
    ServerConfigTargetSelection,
    SubmitObservationProvenance,
    build_bash_argv_bridge,
    build_execution_plan_v2_spec,
    dispatch_policy_digest,
    execution_plan_v2_approval_payload_digest,
    parse_execution_plan_v2_approval_payload,
    parse_execution_plan_v2_spec,
    project_instance_checkout_digest,
)
from app.identity import Actor
from app.project_environments import validate_secret_free_setup
from app.run_templates import (
    RUN_TEMPLATE_CLASSIFICATION_TYPED,
    compile_structured_argv,
)
from app.server_publication import decode_yaml_document, normalize_target, yaml_digest

from app.project_bootstrap import COMPUTE_ACTION_CLASSES

if TYPE_CHECKING:
    from app.db import Database
    from app.project_bootstrap import ProjectDefaultsContract, RunTemplateContract
    from app.project_environments import EnvironmentRevisionContractV1


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("target_observation_invalid")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        raise ValueError("target_observation_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("target_observation_invalid")
    return parsed.astimezone(timezone.utc)


def _canonical_observation_number(value: Any, field_name: str) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"target_{field_name}_unknown")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"target_{field_name}_invalid") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"target_{field_name}_invalid")
    if number == number.to_integral_value():
        return int(number)
    return format(number.normalize(), "f")


def _observation_provenance_from_row(
    observation: sqlite3.Row,
    *,
    server_name: str,
) -> SubmitObservationProvenance:
    if observation["server_name"] != server_name:
        raise ValueError("target_observation_invalid")
    if not bool(observation["online"]) or not bool(observation["probe_ok"]):
        raise ValueError("target_observation_not_ready")
    gpu_count = observation["gpu_count"]
    ram = observation["mem_available_bytes"]
    disk = observation["disk_avail_bytes"]
    if (
        isinstance(gpu_count, bool)
        or not isinstance(gpu_count, int)
        or gpu_count < 0
        or isinstance(ram, bool)
        or not isinstance(ram, int)
        or ram < 0
        or isinstance(disk, bool)
        or not isinstance(disk, int)
        or disk < 0
    ):
        raise ValueError("target_resource_observation_unknown")
    gpu_used = _canonical_observation_number(
        observation["gpu_mem_used_mb"],
        "gpu_memory",
    )
    gpu_total = _canonical_observation_number(
        observation["gpu_mem_total_mb"],
        "gpu_memory",
    )
    if Decimal(str(gpu_used)) > Decimal(str(gpu_total)):
        raise ValueError("target_gpu_memory_invalid")
    return SubmitObservationProvenance(
        observation_id=int(observation["id"]),
        server_name=server_name,
        observed_at=str(observation["observed_at"]),
        online=True,
        probe_ok=True,
        gpu_count=gpu_count,
        gpu_mem_used_mb=gpu_used,
        gpu_mem_total_mb=gpu_total,
        mem_available_bytes=ram,
        disk_avail_bytes=disk,
    )


def _verified_project_version(
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    project_version_id: str,
) -> sqlite3.Row:
    row = cursor.execute(
        """
        SELECT version.*, project.name AS verified_project_name,
               approval.kind AS promotion_kind,
               approval.status AS promotion_approval_status,
               approval.payload AS promotion_payload,
               approval.payload_sha256 AS promotion_payload_sha256,
               approval.payload_contract_version AS promotion_contract
        FROM project_versions AS version
        JOIN projects AS project ON project.id = version.project_id
        JOIN approvals AS approval ON approval.id = version.promotion_approval_id
        WHERE version.id = ? AND version.project_id = ?
        """,
        (project_version_id, project_id),
    ).fetchone()
    if (
        row is None
        or row["promotion_state"] != "promoted"
        or row["promotion_kind"] != "engineering_task_promote"
        or row["promotion_approval_status"] != "approved"
        or row["promotion_contract"] != "code-promotion-v1"
        or not isinstance(row["bundle_sha256"], str)
        or not isinstance(row["promotion_payload_sha256"], str)
    ):
        raise ValueError("project_version_unavailable")
    raw_payload = str(row["promotion_payload"])
    if utf8_sha256(raw_payload) != row["promotion_payload_sha256"]:
        raise ValueError("project_version_provenance_invalid")
    try:
        payload = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("project_version_provenance_invalid") from None
    if (
        not isinstance(payload, dict)
        or canonical_json(payload) != raw_payload
        or payload.get("project_name") != row["verified_project_name"]
        or payload.get("git_commit") != row["git_commit"]
        or payload.get("bundle_sha256") != row["bundle_sha256"]
    ):
        raise ValueError("project_version_provenance_invalid")
    return row


def _resolve_template(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    request: ExecutionPlanV2Request,
) -> tuple[
    "ProjectDefaultsContract | None",
    "RunTemplateContract",
    "EnvironmentRevisionContractV1",
    dict[str, Any],
]:
    defaults = None
    selection = request.template_selection
    if isinstance(selection, ProjectDefaultsTemplateSelection):
        defaults_row = database._project_defaults_head_from_cursor(
            cursor,
            project_id=project_id,
        )
        if (
            defaults_row is None
            or (
                selection.project_defaults_revision_id is not None
                and defaults_row["id"] != selection.project_defaults_revision_id
            )
        ):
            raise ValueError("project_defaults_head_unavailable")
        defaults = database._project_defaults_contract_from_row(defaults_row)
        if database._project_defaults_staleness_from_cursor(
            cursor,
            project_id=project_id,
            contract=defaults,
        ):
            raise ValueError("project_defaults_head_stale")
        run_profile_id = defaults.run_profile_id
    elif isinstance(selection, RunProfileTemplateSelection):
        run_profile_id = selection.run_profile_id
    else:  # pragma: no cover - discriminated union invariant
        raise ValueError("template_selection_invalid")

    profile = database._run_template_head_by_id_from_cursor(
        cursor,
        project_id=project_id,
        run_profile_id=run_profile_id,
    )
    if profile is None or profile["status"] != "approved":
        raise ValueError("run_profile_head_unavailable")
    classification, template = database._run_template_classification_from_cursor(
        cursor,
        profile,
    )
    if classification != RUN_TEMPLATE_CLASSIFICATION_TYPED or template is None:
        raise ValueError("legacy_run_profile_not_supported")
    #: DG-HARDWARE-EXECUTION v1 H-2: physical action classes never run through
    #: the compute path (and therefore never through an experiment matrix);
    #: they need a `hardware_action_v2` card (P3).
    if template.action_class not in COMPUTE_ACTION_CLASSES:
        raise ValueError("hardware_action_required")
    if defaults is not None and (
        defaults.run_profile_spec_digest != template.spec_digest
        or defaults.environment_revision_id != template.environment_revision_id
    ):
        raise ValueError("project_defaults_head_stale")

    environment_row = cursor.execute(
        """
        SELECT environment.name, revision.*
        FROM environment_revisions AS revision
        JOIN project_environments AS environment
          ON environment.id = revision.environment_id
         AND environment.project_id = revision.project_id
        WHERE revision.id = ? AND revision.project_id = ?
          AND revision.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM environment_revisions AS newer
              WHERE newer.environment_id = revision.environment_id
                AND newer.revision > revision.revision
          )
        """,
        (template.environment_revision_id, project_id),
    ).fetchone()
    if environment_row is None:
        raise ValueError("environment_head_unavailable")
    environment = database._environment_contract_from_row(environment_row)
    validate_secret_free_setup(environment)

    parameter_values: dict[str, Any] = {}
    if defaults is not None:
        parameter_values.update(defaults.parameter_values)
    parameter_values.update(request.parameter_overrides)
    parameter_values = dict(sorted(parameter_values.items()))
    compiled = compile_structured_argv(template, parameter_values)
    return defaults, template, environment, {
        "parameter_values": parameter_values,
        "compiled": compiled,
    }


def _asset_snapshot_evidence(
    cursor: sqlite3.Cursor,
    *,
    asset_id: str,
    snapshot_id: str,
) -> sqlite3.Row:
    row = cursor.execute(
        """
        SELECT asset.owning_project_id, asset.asset_digest,
               snapshot.manifest_digest
        FROM dataset_assets AS asset
        JOIN dataset_asset_snapshots AS link
          ON link.asset_id = asset.id
         AND link.owning_project_id = asset.owning_project_id
        JOIN dataset_snapshots AS snapshot ON snapshot.id = link.snapshot_id
        WHERE asset.id = ? AND link.snapshot_id = ?
          AND snapshot.state = 'published'
          AND snapshot.manifest_digest IS NOT NULL
        """,
        (asset_id, snapshot_id),
    ).fetchone()
    if row is None:
        raise ValueError("dataset_snapshot_unavailable")
    return row


def _resolve_dataset_entitlement(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    asset_id: str,
    snapshot_id: str,
    sharing_enabled: bool,
    exact_grant_id: str | None = None,
) -> dict[str, Any]:
    evidence = _asset_snapshot_evidence(
        cursor,
        asset_id=asset_id,
        snapshot_id=snapshot_id,
    )
    if evidence["owning_project_id"] == project_id:
        if exact_grant_id is not None:
            raise ValueError("dataset_entitlement_changed")
        return {
            "access_mode": "owned",
            "asset_digest": str(evidence["asset_digest"]),
            "manifest_digest": str(evidence["manifest_digest"]),
            "grant_id": None,
            "grant_digest": None,
            "offer_id": None,
            "offer_digest": None,
            "accept_approval_id": None,
        }
    if not sharing_enabled:
        raise ValueError("dataset_sharing_v2_disabled")
    if exact_grant_id is not None:
        candidate_ids = [exact_grant_id]
    else:
        candidate_ids = [
            str(row["id"])
            for row in cursor.execute(
                """
                SELECT id FROM project_dataset_grants
                WHERE target_project_id = ? AND asset_id = ? AND snapshot_id = ?
                  AND revocation_approval_id IS NULL AND revoked_at IS NULL
                ORDER BY granted_at DESC, id DESC LIMIT 101
                """,
                (project_id, asset_id, snapshot_id),
            ).fetchall()[:100]
        ]
    for grant_id in candidate_ids:
        try:
            grant = database._verified_dataset_grant_from_cursor(
                cursor,
                grant_id=grant_id,
                require_active=True,
            )
        except (TypeError, ValueError):
            continue
        if (
            grant["target_project_id"] == project_id
            and grant["asset_id"] == asset_id
            and grant["snapshot_id"] == snapshot_id
        ):
            return {
                "access_mode": "shared",
                "asset_digest": str(evidence["asset_digest"]),
                "manifest_digest": str(evidence["manifest_digest"]),
                "grant_id": grant["grant_id"],
                "grant_digest": grant["grant_digest"],
                "offer_id": grant["offer_id"],
                "offer_digest": grant["offer_digest"],
                "accept_approval_id": grant["accept_approval_id"],
            }
    raise ValueError("dataset_entitlement_unavailable")


def _resolve_datasets(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    request: ExecutionPlanV2Request,
    sharing_enabled: bool,
) -> list[ResolvedDatasetBinding]:
    selection = request.dataset_selection
    if not isinstance(selection, DatasetBindingsSelection):
        return []
    resolved: list[ResolvedDatasetBinding] = []
    for binding in selection.bindings:
        if binding.selection.kind == "alias":
            head = database._dataset_alias_head_from_cursor(
                cursor,
                project_id=project_id,
                asset_id=binding.asset_id,
                alias_name=binding.selection.alias_name,
            )
            if head is None:
                raise ValueError("dataset_alias_unavailable")
            alias = database._dataset_alias_contract_from_row(head)
            snapshot_id = alias.snapshot_id
            selection_provenance: dict[str, Any] = {
                "kind": "alias",
                "alias_revision_id": alias.revision_id,
                "alias_revision_digest": alias.revision_digest,
            }
        else:
            snapshot_id = binding.selection.snapshot_id
            selection_provenance = {
                "kind": "snapshot",
                "alias_revision_id": None,
                "alias_revision_digest": None,
            }
        entitlement = _resolve_dataset_entitlement(
            database,
            cursor,
            project_id=project_id,
            asset_id=binding.asset_id,
            snapshot_id=snapshot_id,
            sharing_enabled=sharing_enabled,
        )
        resolved.append(
            ResolvedDatasetBinding.model_validate(
                {
                    "name": binding.name,
                    "asset_id": binding.asset_id,
                    "snapshot_id": snapshot_id,
                    "selection": selection_provenance,
                    "entitlement": entitlement,
                }
            )
        )
    return sorted(
        resolved,
        key=lambda value: (value.name, value.asset_id, value.snapshot_id),
    )


def _verified_policy(
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    policy_id: str,
    run_profile_id: str,
    dataset_bound: bool,
    now: datetime,
) -> tuple[sqlite3.Row, str]:
    row = cursor.execute(
        """
        SELECT policy.*, approval.status AS approval_status
        FROM dispatch_policies AS policy
        LEFT JOIN approvals AS approval ON approval.id = policy.approval_id
        WHERE policy.id = ? AND policy.project_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM dispatch_policies AS newer
              WHERE newer.project_id = policy.project_id
                AND newer.name = policy.name
                AND newer.revision > policy.revision
          )
        """,
        (policy_id, project_id),
    ).fetchone()
    if (
        row is None
        or row["status"] != "approved"
        or row["approval_status"] != "approved"
    ):
        raise ValueError("dispatch_policy_head_unavailable")
    if row["run_profile_id"] is not None and row["run_profile_id"] != run_profile_id:
        raise ValueError("dispatch_policy_run_profile_mismatch")
    if bool(row["dataset_required"]) and not dataset_bound:
        raise ValueError("dispatch_policy_dataset_required")
    if row["valid_until"] is not None:
        if _parse_timestamp(row["valid_until"]) <= now:
            raise ValueError("dispatch_policy_expired")
    try:
        allowed_servers = json.loads(row["allowed_servers"])
    except (TypeError, json.JSONDecodeError):
        raise ValueError("dispatch_policy_contract_invalid") from None
    if (
        not isinstance(allowed_servers, list)
        or not allowed_servers
        or any(not isinstance(value, str) or not value for value in allowed_servers)
        or allowed_servers != sorted(set(allowed_servers))
    ):
        raise ValueError("dispatch_policy_contract_invalid")
    values = dict(row)
    values["allowed_servers"] = allowed_servers
    return row, dispatch_policy_digest(values)


def _active_policy_placement_count(
    cursor: sqlite3.Cursor,
    *,
    policy_id: str,
) -> int:
    row = cursor.execute(
        """
        SELECT COUNT(*) AS placement_count
        FROM execution_plan_v2_specs AS spec
        JOIN execution_plans AS plan ON plan.id = spec.execution_plan_id
        JOIN jobs AS job ON job.id = plan.job_id
        WHERE spec.dispatch_policy_id = ?
          AND job.status IN ('queued', 'running', 'blocked')
        """,
        (policy_id,),
    ).fetchone()
    return int(row["placement_count"]) if row is not None else 0


def _validate_supported_execution_preflight(
    environment: "EnvironmentRevisionContractV1",
) -> None:
    """The read-only resolver accepts tag and executable checks only.

    Tags are proven from the pinned server-config revision; executable
    presence is advisory evidence produced by the monitor's closed
    ``command -v`` probe and surfaced through environment readiness
    (DG-HARDWARE-EXECUTION v1 H-1 -- the named resolver expansion; a missing
    tool then fails the run itself, honestly). Environment, secret-reference,
    and checkout-relative-path checks still require target-host evidence the
    observation contract does not carry -- treating them as satisfied would
    turn unknown evidence into an unsafe positive decision.
    """

    supported = {"server_tag_present", "executable_present"}
    if any(check.kind not in supported for check in environment.preflight_checks):
        raise ValueError("environment_preflight_evidence_unsupported")


def _server_revision_contract(
    cursor: sqlite3.Cursor,
    revision: sqlite3.Row,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[DeviceSpec, ...]]:
    raw_payload = str(revision["creator_payload"])
    if (
        revision["creator_contract"] != "server-config-v1"
        or revision["creator_kind"] not in {"server_add", "server_update"}
        or revision["creator_status"] != "approved"
        or not isinstance(revision["creator_payload_sha256"], str)
        or utf8_sha256(raw_payload) != revision["creator_payload_sha256"]
    ):
        raise ValueError("target_revision_unavailable")
    try:
        payload = json.loads(raw_payload)
        normalized_target = json.loads(revision["normalized_target_json"])
        credential_ref = json.loads(revision["credential_ref_json"])
    except (TypeError, json.JSONDecodeError):
        raise ValueError("target_identity_invalid") from None
    if (
        not isinstance(payload, dict)
        or canonical_json(payload) != raw_payload
        or payload.get("server_name") != revision["server_name"]
        or payload.get("normalized_target") != normalized_target
        or payload.get("credential_ref") != credential_ref
        or canonical_json_sha256(
            {
                "normalized_target": normalized_target,
                "credential_ref": credential_ref,
            }
        )
        != revision["target_identity_sha256"]
        or normalized_target.get("backend") != "ssh"
    ):
        raise ValueError("target_identity_invalid")
    encoded = payload.get("yaml_after_utf8_b64")
    if not isinstance(encoded, str):
        raise ValueError("target_publication_invalid")
    try:
        document = decode_yaml_document(encoded)
    except ValueError:
        raise ValueError("target_publication_invalid") from None
    if payload.get("yaml_after_sha256") != yaml_digest(document):
        raise ValueError("target_publication_invalid")
    entries = [
        entry
        for entry in document.get("servers", [])
        if isinstance(entry, dict) and entry.get("name") == revision["server_name"]
    ]
    if (
        len(entries) != 1
        or entries[0].get("enabled", True) is not True
        or normalize_target(entries[0]) != normalized_target
    ):
        raise ValueError("target_publication_invalid")
    raw_tags = entries[0].get("tags") or []
    if (
        not isinstance(raw_tags, list)
        or any(not isinstance(tag, str) or not tag for tag in raw_tags)
    ):
        raise ValueError("target_publication_invalid")
    if cursor.execute(
        """
        SELECT 1 FROM server_config_mutations
        WHERE server_name = ?
          AND state IN ('intent', 'yaml_applied', 'recovery_hold')
        """,
        (revision["server_name"],),
    ).fetchone() is not None:
        raise ValueError("target_revision_unavailable")
    raw_devices = entries[0].get("devices") or []
    try:
        declared_devices = parse_device_specs(
            raw_devices, server_name=str(revision["server_name"])
        )
    except ValueError:
        raise ValueError("target_publication_invalid") from None
    return normalized_target, tuple(sorted(set(raw_tags))), tuple(declared_devices)


def _worker_is_exclusive(cursor: sqlite3.Cursor, server_name: str) -> bool:
    if cursor.execute(
        """
        SELECT 1 FROM jobs
        WHERE status IN ('queued', 'running')
          AND (pin_server = ? OR server = ?)
        LIMIT 1
        """,
        (server_name, server_name),
    ).fetchone() is not None:
        return False
    return cursor.execute(
        """
        SELECT 1 FROM execution_attempts
        WHERE server_name = ? AND state IN ('leased', 'dispatching', 'running')
        LIMIT 1
        """,
        (server_name,),
    ).fetchone() is None


def _match_required_devices(
    required: Sequence[Mapping[str, Any]],
    declared: Sequence[Any],
) -> tuple[str, ...]:
    """DG-HARDWARE-EXECUTION v1 P1（H-1）：需求 × 宣告的純函式匹配。

    每個 `DeviceRequirement`（dump 後的 mapping：kind／id?／tags?）必須被目標
    revision 宣告的某個 `DeviceSpec` 滿足：kind 相等、（有給 id 時）id 精確
    相等、需求 tags ⊆ 裝置 tags。不滿足＝`target_device_missing`。回傳被匹配
    到的裝置 id（去重、依需求順序），供在場觀測檢查。裝置只是資格過濾——
    一機一件的排程語意不變（H-5）。
    """

    matched: list[str] = []
    for requirement in required:
        wanted_kind = requirement.get("kind")
        wanted_id = requirement.get("id")
        wanted_tags = set(requirement.get("tags") or ())
        satisfied = None
        for device in declared:
            if device.kind != wanted_kind:
                continue
            if wanted_id is not None and device.id != wanted_id:
                continue
            if not wanted_tags.issubset(set(device.tags)):
                continue
            satisfied = device
            break
        if satisfied is None:
            raise ValueError("target_device_missing")
        if satisfied.id not in matched:
            matched.append(satisfied.id)
    return tuple(matched)


def _resource_observation(
    cursor: sqlite3.Cursor,
    *,
    server_name: str,
    activated_at: str,
    requirements: Mapping[str, Any],
    now: datetime,
    required_device_ids: Sequence[str] = (),
) -> SubmitObservationProvenance:
    observation = cursor.execute(
        """
        SELECT * FROM server_observations
        WHERE server_name = ?
        ORDER BY observed_at DESC, id DESC LIMIT 1
        """,
        (server_name,),
    ).fetchone()
    if observation is None:
        raise ValueError("target_observation_unknown")
    provenance = _observation_provenance_from_row(
        observation,
        server_name=server_name,
    )
    observed_at = _parse_timestamp(provenance.observed_at)
    activated = _parse_timestamp(activated_at)
    if (
        observed_at < activated
        or observed_at > now
        or (now - observed_at).total_seconds() > OBSERVATION_MAX_AGE_SECONDS
    ):
        raise ValueError("target_observation_stale")
    gpu_used = Decimal(str(provenance.gpu_mem_used_mb))
    gpu_total = Decimal(str(provenance.gpu_mem_total_mb))
    if provenance.gpu_count < int(requirements["min_gpu_count"]):
        raise ValueError("target_gpu_count_insufficient")
    if gpu_total - gpu_used < Decimal(int(requirements["min_gpu_memory_mb"])):
        raise ValueError("target_gpu_memory_insufficient")
    if (
        provenance.mem_available_bytes
        < int(requirements["min_available_ram_mb"]) * 1024 * 1024
    ):
        raise ValueError("target_ram_insufficient")
    if (
        provenance.disk_avail_bytes
        < int(requirements["min_available_disk_mb"]) * 1024 * 1024
    ):
        raise ValueError("target_disk_insufficient")
    if required_device_ids:
        #: DG-HARDWARE-EXECUTION v1 P1（H-1）：被匹配裝置必須在**同一筆**新鮮
        #: 觀測中為 present。devices_json NULL／不可解析／缺 id＝未觀測
        #: （unknown，不是 absent——INV-SSH-7 同構），absent＝明確不在場。
        raw_devices_json = (
            observation["devices_json"]
            if "devices_json" in observation.keys()
            else None
        )
        observed_devices: Any = None
        if isinstance(raw_devices_json, str):
            try:
                observed_devices = json.loads(raw_devices_json)
            except json.JSONDecodeError:
                observed_devices = None
        if not isinstance(observed_devices, dict):
            raise ValueError("target_device_observation_unknown")
        for device_id in required_device_ids:
            status = observed_devices.get(device_id)
            if status == "absent":
                raise ValueError("target_device_absent")
            if status != "present":
                raise ValueError("target_device_observation_unknown")
    return provenance


def _candidate_for_revision(
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    project_name: str,
    project_version: sqlite3.Row,
    revision: sqlite3.Row,
    required_tags: set[str],
    requirements: Mapping[str, Any],
    now: datetime,
    skip_exclusivity: bool = False,
) -> dict[str, Any]:
    if (
        revision["publication_state"] != "active"
        or revision["assignment_eligibility"] != "approved"
        or revision["attempt_backend_preflight"] != "eligible"
    ):
        raise ValueError("target_revision_unavailable")
    _, tags, declared_devices = _server_revision_contract(cursor, revision)
    if not required_tags.issubset(tags):
        raise ValueError("target_required_tags_missing")
    required_device_ids = _match_required_devices(
        requirements.get("required_devices") or (),
        declared_devices,
    )
    instances = cursor.execute(
        """
        SELECT * FROM project_instances
        WHERE project_id = ? AND project_name = ? AND server = ?
          AND state IN ('available', 'diverged') AND dirty = 0 AND git_commit = ?
        ORDER BY id LIMIT 2
        """,
        (
            project_id,
            project_name,
            revision["server_name"],
            project_version["git_commit"],
        ),
    ).fetchall()
    if len(instances) != 1:
        raise ValueError("target_project_instance_unavailable")
    instance = instances[0]
    if (
        bool(requirements["exclusive_worker"])
        and not skip_exclusivity
        and not _worker_is_exclusive(cursor, str(revision["server_name"]))
    ):
        raise ValueError("target_worker_not_exclusive")
    observation = _resource_observation(
        cursor,
        server_name=str(revision["server_name"]),
        activated_at=str(revision["activated_at"]),
        requirements=requirements,
        now=now,
        required_device_ids=required_device_ids,
    )
    checkout_digest = project_instance_checkout_digest(
        project_instance_id=str(instance["id"]),
        project_id=project_id,
        server_name=str(revision["server_name"]),
        path=str(instance["path"]),
        git_commit=str(project_version["git_commit"]),
    )
    return {
        "revision": revision,
        "instance": instance,
        "observation": observation,
        "checkout_digest": checkout_digest,
    }


def _resolve_target(
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    project_name: str,
    project_version: sqlite3.Row,
    run_profile_id: str,
    dataset_bound: bool,
    request: ExecutionPlanV2Request,
    environment: "EnvironmentRevisionContractV1",
    template: "RunTemplateContract",
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    requirements = template.resource_requirements.model_dump(mode="json")
    required_tags = set(environment.required_server_tags) | set(
        template.resource_requirements.required_tags
    )
    policy_row = None
    policy_digest = None
    selection = request.target_selection
    if isinstance(selection, DispatchPolicyTargetSelection):
        policy_row, policy_digest = _verified_policy(
            cursor,
            project_id=project_id,
            policy_id=selection.dispatch_policy_id,
            run_profile_id=run_profile_id,
            dataset_bound=dataset_bound,
            now=now,
        )
        if policy_row["require_tag"] is not None:
            required_tags.add(str(policy_row["require_tag"]))
        allowed_servers = cast(list[str], json.loads(policy_row["allowed_servers"]))
        active_for_policy = _active_policy_placement_count(
            cursor,
            policy_id=str(policy_row["id"]),
        )
        if active_for_policy >= int(policy_row["max_concurrent_placements"]):
            raise ValueError("dispatch_policy_concurrency_exhausted")
        rows = cursor.execute(
            """
            SELECT revision.*, creator.kind AS creator_kind,
                   creator.status AS creator_status,
                   creator.payload AS creator_payload,
                   creator.payload_sha256 AS creator_payload_sha256,
                   creator.payload_contract_version AS creator_contract
            FROM server_config_revisions AS revision
            JOIN approvals AS creator ON creator.id = revision.created_by_approval_id
            WHERE revision.server_name IN (
                SELECT value FROM json_each(?)
            )
            ORDER BY revision.server_name ASC, revision.id ASC
            """,
            (canonical_json(allowed_servers),),
        ).fetchall()
    elif isinstance(selection, ServerConfigTargetSelection):
        rows = cursor.execute(
            """
            SELECT revision.*, creator.kind AS creator_kind,
                   creator.status AS creator_status,
                   creator.payload AS creator_payload,
                   creator.payload_sha256 AS creator_payload_sha256,
                   creator.payload_contract_version AS creator_contract
            FROM server_config_revisions AS revision
            JOIN approvals AS creator ON creator.id = revision.created_by_approval_id
            WHERE revision.id = ?
            ORDER BY revision.server_name ASC, revision.id ASC
            """,
            (selection.server_config_revision_id,),
        ).fetchall()
    else:  # pragma: no cover - discriminated union invariant
        raise ValueError("target_selection_invalid")

    failures: list[str] = []
    candidate = None
    for revision in rows:
        try:
            candidate = _candidate_for_revision(
                cursor,
                project_id=project_id,
                project_name=project_name,
                project_version=project_version,
                revision=revision,
                required_tags=required_tags,
                requirements=requirements,
                now=now,
            )
            break
        except ValueError as exc:
            failures.append(str(exc))
    if candidate is None:
        raise ValueError(failures[0] if failures else "target_revision_unavailable")
    revision = candidate["revision"]
    target = {
        "selection_kind": selection.kind,
        "dispatch_policy_id": (
            str(policy_row["id"]) if policy_row is not None else None
        ),
        "dispatch_policy_digest": policy_digest,
        "server_config_revision_id": str(revision["id"]),
        "target_identity_sha256": str(revision["target_identity_sha256"]),
        "server_name": str(revision["server_name"]),
    }
    return target, candidate


def _resolve_with_cursor(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    request: ExecutionPlanV2Request,
    requester_actor_id: str,
    sharing_enabled: bool,
    now: datetime,
) -> dict[str, Any]:
    database._validate_environment_project_actor(
        cursor,
        project_id=project_id,
        actor_id=requester_actor_id,
        purpose="requester",
        require_readiness=True,
    )
    project = cursor.execute(
        "SELECT id, name FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if project is None:
        raise ValueError("project_not_found")
    project_version = _verified_project_version(
        cursor,
        project_id=project_id,
        project_version_id=request.project_version_id,
    )
    defaults, template, environment, compilation = _resolve_template(
        database,
        cursor,
        project_id=project_id,
        request=request,
    )
    _validate_supported_execution_preflight(environment)
    bindings = _resolve_datasets(
        database,
        cursor,
        project_id=project_id,
        request=request,
        sharing_enabled=sharing_enabled,
    )
    target, candidate = _resolve_target(
        cursor,
        project_id=project_id,
        project_name=str(project["name"]),
        project_version=project_version,
        run_profile_id=template.run_profile_id,
        dataset_bound=bool(bindings),
        request=request,
        environment=environment,
        template=template,
        now=now,
    )
    instance = candidate["instance"]
    compiled = compilation["compiled"]
    command = build_bash_argv_bridge(
        checkout_path=str(instance["path"]),
        git_commit=str(project_version["git_commit"]),
        setup_command=environment.setup_command,
        argv=compiled.argv,
    )
    resources = template.resource_requirements.model_dump(mode="json")
    output_declarations = [
        output.model_dump(mode="json") for output in template.output_declarations
    ]
    spec = build_execution_plan_v2_spec(
        contract_version="execution-plan-v2",
        project_id=project_id,
        project_version={
            "project_version_id": str(project_version["id"]),
            "git_commit": str(project_version["git_commit"]),
            "bundle_sha256": str(project_version["bundle_sha256"]),
        },
        project_defaults=(
            {
                "project_defaults_revision_id": defaults.revision_id,
                "revision_digest": defaults.revision_digest,
            }
            if defaults is not None
            else None
        ),
        run_profile={
            "run_profile_id": template.run_profile_id,
            "spec_digest": template.spec_digest,
        },
        environment={
            "environment_revision_id": environment.revision_id,
            "revision_digest": environment.revision_digest,
        },
        parameter_values=compilation["parameter_values"],
        dataset_none=not bindings,
        dataset_bindings=[binding.model_dump(mode="json") for binding in bindings],
        target=target,
        backend="ssh",
        project_instance={
            "project_instance_id": str(instance["id"]),
            "checkout_evidence_digest": candidate["checkout_digest"],
        },
        resource_requirements=resources,
        resource_requirements_digest=utf8_sha256(canonical_json(resources)),
        observation_max_age_seconds=OBSERVATION_MAX_AGE_SECONDS,
        canonical_argv_sha256=compiled.argv_sha256,
        output_declarations_digest=utf8_sha256(canonical_json(output_declarations)),
        command_bridge_version=COMMAND_BRIDGE_VERSION,
        compiled_command_sha256=compiled.argv_sha256,
        job_command_sha256=utf8_sha256(command),
    )
    return {
        "spec": spec,
        "canonical_argv_json": compiled.canonical_argv_bytes.decode("utf-8"),
        "job_command": command,
        "observation": candidate["observation"],
        "evidence": {
            "server_name": target["server_name"],
            "observation_id": candidate["observation"].observation_id,
            "observed_at": candidate["observation"].observed_at,
            "resource_state": "eligible",
        },
    }


def resolve_execution_plan_v2(
    database: "Database",
    *,
    project_id: str,
    request: ExecutionPlanV2Request,
    requester_actor_id: str,
    sharing_enabled: bool,
    now: datetime | None = None,
    cursor: sqlite3.Cursor | None = None,
) -> dict[str, Any]:
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None or observed_now.utcoffset() is None:
        raise ValueError("resolver_now_must_be_timezone_aware")
    observed_now = observed_now.astimezone(timezone.utc)
    if cursor is not None:
        return _resolve_with_cursor(
            database,
            cursor,
            project_id=project_id,
            request=request,
            requester_actor_id=requester_actor_id,
            sharing_enabled=sharing_enabled,
            now=observed_now,
        )
    with database.cursor() as read_cursor:
        return _resolve_with_cursor(
            database,
            read_cursor,
            project_id=project_id,
            request=request,
            requester_actor_id=requester_actor_id,
            sharing_enabled=sharing_enabled,
            now=observed_now,
        )


def create_execution_plan_v2_request_in_transaction(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    request: ExecutionPlanV2Request,
    expected_plan_digest: str,
    requester_actor_id: str,
    sharing_enabled: bool,
    now: datetime | None = None,
    execution_plan_id: str | None = None,
) -> dict[str, Any]:
    resolved = resolve_execution_plan_v2(
        database,
        project_id=project_id,
        request=request,
        requester_actor_id=requester_actor_id,
        sharing_enabled=sharing_enabled,
        now=now,
        cursor=cursor,
    )
    spec: ExecutionPlanV2Spec = resolved["spec"]
    if spec.plan_digest != expected_plan_digest:
        raise ValueError("expected_plan_digest_mismatch")
    plan_id = execution_plan_id or str(uuid.uuid4())
    approval_payload = ExecutionPlanV2ApprovalPayload(
        execution_plan_id=plan_id,
        project_id=project_id,
        plan_digest=spec.plan_digest,
    )
    payload_json = canonical_json(approval_payload.model_dump(mode="json"))
    payload_digest = execution_plan_v2_approval_payload_digest(approval_payload)
    created_at = database._sqlite_now(cursor)
    cursor.execute(
        """
        INSERT INTO approvals (
            kind, payload, status, created_at, requester_actor_id,
            payload_sha256, payload_contract_version, payload_immutable_at
        ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            EXECUTION_PLAN_V2_APPROVAL_KIND,
            payload_json,
            created_at,
            requester_actor_id,
            payload_digest,
            EXECUTION_PLAN_V2_APPROVAL_CONTRACT_VERSION,
            created_at,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - sqlite INSERT invariant
        raise RuntimeError("execution_plan_approval_insert_failed")
    approval_id = int(cursor.lastrowid)
    database._append_approval_created_audit(
        cursor,
        approval_id=approval_id,
        kind=EXECUTION_PLAN_V2_APPROVAL_KIND,
        requester_actor_id=requester_actor_id,
    )
    bindings = spec.dataset_bindings
    if not bindings:
        legacy_snapshot_id = None
        legacy_dataset_none = 1
        legacy_reproducible = 1
    elif len(bindings) == 1:
        legacy_snapshot_id = bindings[0].snapshot_id
        legacy_dataset_none = 0
        legacy_reproducible = 1
    else:
        legacy_snapshot_id = None
        legacy_dataset_none = 0
        legacy_reproducible = 0
    project_name = cursor.execute(
        "SELECT name FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()["name"]
    cursor.execute(
        """
        INSERT INTO execution_plans (
            id, project_name, contract_version, plan_digest,
            command, command_sha256, reproducible, project_version_id,
            run_profile_id, dataset_snapshot_id, dataset_none,
            server_config_revision_id, request_approval_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            project_name,
            spec.contract_version,
            spec.plan_digest,
            resolved["job_command"],
            spec.job_command_sha256,
            legacy_reproducible,
            spec.project_version.project_version_id,
            spec.run_profile.run_profile_id,
            legacy_snapshot_id,
            legacy_dataset_none,
            spec.target.server_config_revision_id,
            approval_id,
            created_at,
        ),
    )
    canonical_spec_json = canonical_json(spec.model_dump(mode="json"))
    parameter_values_json = canonical_json(spec.parameter_values)
    dataset_bindings_json = canonical_json(
        [binding.model_dump(mode="json") for binding in spec.dataset_bindings]
    )
    resource_requirements_json = canonical_json(
        spec.resource_requirements.model_dump(mode="json")
    )
    observation: SubmitObservationProvenance = resolved["observation"]
    observation_json = canonical_json(observation.model_dump(mode="json"))
    cursor.execute(
        """
        INSERT INTO execution_plan_v2_specs (
            execution_plan_id, project_id, contract_version,
            canonical_spec_json, plan_digest, project_version_id,
            project_defaults_revision_id, project_defaults_revision_digest,
            run_profile_id, run_profile_spec_digest,
            environment_revision_id, environment_revision_digest,
            parameter_values_json, dataset_none, dataset_bindings_json,
            dataset_bindings_digest, target_selection_kind,
            dispatch_policy_id, dispatch_policy_digest,
            server_config_revision_id, target_identity_sha256, backend,
            project_instance_id, project_instance_checkout_digest,
            resource_requirements_json, resource_requirements_digest,
            output_declarations_digest, canonical_argv_json,
            compiled_command_sha256, command_bridge_version,
            job_command_sha256, observation_max_age_seconds,
            submit_observation_id, submit_observation_json,
            submit_observation_digest, created_approval_id,
            created_by_actor_id, created_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            plan_id,
            project_id,
            spec.contract_version,
            canonical_spec_json,
            spec.plan_digest,
            spec.project_version.project_version_id,
            (
                spec.project_defaults.project_defaults_revision_id
                if spec.project_defaults is not None
                else None
            ),
            (
                spec.project_defaults.revision_digest
                if spec.project_defaults is not None
                else None
            ),
            spec.run_profile.run_profile_id,
            spec.run_profile.spec_digest,
            spec.environment.environment_revision_id,
            spec.environment.revision_digest,
            parameter_values_json,
            1 if spec.dataset_none else 0,
            dataset_bindings_json,
            utf8_sha256(dataset_bindings_json),
            spec.target.selection_kind,
            spec.target.dispatch_policy_id,
            spec.target.dispatch_policy_digest,
            spec.target.server_config_revision_id,
            spec.target.target_identity_sha256,
            spec.backend,
            spec.project_instance.project_instance_id,
            spec.project_instance.checkout_evidence_digest,
            resource_requirements_json,
            spec.resource_requirements_digest,
            spec.output_declarations_digest,
            resolved["canonical_argv_json"],
            spec.compiled_command_sha256,
            spec.command_bridge_version,
            spec.job_command_sha256,
            spec.observation_max_age_seconds,
            observation.observation_id,
            observation_json,
            utf8_sha256(observation_json),
            approval_id,
            requester_actor_id,
            created_at,
        ),
    )
    database.append_durable_audit_event_in_transaction(
        cursor,
        action="execution_plan_v2_materialized",
        params={
            "contract_version": spec.contract_version,
            "dataset_binding_count": len(spec.dataset_bindings),
            "plan_digest": spec.plan_digest,
            "project_id": project_id,
            "server_config_revision_id": spec.target.server_config_revision_id,
        },
        result="pending_approval",
        actor_id=requester_actor_id,
        actor_kind=database._durable_actor_kind(
            cursor,
            requester_actor_id,
            fallback="actor",
        ),
        authentication="product_rbac_v2",
        resource_type="execution_plan",
        resource_id=plan_id,
        approval_id=approval_id,
        event_id=f"execution-plan-v2:{plan_id}:materialized",
    )
    return {
        "execution_plan_id": plan_id,
        "approval_id": approval_id,
        "plan_digest": spec.plan_digest,
        "spec": spec,
    }


def _verify_stored_plan_projection(
    cursor: sqlite3.Cursor,
    *,
    approval: sqlite3.Row,
    plan: sqlite3.Row,
    companion: sqlite3.Row,
    payload: ExecutionPlanV2ApprovalPayload,
    spec: ExecutionPlanV2Spec,
) -> None:
    bindings = [
        binding.model_dump(mode="json") for binding in spec.dataset_bindings
    ]
    parameter_values_json = canonical_json(spec.parameter_values)
    dataset_bindings_json = canonical_json(bindings)
    resource_requirements_json = canonical_json(
        spec.resource_requirements.model_dump(mode="json")
    )
    defaults_id = (
        spec.project_defaults.project_defaults_revision_id
        if spec.project_defaults is not None
        else None
    )
    defaults_digest = (
        spec.project_defaults.revision_digest
        if spec.project_defaults is not None
        else None
    )
    expected_companion = {
        "execution_plan_id": payload.execution_plan_id,
        "project_id": payload.project_id,
        "contract_version": spec.contract_version,
        "plan_digest": spec.plan_digest,
        "project_version_id": spec.project_version.project_version_id,
        "project_defaults_revision_id": defaults_id,
        "project_defaults_revision_digest": defaults_digest,
        "run_profile_id": spec.run_profile.run_profile_id,
        "run_profile_spec_digest": spec.run_profile.spec_digest,
        "environment_revision_id": spec.environment.environment_revision_id,
        "environment_revision_digest": spec.environment.revision_digest,
        "parameter_values_json": parameter_values_json,
        "dataset_none": 1 if spec.dataset_none else 0,
        "dataset_bindings_json": dataset_bindings_json,
        "dataset_bindings_digest": utf8_sha256(dataset_bindings_json),
        "target_selection_kind": spec.target.selection_kind,
        "dispatch_policy_id": spec.target.dispatch_policy_id,
        "dispatch_policy_digest": spec.target.dispatch_policy_digest,
        "server_config_revision_id": spec.target.server_config_revision_id,
        "target_identity_sha256": spec.target.target_identity_sha256,
        "backend": spec.backend,
        "project_instance_id": spec.project_instance.project_instance_id,
        "project_instance_checkout_digest": (
            spec.project_instance.checkout_evidence_digest
        ),
        "resource_requirements_json": resource_requirements_json,
        "resource_requirements_digest": spec.resource_requirements_digest,
        "output_declarations_digest": spec.output_declarations_digest,
        "compiled_command_sha256": spec.compiled_command_sha256,
        "command_bridge_version": spec.command_bridge_version,
        "job_command_sha256": spec.job_command_sha256,
        "observation_max_age_seconds": spec.observation_max_age_seconds,
        "created_approval_id": approval["id"],
        "created_by_actor_id": approval["requester_actor_id"],
        "created_at": approval["created_at"],
    }
    if any(
        companion[field] != expected
        for field, expected in expected_companion.items()
    ):
        raise ValueError("execution_plan_companion_mismatch")

    project = cursor.execute(
        "SELECT name FROM projects WHERE id = ?",
        (spec.project_id,),
    ).fetchone()
    if project is None:
        raise ValueError("execution_plan_contract_mismatch")
    if not bindings:
        legacy_snapshot_id = None
        legacy_dataset_none = 1
        legacy_reproducible = 1
    elif len(bindings) == 1:
        legacy_snapshot_id = bindings[0]["snapshot_id"]
        legacy_dataset_none = 0
        legacy_reproducible = 1
    else:
        legacy_snapshot_id = None
        legacy_dataset_none = 0
        legacy_reproducible = 0
    expected_plan = {
        "id": payload.execution_plan_id,
        "project_name": project["name"],
        "contract_version": spec.contract_version,
        "plan_digest": spec.plan_digest,
        "reproducible": legacy_reproducible,
        "project_version_id": spec.project_version.project_version_id,
        "run_profile_id": spec.run_profile.run_profile_id,
        "dataset_snapshot_id": legacy_snapshot_id,
        "dataset_none": legacy_dataset_none,
        "server_config_revision_id": spec.target.server_config_revision_id,
        "request_approval_id": approval["id"],
        "created_at": approval["created_at"],
    }
    if any(plan[field] != expected for field, expected in expected_plan.items()):
        raise ValueError("execution_plan_contract_mismatch")
    command = plan["command"]
    if (
        not isinstance(command, str)
        or plan["command_sha256"] != spec.job_command_sha256
        or utf8_sha256(command) != spec.job_command_sha256
        or approval["payload_immutable_at"] != approval["created_at"]
    ):
        raise ValueError("execution_plan_contract_mismatch")

    jobs = cursor.execute(
        "SELECT * FROM jobs WHERE execution_approval_id = ? ORDER BY id LIMIT 2",
        (approval["id"],),
    ).fetchall()
    if approval["status"] in {"pending", "rejected"}:
        if plan["job_id"] is not None or jobs:
            raise ValueError("execution_plan_job_mismatch")
    elif approval["status"] == "approved":
        if plan["job_id"] is None or len(jobs) != 1:
            raise ValueError("execution_plan_job_mismatch")
        job = jobs[0]
        expected_job = {
            "id": plan["job_id"],
            "type": "adhoc",
            "project": plan["project_name"],
            "command": command,
            "require_tag": None,
            "pin_server": spec.target.server_name,
            "depends_on": "[]",
            "gpus_needed": spec.resource_requirements.min_gpu_count,
            "priority": "normal",
            "target_server": None,
            "dataset_name": None,
            "dataset_version": None,
            "engineering_task_id": None,
            "engineering_task_role": None,
            "engineering_attempt_number": None,
            "engineering_validation_request_id": None,
            "auto_placement_approval_id": None,
            "execution_approval_id": approval["id"],
            "approved_payload_sha256": approval["payload_sha256"],
            "execution_contract_version": spec.contract_version,
            "execution_contract_role": "main",
            "approved_command_sha256": spec.job_command_sha256,
        }
        if any(job[field] != expected for field, expected in expected_job.items()):
            raise ValueError("execution_plan_job_mismatch")
        if (
            job["status"]
            not in {"queued", "running", "blocked", "done", "failed", "cancelled"}
            or (job["server"] is not None and job["server"] != spec.target.server_name)
            or (job["status"] == "queued" and job["server"] is not None)
            or utf8_sha256(str(job["command"])) != spec.job_command_sha256
        ):
            raise ValueError("execution_plan_job_mismatch")
    else:
        raise ValueError("execution_plan_approval_invalid")

    raw_argv = companion["canonical_argv_json"]
    if not isinstance(raw_argv, str):
        raise ValueError("execution_plan_companion_mismatch")
    try:
        argv = json.loads(raw_argv)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("execution_plan_companion_mismatch") from None
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(element, str) or not element for element in argv)
        or canonical_json(argv) != raw_argv
        or utf8_sha256(raw_argv) != spec.canonical_argv_sha256
    ):
        raise ValueError("execution_plan_companion_mismatch")

    raw_observation = companion["submit_observation_json"]
    if not isinstance(raw_observation, str):
        raise ValueError("execution_plan_companion_mismatch")
    try:
        stored_observation = SubmitObservationProvenance.model_validate_json(
            raw_observation
        )
    except ValueError:
        raise ValueError("execution_plan_companion_mismatch") from None
    if (
        canonical_json(stored_observation.model_dump(mode="json"))
        != raw_observation
        or utf8_sha256(raw_observation) != companion["submit_observation_digest"]
        or stored_observation.observation_id != companion["submit_observation_id"]
        or stored_observation.server_name != spec.target.server_name
    ):
        raise ValueError("execution_plan_companion_mismatch")


def _verified_stored_plan(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    approval_id: int,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row, ExecutionPlanV2ApprovalPayload, ExecutionPlanV2Spec]:
    approval = cursor.execute(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()
    if approval is None or approval["kind"] != EXECUTION_PLAN_V2_APPROVAL_KIND:
        raise ValueError("execution_plan_approval_unavailable")
    raw_payload = str(approval["payload"])
    if (
        approval["payload_contract_version"]
        != EXECUTION_PLAN_V2_APPROVAL_CONTRACT_VERSION
        or approval["payload_immutable_at"] is None
        or not isinstance(approval["payload_sha256"], str)
        or utf8_sha256(raw_payload) != approval["payload_sha256"]
    ):
        raise ValueError("execution_plan_approval_invalid")
    try:
        payload = parse_execution_plan_v2_approval_payload(json.loads(raw_payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("execution_plan_approval_invalid") from None
    if (
        canonical_json(payload.model_dump(mode="json")) != raw_payload
        or execution_plan_v2_approval_payload_digest(payload)
        != approval["payload_sha256"]
    ):
        raise ValueError("execution_plan_approval_invalid")
    plan = cursor.execute(
        "SELECT * FROM execution_plans WHERE id = ?",
        (payload.execution_plan_id,),
    ).fetchone()
    companion = cursor.execute(
        "SELECT * FROM execution_plan_v2_specs WHERE execution_plan_id = ?",
        (payload.execution_plan_id,),
    ).fetchone()
    if plan is None or companion is None:
        raise ValueError("execution_plan_companion_unavailable")
    raw_spec = str(companion["canonical_spec_json"])
    try:
        spec = parse_execution_plan_v2_spec(json.loads(raw_spec))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("execution_plan_spec_invalid") from None
    if canonical_json(spec.model_dump(mode="json")) != raw_spec:
        raise ValueError("execution_plan_spec_invalid")
    if spec.plan_digest != payload.plan_digest:
        raise ValueError("execution_plan_contract_mismatch")
    _verify_stored_plan_projection(
        cursor,
        approval=approval,
        plan=plan,
        companion=companion,
        payload=payload,
        spec=spec,
    )
    return approval, plan, companion, payload, spec


def _revalidate_stored_datasets(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    spec: ExecutionPlanV2Spec,
    sharing_enabled: bool,
) -> None:
    for binding in spec.dataset_bindings:
        entitlement = _resolve_dataset_entitlement(
            database,
            cursor,
            project_id=spec.project_id,
            asset_id=binding.asset_id,
            snapshot_id=binding.snapshot_id,
            sharing_enabled=sharing_enabled,
            exact_grant_id=binding.entitlement.grant_id,
        )
        if entitlement != binding.entitlement.model_dump(mode="json"):
            raise ValueError("dataset_entitlement_changed")
        if binding.selection.kind == "alias":
            alias = cursor.execute(
                "SELECT * FROM dataset_alias_revisions WHERE id = ?",
                (binding.selection.alias_revision_id,),
            ).fetchone()
            if alias is None:
                raise ValueError("dataset_alias_revision_unavailable")
            contract = database._dataset_alias_contract_from_row(alias)
            if (
                contract.project_id != spec.project_id
                or contract.asset_id != binding.asset_id
                or contract.snapshot_id != binding.snapshot_id
                or contract.revision_digest != binding.selection.alias_revision_digest
            ):
                raise ValueError("dataset_alias_revision_changed")


def _revalidate_stored_plan(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    plan: sqlite3.Row,
    companion: sqlite3.Row,
    spec: ExecutionPlanV2Spec,
    sharing_enabled: bool,
    now: datetime,
    skip_exclusivity: bool = False,
) -> None:
    """Revalidate one stored plan's spec against current state (INV-APPROVAL-3).

    ``companion`` only needs the column shape of ``execution_plan_v2_specs``
    (``canonical_argv_json`` / ``submit_observation_json`` /
    ``submit_observation_id`` / ``submit_observation_digest`` /
    ``job_command_sha256``) -- ``app.experiment_v2_store``'s
    ``experiment_plan_specs`` companion mirrors that shape exactly, so this
    function is reused unmodified for Experiment members (DG-EXPERIMENT-V1
    P2). ``skip_exclusivity`` defaults to ``False`` so every existing
    single-run call site is byte-identical; the Experiment decision path
    passes ``True`` because it has already verified exclusivity once per
    distinct target server against *external* state before any of its own
    Jobs exist, and a per-member re-check here would otherwise trip on a
    sibling member's own Job inserted earlier in the same transaction.
    """
    project_version = _verified_project_version(
        cursor,
        project_id=spec.project_id,
        project_version_id=spec.project_version.project_version_id,
    )
    if (
        project_version["git_commit"] != spec.project_version.git_commit
        or project_version["bundle_sha256"] != spec.project_version.bundle_sha256
    ):
        raise ValueError("project_version_changed")
    profile = database._run_template_head_by_id_from_cursor(
        cursor,
        project_id=spec.project_id,
        run_profile_id=spec.run_profile.run_profile_id,
    )
    if profile is None or profile["status"] != "approved":
        raise ValueError("run_profile_head_changed")
    classification, template = database._run_template_classification_from_cursor(
        cursor,
        profile,
    )
    if (
        classification != RUN_TEMPLATE_CLASSIFICATION_TYPED
        or template is None
        or template.spec_digest != spec.run_profile.spec_digest
        or template.environment_revision_id
        != spec.environment.environment_revision_id
    ):
        raise ValueError("run_profile_contract_changed")
    environment_row = cursor.execute(
        """
        SELECT environment.name, revision.*
        FROM environment_revisions AS revision
        JOIN project_environments AS environment
          ON environment.id = revision.environment_id
         AND environment.project_id = revision.project_id
        WHERE revision.id = ? AND revision.project_id = ?
          AND revision.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM environment_revisions AS newer
              WHERE newer.environment_id = revision.environment_id
                AND newer.revision > revision.revision
          )
        """,
        (spec.environment.environment_revision_id, spec.project_id),
    ).fetchone()
    if environment_row is None:
        raise ValueError("environment_head_changed")
    environment = database._environment_contract_from_row(environment_row)
    validate_secret_free_setup(environment)
    _validate_supported_execution_preflight(environment)
    if environment.revision_digest != spec.environment.revision_digest:
        raise ValueError("environment_contract_changed")
    if spec.project_defaults is not None:
        defaults_row = database._project_defaults_head_from_cursor(
            cursor,
            project_id=spec.project_id,
        )
        if defaults_row is None:
            raise ValueError("project_defaults_head_changed")
        defaults = database._project_defaults_contract_from_row(defaults_row)
        if (
            defaults.revision_id
            != spec.project_defaults.project_defaults_revision_id
            or defaults.revision_digest != spec.project_defaults.revision_digest
            or database._project_defaults_staleness_from_cursor(
                cursor,
                project_id=spec.project_id,
                contract=defaults,
            )
        ):
            raise ValueError("project_defaults_head_changed")
    compiled = compile_structured_argv(template, spec.parameter_values)
    if (
        compiled.canonical_argv_bytes.decode("utf-8")
        != companion["canonical_argv_json"]
        or compiled.argv_sha256 != spec.canonical_argv_sha256
        or compiled.argv_sha256 != spec.compiled_command_sha256
    ):
        raise ValueError("compiled_argv_changed")
    resources = template.resource_requirements.model_dump(mode="json")
    outputs = [output.model_dump(mode="json") for output in template.output_declarations]
    if (
        resources != spec.resource_requirements.model_dump(mode="json")
        or utf8_sha256(canonical_json(resources)) != spec.resource_requirements_digest
        or utf8_sha256(canonical_json(outputs)) != spec.output_declarations_digest
    ):
        raise ValueError("run_profile_contract_changed")
    _revalidate_stored_datasets(
        database,
        cursor,
        spec=spec,
        sharing_enabled=sharing_enabled,
    )

    project = cursor.execute(
        "SELECT name FROM projects WHERE id = ?",
        (spec.project_id,),
    ).fetchone()
    if project is None:
        raise ValueError("project_not_found")
    required_tags = set(environment.required_server_tags) | set(
        template.resource_requirements.required_tags
    )
    if spec.target.dispatch_policy_id is not None:
        policy, digest = _verified_policy(
            cursor,
            project_id=spec.project_id,
            policy_id=spec.target.dispatch_policy_id,
            run_profile_id=spec.run_profile.run_profile_id,
            dataset_bound=bool(spec.dataset_bindings),
            now=now,
        )
        if digest != spec.target.dispatch_policy_digest:
            raise ValueError("dispatch_policy_changed")
        allowed_servers = cast(list[str], json.loads(policy["allowed_servers"]))
        if spec.target.server_name not in allowed_servers:
            raise ValueError("dispatch_policy_changed")
        if policy["require_tag"] is not None:
            required_tags.add(str(policy["require_tag"]))
        if _active_policy_placement_count(
            cursor,
            policy_id=str(policy["id"]),
        ) >= int(policy["max_concurrent_placements"]):
            raise ValueError("dispatch_policy_concurrency_exhausted")
    revision = cursor.execute(
        """
        SELECT revision.*, creator.kind AS creator_kind,
               creator.status AS creator_status,
               creator.payload AS creator_payload,
               creator.payload_sha256 AS creator_payload_sha256,
               creator.payload_contract_version AS creator_contract
        FROM server_config_revisions AS revision
        JOIN approvals AS creator ON creator.id = revision.created_by_approval_id
        WHERE revision.id = ?
        """,
        (spec.target.server_config_revision_id,),
    ).fetchone()
    if revision is None:
        raise ValueError("target_revision_unavailable")
    candidate = _candidate_for_revision(
        cursor,
        project_id=spec.project_id,
        project_name=str(project["name"]),
        project_version=project_version,
        revision=revision,
        required_tags=required_tags,
        requirements=resources,
        now=now,
        skip_exclusivity=skip_exclusivity,
    )
    if (
        revision["server_name"] != spec.target.server_name
        or revision["target_identity_sha256"] != spec.target.target_identity_sha256
        or candidate["instance"]["id"] != spec.project_instance.project_instance_id
        or candidate["checkout_digest"]
        != spec.project_instance.checkout_evidence_digest
    ):
        raise ValueError("target_contract_changed")
    command = build_bash_argv_bridge(
        checkout_path=str(candidate["instance"]["path"]),
        git_commit=spec.project_version.git_commit,
        setup_command=environment.setup_command,
        argv=compiled.argv,
    )
    if (
        command != plan["command"]
        or utf8_sha256(command) != spec.job_command_sha256
        or companion["job_command_sha256"] != spec.job_command_sha256
    ):
        raise ValueError("job_command_changed")
    raw_observation = str(companion["submit_observation_json"])
    try:
        stored_observation = SubmitObservationProvenance.model_validate_json(
            raw_observation
        )
    except ValueError:
        raise ValueError("submit_observation_invalid") from None
    if (
        canonical_json(stored_observation.model_dump(mode="json"))
        != raw_observation
        or utf8_sha256(raw_observation) != companion["submit_observation_digest"]
        or stored_observation.observation_id != companion["submit_observation_id"]
        or stored_observation.server_name != spec.target.server_name
    ):
        raise ValueError("submit_observation_invalid")


def _reject_stale_in_transaction(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    approval: sqlite3.Row,
    decision_actor_id: str,
    decision_mechanism: str,
) -> dict[str, Any]:
    decided_at = database._sqlite_now(cursor)
    cursor.execute(
        """
        UPDATE approvals
        SET status = 'rejected', decided_at = ?, note = ?,
            decision_actor_id = ?, decision_mechanism = ?
        WHERE id = ? AND kind = ? AND status = 'pending'
        """,
        (
            decided_at,
            "execution_plan_stale",
            decision_actor_id,
            decision_mechanism,
            approval["id"],
            EXECUTION_PLAN_V2_APPROVAL_KIND,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("execution_plan_decision_conflict")
    database._append_approval_decided_audit(
        cursor,
        approval_id=int(approval["id"]),
        kind=EXECUTION_PLAN_V2_APPROVAL_KIND,
        status="rejected",
        decision_actor_id=decision_actor_id,
        decision_mechanism=decision_mechanism,
    )
    database.append_durable_audit_event_in_transaction(
        cursor,
        action="execution_plan_v2_rejected_stale",
        params={"reason_code": "execution_plan_stale"},
        result="rejected",
        actor_id=decision_actor_id,
        actor_kind=database._durable_actor_kind(
            cursor,
            decision_actor_id,
            fallback="actor",
        ),
        authentication=decision_mechanism,
        resource_type="approval",
        resource_id=str(approval["id"]),
        approval_id=int(approval["id"]),
        event_id=f"execution-plan-v2:{approval['id']}:stale-rejected",
    )
    return {
        "approval_id": int(approval["id"]),
        "status": "rejected",
        "reason_code": "execution_plan_stale",
        "job_id": None,
        "created": False,
    }


def apply_execution_plan_v2_decision_in_transaction(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    approval_id: int,
    decision_actor_id: str,
    decision_mechanism: str,
    sharing_enabled: bool,
    note: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    existing_approval = cursor.execute(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()
    if (
        existing_approval is None
        or existing_approval["kind"] != EXECUTION_PLAN_V2_APPROVAL_KIND
    ):
        raise ValueError("execution_plan_approval_unavailable")
    if existing_approval["status"] == "approved":
        _approval, plan, _companion, payload, _spec = _verified_stored_plan(
            database,
            cursor,
            approval_id=approval_id,
        )
        return {
            "approval_id": approval_id,
            "execution_plan_id": payload.execution_plan_id,
            "status": "approved",
            "job_id": int(plan["job_id"]),
            "created": False,
        }
    if existing_approval["status"] != "pending":
        raise ValueError("execution_plan_approval_not_pending")
    scope_row = cursor.execute(
        """
        SELECT spec.project_id
        FROM execution_plan_v2_specs AS spec
        WHERE spec.created_approval_id = ?
        """,
        (approval_id,),
    ).fetchone()
    if scope_row is None:
        raise ValueError("execution_plan_companion_unavailable")
    decision_project_id = str(scope_row["project_id"])
    requester_id = str(existing_approval["requester_actor_id"])
    if (
        not database.allow_high_risk_self_approval
        and requester_id == decision_actor_id
    ):
        raise ValueError("high_risk_self_decision")
    decider: Actor = database._validate_environment_project_actor(
        cursor,
        project_id=decision_project_id,
        actor_id=decision_actor_id,
        purpose="decider",
        require_readiness=True,
    )
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None or observed_now.utcoffset() is None:
        raise ValueError("decision_now_must_be_timezone_aware")
    observed_now = observed_now.astimezone(timezone.utc)
    try:
        approval, plan, companion, payload, spec = _verified_stored_plan(
            database,
            cursor,
            approval_id=approval_id,
        )
        if payload.project_id != decision_project_id:
            raise ValueError("execution_plan_project_scope_changed")
        database._validate_environment_project_actor(
            cursor,
            project_id=payload.project_id,
            actor_id=requester_id,
            purpose="requester",
            require_readiness=True,
        )
        _revalidate_stored_plan(
            database,
            cursor,
            plan=plan,
            companion=companion,
            spec=spec,
            sharing_enabled=sharing_enabled,
            now=observed_now,
        )
    except (TypeError, ValueError, json.JSONDecodeError, sqlite3.IntegrityError):
        return _reject_stale_in_transaction(
            database,
            cursor,
            approval=existing_approval,
            decision_actor_id=decision_actor_id,
            decision_mechanism=decision_mechanism,
        )

    cursor.execute(
        """
        INSERT INTO jobs (
            type, project, command, require_tag, pin_server, depends_on,
            gpus_needed, status, priority, created_at,
            execution_approval_id, approved_payload_sha256,
            execution_contract_version, execution_contract_role,
            approved_command_sha256
        ) VALUES (
            'adhoc', ?, ?, NULL, ?, '[]', ?, 'queued', 'normal', ?,
            ?, ?, ?, 'main', ?
        )
        """,
        (
            plan["project_name"],
            plan["command"],
            spec.target.server_name,
            spec.resource_requirements.min_gpu_count,
            database._sqlite_now(cursor),
            approval_id,
            approval["payload_sha256"],
            spec.contract_version,
            spec.job_command_sha256,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - sqlite INSERT invariant
        raise RuntimeError("execution_plan_job_insert_failed")
    job_id = int(cursor.lastrowid)
    cursor.execute(
        "UPDATE execution_plans SET job_id = ? WHERE id = ? AND job_id IS NULL",
        (job_id, payload.execution_plan_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("execution_plan_materialization_conflict")
    decided_at = database._sqlite_now(cursor)
    cursor.execute(
        """
        UPDATE approvals
        SET status = 'approved', decided_at = ?, note = ?,
            decision_actor_id = ?, decision_mechanism = ?,
            materialization_started_at = ?
        WHERE id = ? AND kind = ? AND status = 'pending'
        """,
        (
            decided_at,
            note,
            decision_actor_id,
            decision_mechanism,
            decided_at,
            approval_id,
            EXECUTION_PLAN_V2_APPROVAL_KIND,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("execution_plan_decision_conflict")
    database._append_approval_decided_audit(
        cursor,
        approval_id=approval_id,
        kind=EXECUTION_PLAN_V2_APPROVAL_KIND,
        status="approved",
        decision_actor_id=decision_actor_id,
        decision_actor_kind=decider.actor_type.value,
        decision_mechanism=decision_mechanism,
    )
    database.append_durable_audit_event_in_transaction(
        cursor,
        action="execution_plan_v2_job_materialized",
        params={
            "execution_plan_id": payload.execution_plan_id,
            "job_command_sha256": spec.job_command_sha256,
            "job_id": job_id,
            "server_config_revision_id": spec.target.server_config_revision_id,
        },
        result="queued",
        actor_id=decision_actor_id,
        actor_kind=decider.actor_type.value,
        authentication=decision_mechanism,
        resource_type="job",
        resource_id=str(job_id),
        approval_id=approval_id,
        event_id=f"execution-plan-v2:{payload.execution_plan_id}:job",
    )
    return {
        "approval_id": approval_id,
        "execution_plan_id": payload.execution_plan_id,
        "status": "approved",
        "job_id": job_id,
        "created": True,
    }


def reject_execution_plan_v2_decision_in_transaction(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    approval_id: int,
    decision_actor_id: str,
    decision_mechanism: str,
    note: str | None = None,
) -> dict[str, Any]:
    approval, _plan, _companion, payload, _spec = _verified_stored_plan(
        database,
        cursor,
        approval_id=approval_id,
    )
    if approval["status"] != "pending":
        raise ValueError("execution_plan_approval_not_pending")
    requester_id = str(approval["requester_actor_id"])
    if (
        not database.allow_high_risk_self_approval
        and requester_id == decision_actor_id
    ):
        raise ValueError("high_risk_self_decision")
    database._validate_environment_project_actor(
        cursor,
        project_id=payload.project_id,
        actor_id=requester_id,
        purpose="requester",
        require_readiness=True,
    )
    decider = database._validate_environment_project_actor(
        cursor,
        project_id=payload.project_id,
        actor_id=decision_actor_id,
        purpose="decider",
        require_readiness=True,
    )
    decided_at = database._sqlite_now(cursor)
    cursor.execute(
        """
        UPDATE approvals
        SET status = 'rejected', decided_at = ?, note = ?,
            decision_actor_id = ?, decision_mechanism = ?
        WHERE id = ? AND kind = ? AND status = 'pending'
        """,
        (
            decided_at,
            note,
            decision_actor_id,
            decision_mechanism,
            approval_id,
            EXECUTION_PLAN_V2_APPROVAL_KIND,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("execution_plan_decision_conflict")
    database._append_approval_decided_audit(
        cursor,
        approval_id=approval_id,
        kind=EXECUTION_PLAN_V2_APPROVAL_KIND,
        status="rejected",
        decision_actor_id=decision_actor_id,
        decision_actor_kind=decider.actor_type.value,
        decision_mechanism=decision_mechanism,
    )
    return {
        "approval_id": approval_id,
        "execution_plan_id": payload.execution_plan_id,
        "status": "rejected",
        "job_id": None,
        "created": False,
    }


def verify_execution_plan_v2_approval_in_cursor(
    database: "Database",
    cursor: sqlite3.Cursor,
    approval_id: int,
) -> dict[str, Any] | None:
    """Verify a stored v2 approval inside the caller's read transaction."""

    try:
        approval, plan, companion, payload, spec = _verified_stored_plan(
            database,
            cursor,
            approval_id=approval_id,
        )
    except ValueError:
        return None
    return {
        "approval": dict(approval),
        "execution_plan": dict(plan),
        "companion": dict(companion),
        "approval_payload": payload,
        "spec": spec,
    }


def verify_execution_plan_v2_in_cursor(
    database: "Database",
    cursor: sqlite3.Cursor,
    execution_plan_id: str,
) -> dict[str, Any] | None:
    """Verify one Product v2 plan by its canonical Run identity.

    Product Run read surfaces use the same closed verifier as approval review
    and approve-time materialisation.  A row that carries a v2 marker but does
    not pass this function must never fall back to a legacy adapter.
    """

    row = cursor.execute(
        """
        SELECT request_approval_id
        FROM execution_plans
        WHERE id = ?
        """,
        (execution_plan_id,),
    ).fetchone()
    if row is None or row["request_approval_id"] is None:
        return None
    verified = verify_execution_plan_v2_approval_in_cursor(
        database,
        cursor,
        int(row["request_approval_id"]),
    )
    if (
        verified is None
        or verified["execution_plan"]["id"] != execution_plan_id
    ):
        return None
    return verified


def get_verified_execution_plan_v2_approval(
    database: "Database",
    approval_id: int,
) -> dict[str, Any] | None:
    with database.cursor() as cursor:
        return verify_execution_plan_v2_approval_in_cursor(
            database,
            cursor,
            approval_id,
        )


def get_verified_execution_plan_v2_job_target_revision(
    database: "Database",
    job_id: int,
) -> str | None:
    """Return the immutable target revision for one verified v2 Job.

    Scheduler selection uses this before contacting an executor.  Missing or
    inconsistent linkage fails closed as ``None`` so a corrupt Product v2 Job
    cannot fall through to a current-revision or legacy dispatch path.
    """

    with database.cursor() as cursor:
        job = cursor.execute(
            """
            SELECT execution_approval_id
            FROM jobs
            WHERE id = ? AND execution_contract_version = ?
            """,
            (job_id, EXECUTION_PLAN_V2_CONTRACT_VERSION),
        ).fetchone()
        if job is None or job["execution_approval_id"] is None:
            return None
        try:
            _approval, plan, _companion, _payload, spec = _verified_stored_plan(
                database,
                cursor,
                approval_id=int(job["execution_approval_id"]),
            )
        except (TypeError, ValueError):
            return None
        if plan["job_id"] != job_id:
            return None
        return spec.target.server_config_revision_id


def get_execution_plan_v2_request_result(
    database: "Database",
    execution_plan_id: str,
) -> dict[str, Any] | None:
    """Load the committed submit result without exposing command or host data."""

    with database.cursor() as cursor:
        row = cursor.execute(
            """
            SELECT plan.id AS execution_plan_id, plan.job_id,
                   plan.request_approval_id, plan.plan_digest,
                   approval.status AS approval_status
            FROM execution_plans AS plan
            JOIN execution_plan_v2_specs AS spec
              ON spec.execution_plan_id = plan.id
            JOIN approvals AS approval ON approval.id = plan.request_approval_id
            WHERE plan.id = ? AND spec.created_approval_id = plan.request_approval_id
            """,
            (execution_plan_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            _verified_stored_plan(
                database,
                cursor,
                approval_id=int(row["request_approval_id"]),
            )
        except ValueError:
            raise ValueError("execution_plan_request_result_invalid") from None
        return {
            "execution_plan_id": str(row["execution_plan_id"]),
            "approval_id": int(row["request_approval_id"]),
            "plan_digest": str(row["plan_digest"]),
            "status": str(row["approval_status"]),
            "job_id": (
                int(row["job_id"]) if row["job_id"] is not None else None
            ),
        }


__all__ = [
    "apply_execution_plan_v2_decision_in_transaction",
    "create_execution_plan_v2_request_in_transaction",
    "get_verified_execution_plan_v2_approval",
    "get_verified_execution_plan_v2_job_target_revision",
    "get_execution_plan_v2_request_result",
    "reject_execution_plan_v2_decision_in_transaction",
    "resolve_execution_plan_v2",
    "verify_execution_plan_v2_approval_in_cursor",
    "verify_execution_plan_v2_in_cursor",
]
