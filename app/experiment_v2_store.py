"""SQLite resolution and materialisation for Experiment v2 (DG-EXPERIMENT-V1, P2).

Approved 2026-08-25 (`docs/DG_EXPERIMENT_V1_DECISION.md` EX-1..EX-7, plus the
"P2 實作註記" migration-15 ruling) and `docs/DECISIONS.md` same-day entry.

An Experiment is a container over N Product v2 ExecutionPlans, all resolved
and decided together under one approval (`experiment_create_v2`). This module
deliberately reuses `app.execution_plan_v2_store`'s resolver, revalidation,
and exclusivity primitives byte-for-byte instead of re-implementing them:

* Request time runs the existing single-run resolver
  (`resolve_execution_plan_v2`) once per expanded matrix combination, inside
  the caller's transaction cursor, so every member gets exactly the same
  validation a lone `execution_plan_v2` request gets.
* Decision time reuses `_revalidate_stored_plan` / `_revalidate_stored_datasets`
  unmodified in logic, pointed at a member's `experiment_plan_specs` row
  instead of `execution_plan_v2_specs` -- the two companion tables share an
  identical column shape for exactly this reason (see
  `app.db.apply_experiment_plan_specs_migration`).
* Batch-aware exclusivity (EX-3): `_worker_is_exclusive` itself is never
  changed. At decision time this module checks each *distinct* target server
  used by the experiment exactly once, before any of the experiment's own
  Jobs exist in this transaction, then revalidates every member with
  `skip_exclusivity=True` so a sibling member's just-inserted Job cannot make
  a shared server look non-exclusive to itself.

Nothing here ever touches `execution_plan_v2_specs`, its triggers, or the
`execution_plans` schema -- see `experiments` / `experiment_plan_members`
(migration 14) and `experiment_plan_specs` (migration 15), all additive.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.execution_contract import canonical_json, utf8_sha256
from app.execution_plan_v2 import (
    DatasetSelection,
    ExecutionPlanV2Request,
    ExecutionPlanV2Spec,
    ServerConfigTargetSelection,
    SubmitObservationProvenance,
    TemplateSelection,
    parse_execution_plan_v2_spec,
)
from app.execution_plan_v2_store import (
    _revalidate_stored_plan,
    _worker_is_exclusive,
    resolve_execution_plan_v2,
)
from app.experiment_v2 import (
    EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION,
    EXPERIMENT_V2_APPROVAL_KIND,
    ExperimentGuard,
    ExperimentMatrix,
    ExperimentV2ApprovalPayload,
    expand_matrix,
    experiment_v2_approval_payload_digest,
    parse_experiment_v2_approval_payload,
)
from app.identity import Actor

if TYPE_CHECKING:
    from app.db import Database


def _current_server_config_revision_id(cursor: sqlite3.Cursor, server_name: str) -> str:
    """Resolve a guard-declared target server name to its active revision.

    Mirrors the ``server_name`` + ``publication_state = 'active'`` uniqueness
    the publication pipeline itself relies on (`app/db.py` server-config
    mutation apply path). No active revision is a fail-closed request
    failure -- never a silent skip of that target.
    """

    row = cursor.execute(
        """
        SELECT id FROM server_config_revisions
        WHERE server_name = ? AND publication_state = 'active'
        """,
        (server_name,),
    ).fetchone()
    if row is None:
        raise ValueError("target_server_unavailable")
    return str(row["id"])


def create_experiment_v2_request_in_transaction(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    project_id: str,
    template_selection: TemplateSelection,
    dataset_selection: DatasetSelection,
    project_version_id: str,
    matrix: ExperimentMatrix,
    guard: ExperimentGuard,
    requester_actor_id: str,
    sharing_enabled: bool,
    experiment_v2_enabled: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """EX-1: expand the matrix, resolve N full specs, and request one approval.

    All-or-nothing: any combination that fails to resolve raises before any
    row is written (the caller's transaction is never partially committed).
    """

    if not experiment_v2_enabled:
        raise ValueError("experiment_v2_disabled")

    combinations = expand_matrix(matrix)
    run_count = len(combinations)
    if guard.total_runs != run_count:
        raise ValueError("experiment_guard_run_count_mismatch")

    target_servers = guard.target_servers
    target_revision_ids = {
        server_name: _current_server_config_revision_id(cursor, server_name)
        for server_name in target_servers
    }

    resolved_members: list[dict[str, Any]] = []
    for index, combination in enumerate(combinations):
        target_server = target_servers[index % len(target_servers)]
        combo_request = ExecutionPlanV2Request(
            project_version_id=project_version_id,
            template_selection=template_selection,
            parameter_overrides=combination,
            dataset_selection=dataset_selection,
            target_selection=ServerConfigTargetSelection(
                kind="server_config_revision",
                server_config_revision_id=target_revision_ids[target_server],
            ),
        )
        # Full single-run resolution, unmodified -- see module docstring.
        resolved = resolve_execution_plan_v2(
            database,
            project_id=project_id,
            request=combo_request,
            requester_actor_id=requester_actor_id,
            sharing_enabled=sharing_enabled,
            now=now,
            cursor=cursor,
        )
        spec: ExecutionPlanV2Spec = resolved["spec"]
        if spec.target.server_name != target_server:
            raise ValueError("experiment_target_resolution_mismatch")
        resolved_members.append(
            {"target_server": target_server, "resolved": resolved, "spec": spec}
        )

    first_spec = resolved_members[0]["spec"]
    for member in resolved_members[1:]:
        spec = member["spec"]
        if (
            spec.project_version != first_spec.project_version
            or spec.environment != first_spec.environment
            or spec.run_profile != first_spec.run_profile
            or spec.dataset_none != first_spec.dataset_none
            or spec.dataset_bindings != first_spec.dataset_bindings
        ):
            raise ValueError("experiment_member_shared_selection_mismatch")

    plan_digests = [member["spec"].plan_digest for member in resolved_members]
    approval_payload = ExperimentV2ApprovalPayload(
        project_id=project_id,
        matrix=matrix,
        guard=guard,
        plan_digests=plan_digests,
        run_count=run_count,
    )
    payload_json = canonical_json(approval_payload.model_dump(mode="json"))
    payload_digest = experiment_v2_approval_payload_digest(approval_payload)
    created_at = database._sqlite_now(cursor)

    cursor.execute(
        """
        INSERT INTO approvals (
            kind, payload, status, created_at, requester_actor_id,
            payload_sha256, payload_contract_version, payload_immutable_at
        ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            EXPERIMENT_V2_APPROVAL_KIND,
            payload_json,
            created_at,
            requester_actor_id,
            payload_digest,
            EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION,
            created_at,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - sqlite INSERT invariant
        raise RuntimeError("experiment_approval_insert_failed")
    approval_id = int(cursor.lastrowid)
    database._append_approval_created_audit(
        cursor,
        approval_id=approval_id,
        kind=EXPERIMENT_V2_APPROVAL_KIND,
        requester_actor_id=requester_actor_id,
    )

    matrix_json = canonical_json(matrix.model_dump(mode="json"))
    guard_json = canonical_json(guard.model_dump(mode="json"))
    cursor.execute(
        """
        INSERT INTO experiments (
            project_id, approval_id, matrix_json, guard_json, run_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, approval_id, matrix_json, guard_json, run_count, created_at),
    )
    if cursor.lastrowid is None:  # pragma: no cover - sqlite INSERT invariant
        raise RuntimeError("experiment_insert_failed")
    experiment_id = int(cursor.lastrowid)

    project_row = cursor.execute(
        "SELECT name FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if project_row is None:
        raise ValueError("project_not_found")
    project_name = str(project_row["name"])

    plan_ids: list[str] = []
    for member in resolved_members:
        spec = member["spec"]
        resolved = member["resolved"]
        plan_id = str(uuid.uuid4())
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
        cursor.execute(
            "INSERT INTO experiment_plan_members (experiment_id, plan_id) VALUES (?, ?)",
            (experiment_id, plan_id),
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
            INSERT INTO experiment_plan_specs (
                execution_plan_id, experiment_id, project_id, contract_version,
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
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                plan_id,
                experiment_id,
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
        plan_ids.append(plan_id)

    database.append_durable_audit_event_in_transaction(
        cursor,
        action="experiment_v2_requested",
        params={
            "contract_version": EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION,
            "project_id": project_id,
            "run_count": run_count,
            "target_servers": sorted(target_servers),
        },
        result="pending_approval",
        actor_id=requester_actor_id,
        actor_kind=database._durable_actor_kind(
            cursor,
            requester_actor_id,
            fallback="actor",
        ),
        authentication="product_rbac_v2",
        resource_type="experiment",
        resource_id=str(experiment_id),
        approval_id=approval_id,
        event_id=f"experiment-v2:{experiment_id}:requested",
    )

    return {
        "experiment_id": experiment_id,
        "approval_id": approval_id,
        "run_count": run_count,
        "plan_ids": plan_ids,
        "plan_digests": plan_digests,
        "payload_sha256": payload_digest,
    }


def _verified_experiment(
    cursor: sqlite3.Cursor,
    *,
    approval_id: int,
) -> tuple[sqlite3.Row, sqlite3.Row, ExperimentV2ApprovalPayload]:
    """Load and verify the approval/payload/`experiments` row linkage."""

    approval = cursor.execute(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()
    if approval is None or approval["kind"] != EXPERIMENT_V2_APPROVAL_KIND:
        raise ValueError("experiment_approval_unavailable")
    raw_payload = str(approval["payload"])
    if (
        approval["payload_contract_version"] != EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION
        or approval["payload_immutable_at"] is None
        or not isinstance(approval["payload_sha256"], str)
        or utf8_sha256(raw_payload) != approval["payload_sha256"]
    ):
        raise ValueError("experiment_approval_invalid")
    try:
        payload = parse_experiment_v2_approval_payload(json.loads(raw_payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("experiment_approval_invalid") from None
    if (
        canonical_json(payload.model_dump(mode="json")) != raw_payload
        or experiment_v2_approval_payload_digest(payload) != approval["payload_sha256"]
    ):
        raise ValueError("experiment_approval_invalid")
    experiment = cursor.execute(
        "SELECT * FROM experiments WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    if (
        experiment is None
        or experiment["project_id"] != payload.project_id
        or int(experiment["run_count"]) != payload.run_count
        or canonical_json(payload.matrix.model_dump(mode="json"))
        != experiment["matrix_json"]
        or canonical_json(payload.guard.model_dump(mode="json"))
        != experiment["guard_json"]
    ):
        raise ValueError("experiment_companion_unavailable")
    return approval, experiment, payload


def _verified_experiment_member(
    cursor: sqlite3.Cursor,
    *,
    approval: sqlite3.Row,
    experiment: sqlite3.Row,
    payload: ExperimentV2ApprovalPayload,
    plan_id: str,
) -> tuple[sqlite3.Row, sqlite3.Row, ExecutionPlanV2Spec]:
    """Load and verify one member's plan/companion/spec triple.

    Mirrors the cross-checks `execution_plan_v2_store._verify_stored_plan_
    projection` runs for a single-run plan, adapted for N members sharing one
    container approval instead of a 1:1 approval-per-plan.
    """

    member = cursor.execute(
        "SELECT 1 FROM experiment_plan_members WHERE experiment_id = ? AND plan_id = ?",
        (experiment["id"], plan_id),
    ).fetchone()
    if member is None:
        raise ValueError("experiment_member_unavailable")
    plan = cursor.execute(
        "SELECT * FROM execution_plans WHERE id = ?",
        (plan_id,),
    ).fetchone()
    companion = cursor.execute(
        "SELECT * FROM experiment_plan_specs WHERE execution_plan_id = ?",
        (plan_id,),
    ).fetchone()
    if plan is None or companion is None:
        raise ValueError("experiment_member_companion_unavailable")
    if (
        int(companion["experiment_id"]) != int(experiment["id"])
        or int(companion["created_approval_id"]) != int(approval["id"])
        or plan["request_approval_id"] != approval["id"]
        or companion["plan_digest"] not in payload.plan_digests
    ):
        raise ValueError("experiment_member_contract_mismatch")
    raw_spec = str(companion["canonical_spec_json"])
    try:
        spec = parse_execution_plan_v2_spec(json.loads(raw_spec))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("experiment_member_spec_invalid") from None
    if (
        canonical_json(spec.model_dump(mode="json")) != raw_spec
        or spec.plan_digest != companion["plan_digest"]
    ):
        raise ValueError("experiment_member_spec_invalid")

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
    command = plan["command"]
    if (
        plan["plan_digest"] != spec.plan_digest
        or plan["contract_version"] != spec.contract_version
        or plan["reproducible"] != legacy_reproducible
        or plan["project_version_id"] != spec.project_version.project_version_id
        or plan["run_profile_id"] != spec.run_profile.run_profile_id
        or plan["dataset_snapshot_id"] != legacy_snapshot_id
        or plan["dataset_none"] != legacy_dataset_none
        or plan["server_config_revision_id"] != spec.target.server_config_revision_id
        or plan["command_sha256"] != spec.job_command_sha256
        or not isinstance(command, str)
        or utf8_sha256(command) != spec.job_command_sha256
    ):
        raise ValueError("experiment_member_contract_mismatch")
    return plan, companion, spec


def _load_experiment_members(
    cursor: sqlite3.Cursor,
    *,
    approval: sqlite3.Row,
    experiment: sqlite3.Row,
    payload: ExperimentV2ApprovalPayload,
) -> list[tuple[sqlite3.Row, sqlite3.Row, ExecutionPlanV2Spec]]:
    plan_ids = [
        str(row["plan_id"])
        for row in cursor.execute(
            "SELECT plan_id FROM experiment_plan_members WHERE experiment_id = ? "
            "ORDER BY plan_id",
            (experiment["id"],),
        ).fetchall()
    ]
    if len(plan_ids) != int(experiment["run_count"]):
        raise ValueError("experiment_member_count_mismatch")
    return [
        _verified_experiment_member(
            cursor,
            approval=approval,
            experiment=experiment,
            payload=payload,
            plan_id=plan_id,
        )
        for plan_id in plan_ids
    ]


def _reject_stale_experiment_in_transaction(
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
            "experiment_stale",
            decision_actor_id,
            decision_mechanism,
            approval["id"],
            EXPERIMENT_V2_APPROVAL_KIND,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("experiment_decision_conflict")
    database._append_approval_decided_audit(
        cursor,
        approval_id=int(approval["id"]),
        kind=EXPERIMENT_V2_APPROVAL_KIND,
        status="rejected",
        decision_actor_id=decision_actor_id,
        decision_mechanism=decision_mechanism,
    )
    database.append_durable_audit_event_in_transaction(
        cursor,
        action="experiment_v2_rejected_stale",
        params={"reason_code": "experiment_stale"},
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
        event_id=f"experiment-v2:{approval['id']}:stale-rejected",
    )
    return {
        "approval_id": int(approval["id"]),
        "status": "rejected",
        "reason_code": "experiment_stale",
        "job_ids": [],
        "created": False,
    }


def apply_experiment_v2_decision_in_transaction(
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
    """EX-2: approve materializes all N (plan, Job) pairs atomically or none.

    Batch-aware exclusivity (EX-3): every distinct target server is checked
    against `_worker_is_exclusive` exactly once, before any of this
    experiment's Jobs exist in the transaction; per-member revalidation then
    runs with that check already satisfied (`skip_exclusivity=True`) so a
    sibling member's own freshly inserted Job cannot self-block a shared
    server. `_worker_is_exclusive` itself is untouched.
    """

    existing_approval = cursor.execute(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()
    if existing_approval is None or existing_approval["kind"] != EXPERIMENT_V2_APPROVAL_KIND:
        raise ValueError("experiment_approval_unavailable")

    if existing_approval["status"] == "approved":
        approval, experiment, payload = _verified_experiment(
            cursor,
            approval_id=approval_id,
        )
        members = _load_experiment_members(
            cursor,
            approval=approval,
            experiment=experiment,
            payload=payload,
        )
        job_ids: list[int] = []
        for plan, _companion, _spec in members:
            if plan["job_id"] is None:
                raise ValueError("experiment_materialization_incomplete")
            job_ids.append(int(plan["job_id"]))
        return {
            "approval_id": approval_id,
            "experiment_id": int(experiment["id"]),
            "status": "approved",
            "job_ids": job_ids,
            "created": False,
        }
    if existing_approval["status"] != "pending":
        raise ValueError("experiment_approval_not_pending")

    experiment_scope = cursor.execute(
        "SELECT project_id FROM experiments WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    if experiment_scope is None:
        raise ValueError("experiment_companion_unavailable")
    decision_project_id = str(experiment_scope["project_id"])
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
        approval, experiment, payload = _verified_experiment(
            cursor,
            approval_id=approval_id,
        )
        if payload.project_id != decision_project_id:
            raise ValueError("experiment_project_scope_changed")
        database._validate_environment_project_actor(
            cursor,
            project_id=payload.project_id,
            actor_id=requester_id,
            purpose="requester",
            require_readiness=True,
        )
        members = _load_experiment_members(
            cursor,
            approval=approval,
            experiment=experiment,
            payload=payload,
        )
        distinct_servers = sorted(
            {spec.target.server_name for _plan, _companion, spec in members}
        )
        for server_name in distinct_servers:
            if not _worker_is_exclusive(cursor, server_name):
                raise ValueError("target_worker_not_exclusive")
        for plan, companion, spec in members:
            _revalidate_stored_plan(
                database,
                cursor,
                plan=plan,
                companion=companion,
                spec=spec,
                sharing_enabled=sharing_enabled,
                now=observed_now,
                skip_exclusivity=True,
            )
    except (TypeError, ValueError, json.JSONDecodeError, sqlite3.IntegrityError):
        return _reject_stale_experiment_in_transaction(
            database,
            cursor,
            approval=existing_approval,
            decision_actor_id=decision_actor_id,
            decision_mechanism=decision_mechanism,
        )

    job_ids = []
    for plan, _companion, spec in members:
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
                ?, ?, ?, ?, ?
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
                # Distinct from single-run: the container approval is not
                # kind `execution_plan_v2`, so `jobs_execution_pin_insert_
                # guard` (app/execution_attempt_schema.py) requires this to
                # equal the approval's own payload_contract_version.
                EXPERIMENT_V2_APPROVAL_CONTRACT_VERSION,
                # Distinct per member so the partial UNIQUE index on
                # (execution_approval_id, execution_contract_role) --
                # `idx_jobs_execution_contract_role` -- allows N Jobs to
                # share one container approval id.
                plan["id"],
                spec.job_command_sha256,
            ),
        )
        if cursor.lastrowid is None:  # pragma: no cover - sqlite INSERT invariant
            raise RuntimeError("experiment_job_insert_failed")
        job_id = int(cursor.lastrowid)
        cursor.execute(
            "UPDATE execution_plans SET job_id = ? WHERE id = ? AND job_id IS NULL",
            (job_id, plan["id"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("experiment_materialization_conflict")
        job_ids.append(job_id)
        database.append_durable_audit_event_in_transaction(
            cursor,
            action="experiment_v2_job_materialized",
            params={
                "execution_plan_id": plan["id"],
                "experiment_id": int(experiment["id"]),
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
            event_id=f"experiment-v2:{plan['id']}:job",
        )

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
            EXPERIMENT_V2_APPROVAL_KIND,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("experiment_decision_conflict")
    database._append_approval_decided_audit(
        cursor,
        approval_id=approval_id,
        kind=EXPERIMENT_V2_APPROVAL_KIND,
        status="approved",
        decision_actor_id=decision_actor_id,
        decision_actor_kind=decider.actor_type.value,
        decision_mechanism=decision_mechanism,
    )
    return {
        "approval_id": approval_id,
        "experiment_id": int(experiment["id"]),
        "status": "approved",
        "job_ids": job_ids,
        "created": True,
    }


def reject_experiment_v2_decision_in_transaction(
    database: "Database",
    cursor: sqlite3.Cursor,
    *,
    approval_id: int,
    decision_actor_id: str,
    decision_mechanism: str,
    note: str | None = None,
) -> dict[str, Any]:
    approval, experiment, payload = _verified_experiment(
        cursor,
        approval_id=approval_id,
    )
    if approval["status"] != "pending":
        raise ValueError("experiment_approval_not_pending")
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
            EXPERIMENT_V2_APPROVAL_KIND,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("experiment_decision_conflict")
    database._append_approval_decided_audit(
        cursor,
        approval_id=approval_id,
        kind=EXPERIMENT_V2_APPROVAL_KIND,
        status="rejected",
        decision_actor_id=decision_actor_id,
        decision_actor_kind=decider.actor_type.value,
        decision_mechanism=decision_mechanism,
    )
    return {
        "approval_id": approval_id,
        "experiment_id": int(experiment["id"]),
        "status": "rejected",
        "job_ids": [],
        "created": False,
    }


def verify_experiment_v2_approval_in_cursor(
    cursor: sqlite3.Cursor,
    approval_id: int,
) -> dict[str, Any] | None:
    """Verify a stored Experiment approval inside the caller's transaction."""

    try:
        approval, experiment, payload = _verified_experiment(
            cursor,
            approval_id=approval_id,
        )
        members = _load_experiment_members(
            cursor,
            approval=approval,
            experiment=experiment,
            payload=payload,
        )
    except ValueError:
        return None
    return {
        "approval": dict(approval),
        "experiment": dict(experiment),
        "approval_payload": payload,
        "members": [
            {
                "execution_plan_id": str(plan["id"]),
                "job_id": (
                    int(plan["job_id"]) if plan["job_id"] is not None else None
                ),
                "plan_digest": str(plan["plan_digest"]),
                "server_name": spec.target.server_name,
                "spec": spec,
            }
            for plan, _companion, spec in members
        ],
    }


def get_experiment_v2_by_approval(
    database: "Database",
    approval_id: int,
) -> dict[str, Any] | None:
    with database.cursor() as cursor:
        verified = verify_experiment_v2_approval_in_cursor(cursor, approval_id)
        if verified is None:
            return None
        return {
            "experiment_id": int(verified["experiment"]["id"]),
            "project_id": str(verified["experiment"]["project_id"]),
            "approval_id": int(verified["approval"]["id"]),
            "status": str(verified["approval"]["status"]),
            "run_count": int(verified["experiment"]["run_count"]),
            "payload": verified["approval_payload"],
            "members": [
                {
                    "execution_plan_id": member["execution_plan_id"],
                    "job_id": member["job_id"],
                    "plan_digest": member["plan_digest"],
                    "server_name": member["server_name"],
                }
                for member in verified["members"]
            ],
        }


def get_experiment_v2(
    database: "Database",
    experiment_id: int,
) -> dict[str, Any] | None:
    with database.cursor() as cursor:
        row = cursor.execute(
            "SELECT approval_id FROM experiments WHERE id = ?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            return None
        approval_id = int(row["approval_id"])
    return get_experiment_v2_by_approval(database, approval_id)


def list_experiments_v2_for_project(
    database: "Database",
    project_id: str,
) -> list[dict[str, Any]]:
    with database.cursor() as cursor:
        rows = cursor.execute(
            "SELECT id, approval_id FROM experiments WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        approval_ids = [int(row["approval_id"]) for row in rows]
    results = []
    for approval_id in approval_ids:
        detail = get_experiment_v2_by_approval(database, approval_id)
        if detail is not None:
            results.append(detail)
    return results


__all__ = [
    "apply_experiment_v2_decision_in_transaction",
    "create_experiment_v2_request_in_transaction",
    "get_experiment_v2",
    "get_experiment_v2_by_approval",
    "list_experiments_v2_for_project",
    "reject_experiment_v2_decision_in_transaction",
    "verify_experiment_v2_approval_in_cursor",
]
