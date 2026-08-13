from __future__ import annotations

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.db import TRANSACTION_ONLY_APPROVAL_KINDS, Database
from app.approvals import (
    InvalidDispatchPolicyRequestError,
    approve,
    request_auto_placement_approval,
    request_dispatch_policy_create_approval,
)
from app.execution_contract import canonical_json
from app.execution_plan import PlanInputs, derive_plan_draft
from app.identity import (
    ActorType,
    ProjectRoleV2,
    RequestContext,
    generate_service_token,
    generate_session_token,
)
from app.project_bootstrap import (
    EnvironmentRevisionInput,
    ProjectDefaultsInput,
    RunParameterSpec,
    RunTemplateContract,
    RunTemplateSpecInput,
)
from app.run_templates import (
    build_run_template_revision,
    compile_structured_argv,
)
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


OWNER_ID = "20000000-0000-0000-0000-000000000061"
REVIEWER_ID = "20000000-0000-0000-0000-000000000062"
OPERATOR_ID = "20000000-0000-0000-0000-000000000063"
SERVICE_ID = "20000000-0000-0000-0000-000000000064"


def _insert_binding(
    database: Database,
    *,
    project_id: str,
    actor_id: str,
    role: ProjectRoleV2,
) -> None:
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO project_role_bindings (
                id, project_id, actor_id, role, grant_provenance, granted_at
            ) VALUES (?, ?, ?, ?, 'legacy_membership', ?)
            """,
            (
                str(uuid.uuid4()),
                project_id,
                actor_id,
                role.value,
                "2026-08-01T00:00:00+00:00",
            ),
        )


def _seed_project(database: Database) -> str:
    project_id = database.insert_project(
        f"template-{uuid.uuid4()}",
        "/srv/projects/template",
    )
    for actor_id, display_name, actor_type in (
        (OWNER_ID, "Template owner", ActorType.HUMAN),
        (REVIEWER_ID, "Template reviewer", ActorType.HUMAN),
        (OPERATOR_ID, "Template operator", ActorType.HUMAN),
        (SERVICE_ID, "Template service operator", ActorType.SERVICE),
    ):
        database.insert_actor(
            actor_id=actor_id,
            actor_type=actor_type,
            display_name=display_name,
        )
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=OWNER_ID,
        role=ProjectRoleV2.OWNER,
    )
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=REVIEWER_ID,
        role=ProjectRoleV2.REVIEWER,
    )
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=OPERATOR_ID,
        role=ProjectRoleV2.OPERATOR,
    )
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=SERVICE_ID,
        role=ProjectRoleV2.OPERATOR,
    )
    return project_id


def _environment_input(*, name: str = "host-117") -> EnvironmentRevisionInput:
    return EnvironmentRevisionInput.model_validate(
        {
            "name": name,
            "setup_command": "python -m venv .venv",
            "required_server_tags": ["gpu"],
            "working_directory_policy": "project_checkout",
            "non_secret_env": [],
            "secret_references": [],
            "preflight_checks": [
                {"kind": "server_tag_present", "name": "gpu"}
            ],
        }
    )


def _create_environment(database: Database, project_id: str) -> dict:
    with SQLiteUnitOfWork(database) as unit_of_work:
        approval_id = unit_of_work.run(
            lambda cursor: database.create_environment_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                operation="create",
                expected_revision=0,
                expected_head_revision_id=None,
                environment_id=None,
                environment=_environment_input(),
                requester_actor_id=OPERATOR_ID,
            )
        )
    return database.apply_environment_change_decision(
        approval_id=approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )


def _update_environment(
    database: Database,
    *,
    project_id: str,
    environment_id: str,
    head_revision_id: str,
    expected_revision: int,
) -> dict:
    with SQLiteUnitOfWork(database) as unit_of_work:
        approval_id = unit_of_work.run(
            lambda cursor: database.create_environment_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                operation="update",
                expected_revision=expected_revision,
                expected_head_revision_id=head_revision_id,
                environment_id=environment_id,
                environment=_environment_input(),
                requester_actor_id=OPERATOR_ID,
            )
        )
    return database.apply_environment_change_decision(
        approval_id=approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )


def _request_template(
    database: Database,
    *,
    project_id: str,
    environment_revision_id: str | None,
    operation: str = "create",
    expected_revision: int = 0,
    expected_head_run_profile_id: str | None = None,
    expected_head_classification: str | None = None,
    expected_head_spec_digest: str | None = None,
    template: RunTemplateSpecInput | None = None,
) -> int:
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_run_template_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                operation=operation,
                expected_revision=expected_revision,
                expected_head_run_profile_id=expected_head_run_profile_id,
                expected_head_classification=expected_head_classification,
                expected_head_spec_digest=expected_head_spec_digest,
                expected_environment_head_revision_id=environment_revision_id,
                template=template,
                requester_actor_id=OPERATOR_ID,
            )
        )


def _request_defaults(
    database: Database,
    *,
    project_id: str,
    template_result: dict,
    environment_revision_id: str,
    operation: str = "create",
    expected_revision: int = 0,
    expected_head_revision_id: str | None = None,
    expected_head_revision_digest: str | None = None,
    epochs: int = 10,
) -> int:
    with SQLiteUnitOfWork(database) as unit_of_work:
        return unit_of_work.run(
            lambda cursor: database.create_project_defaults_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                operation=operation,
                expected_revision=expected_revision,
                expected_head_revision_id=expected_head_revision_id,
                expected_head_revision_digest=expected_head_revision_digest,
                expected_template_head_run_profile_id=(
                    template_result["run_profile_id"]
                ),
                expected_template_spec_digest=template_result["spec_digest"],
                expected_environment_head_revision_id=environment_revision_id,
                defaults=ProjectDefaultsInput(
                    parameter_values={
                        "enabled": True,
                        "epochs": epochs,
                        "ratio": "0.25",
                        "label": "model",
                        "mode": "safe",
                    }
                ),
                requester_actor_id=OPERATOR_ID,
            )
        )


def _session_for(client, main_module, actor_id: str) -> None:
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor_id,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.clear()
    client.cookies.set(
        main_module.app_state.config.session_cookie_name,
        issued.raw_token,
    )


def _enable_run_templates(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True
    config.project_environments_v1_enabled = True
    config.run_template_v2_enabled = True
    config.authorization_mode = "enforce"


def _compiler_template() -> RunTemplateSpecInput:
    return RunTemplateSpecInput.model_validate(
        {
            "name": "train",
            "argv_template": [
                {"kind": "literal", "value": "python"},
                {"kind": "literal", "value": "train.py"},
                {"kind": "parameter", "name": "enabled"},
                {"kind": "parameter", "name": "epochs"},
                {"kind": "parameter", "name": "ratio"},
                {"kind": "parameter", "name": "label"},
                {"kind": "parameter", "name": "mode"},
                {"kind": "parameter", "name": "optional_note"},
            ],
            "parameter_schema": [
                {"name": "enabled", "type": "boolean", "required": True},
                {
                    "name": "epochs",
                    "type": "integer",
                    "required": True,
                    "minimum": 1,
                    "maximum": 100,
                },
                {
                    "name": "ratio",
                    "type": "number",
                    "required": True,
                    "minimum": 0,
                    "maximum": "1.5",
                },
                {
                    "name": "label",
                    "type": "string",
                    "required": True,
                    "min_length": 0,
                    "max_length": 128,
                },
                {
                    "name": "mode",
                    "type": "enum",
                    "required": True,
                    "enum_values": ["fast", "safe"],
                },
                {
                    "name": "optional_note",
                    "type": "string",
                    "required": False,
                    "min_length": 0,
                    "max_length": 128,
                },
            ],
            "resource_requirements": {
                "required_tags": ["gpu"],
                "min_gpu_count": 1,
                "min_gpu_memory_mb": 8192,
                "min_available_ram_mb": 1024,
                "min_available_disk_mb": 2048,
                "exclusive_worker": True,
            },
            "output_declarations": [
                {
                    "name": "result",
                    "kind": "file",
                    "path_pattern": "results/*.json",
                    "required": True,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "value",
    ["0.50", "1e-3", "+0.5", "00.5", "-0.0", "0.0", "NaN", "Infinity"],
)
def test_fractional_number_contract_accepts_only_canonical_decimal_strings(value):
    with pytest.raises(ValidationError):
        RunParameterSpec.model_validate(
            {
                "name": "ratio",
                "type": "number",
                "minimum": value,
                "maximum": "1.5",
            }
        )


def test_number_contract_uses_decimal_without_float_or_type_coercion():
    parameter = RunParameterSpec.model_validate(
        {
            "name": "ratio",
            "type": "number",
            "minimum": "0.1",
            "maximum": "0.3",
        }
    )
    assert parameter.validate_value("0.2") == "0.2"
    with pytest.raises(ValueError, match="outside range"):
        parameter.validate_value("0.4")
    with pytest.raises(ValueError, match="canonical decimal"):
        parameter.validate_value(0.2)
    with pytest.raises(ValidationError):
        RunParameterSpec.model_validate(
            {
                "name": "ratio",
                "type": "number",
                "minimum": 0.1,
                "maximum": "0.3",
            }
        )
    with pytest.raises(ValidationError):
        RunParameterSpec.model_validate(
            {
                "name": "count",
                "type": "integer",
                "minimum": "1",
                "maximum": 3,
            }
        )
    with pytest.raises(ValidationError):
        ProjectDefaultsInput.model_validate({"parameter_values": {"ratio": 0.2}})


def test_compiler_golden_is_exact_and_omits_missing_optional_parameter():
    compiled = compile_structured_argv(
        _compiler_template(),
        {
            "enabled": True,
            "epochs": 12,
            "ratio": "0.25",
            "label": "模型甲",
            "mode": "safe",
        },
    )
    assert compiled.argv == (
        "python",
        "train.py",
        "true",
        "12",
        "0.25",
        "模型甲",
        "safe",
    )
    assert compiled.canonical_argv_bytes == (
        '["python","train.py","true","12","0.25","模型甲","safe"]'.encode()
    )
    assert compiled.argv_sha256 == (
        "865e4455b4d2e38510cc7602e3f51a4626baa8235bdd7d718479845039ac9f89"
    )
    assert compiled.argv_sha256 == hashlib.sha256(
        compiled.canonical_argv_bytes
    ).hexdigest()


def test_printable_shell_sentinel_remains_one_inert_argv_element():
    compiled = compile_structured_argv(
        _compiler_template(),
        {
            "enabled": False,
            "epochs": 1,
            "ratio": 1,
            "label": "; rm -rf / $(touch nope)",
            "mode": "fast",
            "optional_note": "a b | c && d",
        },
    )
    assert compiled.argv[4:] == (
        "1",
        "; rm -rf / $(touch nope)",
        "fast",
        "a b | c && d",
    )
    assert len(compiled.argv) == 8


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {
                "enabled": True,
                "epochs": 2,
                "ratio": "0.5",
                "label": "ok",
                "mode": "safe",
                "unknown": "x",
            },
            "unknown parameter",
        ),
        (
            {"enabled": True, "epochs": 2, "ratio": "0.5", "label": "ok"},
            "required parameter",
        ),
        (
            {
                "enabled": "true",
                "epochs": 2,
                "ratio": "0.5",
                "label": "ok",
                "mode": "safe",
            },
            "boolean",
        ),
        (
            {
                "enabled": True,
                "epochs": 2,
                "ratio": "0.5",
                "label": "bad\u0085value",
                "mode": "safe",
            },
            "control character",
        ),
    ],
)
def test_compiler_rejects_unknown_missing_invalid_and_control_values(values, message):
    with pytest.raises(ValueError, match=message):
        compile_structured_argv(_compiler_template(), values)


def test_compiler_enforces_element_and_total_utf8_byte_limits():
    # The declared label is capped at 128 characters before the independent
    # compiler byte guard can apply, so use a dedicated 4096-character schema.
    byte_template = RunTemplateSpecInput.model_validate(
        {
            "name": "byte-limit",
            "argv_template": [
                {"kind": "literal", "value": "tool"},
                {"kind": "parameter", "name": "value"},
            ],
            "parameter_schema": [
                {
                    "name": "value",
                    "type": "string",
                    "required": True,
                    "min_length": 0,
                    "max_length": 4096,
                }
            ],
            "resource_requirements": {"exclusive_worker": True},
            "output_declarations": [],
        }
    )
    with pytest.raises(ValueError, match="element exceeds"):
        compile_structured_argv(byte_template, {"value": "界" * 1366})

    total_template = RunTemplateSpecInput.model_validate(
        {
            "name": "total-limit",
            "argv_template": [
                {"kind": "literal", "value": "tool"},
                *[
                    {"kind": "parameter", "name": "value"}
                    for _ in range(63)
                ],
            ],
            "parameter_schema": [
                {
                    "name": "value",
                    "type": "string",
                    "required": True,
                    "min_length": 0,
                    "max_length": 4096,
                }
            ],
            "resource_requirements": {"exclusive_worker": True},
            "output_declarations": [],
        }
    )
    with pytest.raises(ValueError, match="canonical argv exceeds"):
        compile_structured_argv(total_template, {"value": "x" * 4096})


def test_generalized_revision_one_keeps_existing_canonical_digest_bytes():
    template = _compiler_template()
    body = {
        **template.model_dump(mode="json"),
        "run_profile_id": "11111111-1111-4111-8111-111111111111",
        "revision": 1,
        "contract_version": "run-template-spec-v2",
        "environment_revision_id": "22222222-2222-4222-8222-222222222222",
    }
    expected_digest = hashlib.sha256(canonical_json(body).encode()).hexdigest()
    revision = build_run_template_revision(
        template,
        run_profile_id=body["run_profile_id"],
        environment_revision_id=body["environment_revision_id"],
        revision=1,
    )
    assert revision.spec_digest == expected_digest
    assert revision.model_dump(mode="json", exclude={"spec_digest"}) == body


def test_digest_matching_but_unsorted_typed_spec_is_not_canonical():
    template = _compiler_template().model_dump(mode="json")
    template["parameter_schema"] = list(reversed(template["parameter_schema"]))
    body = {
        **template,
        "run_profile_id": "11111111-1111-4111-8111-111111111111",
        "revision": 2,
        "contract_version": "run-template-spec-v2",
        "environment_revision_id": "22222222-2222-4222-8222-222222222222",
    }
    body["spec_digest"] = hashlib.sha256(
        canonical_json(body).encode()
    ).hexdigest()
    with pytest.raises(ValidationError, match="canonical order"):
        RunTemplateContract.model_validate(body)


def test_typed_template_and_defaults_lifecycle_is_immutable_and_exact(db):
    assert {
        "run_template_change_v2",
        "project_defaults_change_v2",
    }.issubset(TRANSACTION_ONLY_APPROVAL_KINDS)
    project_id = _seed_project(db)
    environment = _create_environment(db, project_id)

    template_approval_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    with db.cursor() as cursor:
        approval = cursor.execute(
            "SELECT * FROM approvals WHERE id = ?",
            (template_approval_id,),
        ).fetchone()
        assert approval["kind"] == "run_template_change_v2"
        assert approval["payload_contract_version"] == "run-template-change-v2"
        assert approval["status"] == "pending"
        assert cursor.execute(
            "SELECT COUNT(*) FROM run_profiles WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == 0

    template = db.apply_run_template_change_decision(
        approval_id=template_approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    assert template["revision"] == 1
    assert template["status"] == "approved"
    with db.cursor() as cursor:
        profile = cursor.execute(
            "SELECT * FROM run_profiles WHERE id = ?",
            (template["run_profile_id"],),
        ).fetchone()
        spec = cursor.execute(
            "SELECT * FROM run_profile_specs WHERE run_profile_id = ?",
            (template["run_profile_id"],),
        ).fetchone()
        assert profile["command"] is None
        assert profile["setup_cmd"] is None
        assert profile["require_tag"] is None
        assert spec["spec_digest"] == template["spec_digest"]
        assert spec["environment_revision_id"] == (
            environment["environment_revision_id"]
        )

    defaults_approval_id = _request_defaults(
        db,
        project_id=project_id,
        template_result=template,
        environment_revision_id=environment["environment_revision_id"],
    )
    defaults = db.apply_project_defaults_change_decision(
        approval_id=defaults_approval_id,
        decision_actor_id=OWNER_ID,
        decision_mechanism="session",
    )
    assert defaults["revision"] == 1
    with db.cursor() as cursor:
        head = cursor.execute(
            "SELECT * FROM project_default_revisions WHERE id = ?",
            (defaults["defaults_revision_id"],),
        ).fetchone()
        assert head["run_profile_id"] == template["run_profile_id"]
        assert head["run_profile_spec_digest"] == template["spec_digest"]
        assert head["environment_revision_id"] == (
            environment["environment_revision_id"]
        )
        assert cursor.execute(
            """
            SELECT COUNT(*) FROM audit_events
            WHERE action IN (
                'run_profile_spec_created',
                'project_defaults_revision_created'
            ) AND approval_id IN (?, ?)
            """,
            (template_approval_id, defaults_approval_id),
        ).fetchone()[0] == 2


def test_legacy_profile_requires_explicit_adoption_and_remains_unchanged(db):
    project_id = _seed_project(db)
    environment = _create_environment(db, project_id)
    with db.cursor() as cursor:
        project_name = cursor.execute(
            "SELECT name FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()[0]
    legacy = db.insert_run_profile_revision(
        project_id=project_id,
        project_name=project_name,
        name="train",
        status="approved",
        command="python train.py --epochs 10",
        setup_cmd="pip install -r requirements.txt",
        require_tag="gpu",
        supersedes_id=None,
        approval_id=None,
        created_by_actor_id=None,
    )
    assert not db.run_profile_has_typed_spec(legacy.id)
    assert not db.run_profile_lineage_has_typed_spec(project_id, legacy.name)

    approval_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        operation="adopt",
        expected_revision=1,
        expected_head_run_profile_id=legacy.id,
        expected_head_classification="legacy_raw_command",
        template=_compiler_template().model_copy(
            update={"name": legacy.name}
        ),
    )
    adopted = db.apply_run_template_change_decision(
        approval_id=approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    assert adopted["revision"] == 2
    assert db.run_profile_has_typed_spec(adopted["run_profile_id"])
    assert db.run_profile_lineage_has_typed_spec(project_id, legacy.name)
    predecessor_inputs = PlanInputs(
        project_name=project_name,
        command="python train.py --epochs 10",
        run_profile_id=legacy.id,
        dataset_none=True,
        require_reproducible=False,
    )
    predecessor_draft = derive_plan_draft(
        predecessor_inputs,
        db.resolve_execution_plan_inputs(predecessor_inputs),
    )
    assert "run_profile_requires_v2_compiler" in predecessor_draft.reason_codes
    assert predecessor_draft.plan_digest is None
    with db.cursor() as cursor:
        old = cursor.execute(
            "SELECT * FROM run_profiles WHERE id = ?",
            (legacy.id,),
        ).fetchone()
        new = cursor.execute(
            "SELECT * FROM run_profiles WHERE id = ?",
            (adopted["run_profile_id"],),
        ).fetchone()
        assert old["command"] == "python train.py --epochs 10"
        assert old["setup_cmd"] == "pip install -r requirements.txt"
        assert old["require_tag"] == "gpu"
        assert cursor.execute(
            "SELECT 1 FROM run_profile_specs WHERE run_profile_id = ?",
            (legacy.id,),
        ).fetchone() is None
        assert new["command"] is None
        assert new["supersedes_id"] == legacy.id


def test_adoption_fences_preexisting_and_new_legacy_dispatch_policies(
    db,
    audit_path,
):
    project_id = _seed_project(db)
    environment = _create_environment(db, project_id)
    project = next(item for item in db.list_projects() if item.id == project_id)
    context = RequestContext(
        actor=db.get_actor(OPERATOR_ID),
        authentication_method="session",
    )
    dispatch_config = SimpleNamespace(
        dispatch_policy_v1_enabled=True,
        auto_placement_cooldown_sec=3600,
    )
    app_state = SimpleNamespace(config=dispatch_config)
    legacy = db.insert_run_profile_revision(
        project_id=project_id,
        project_name=project.name,
        name="legacy-policy-profile",
        status="approved",
        command="python train.py",
        setup_cmd=None,
        require_tag="gpu",
        supersedes_id=None,
        approval_id=None,
        created_by_actor_id=None,
    )
    active_request = request_dispatch_policy_create_approval(
        db,
        project.name,
        "active-before-adoption",
        allowed_servers=["worker-a"],
        run_profile_id=legacy.id,
        config=dispatch_config,
        audit_path=audit_path,
        request_context=context,
    )
    active_policy = asyncio.run(
        approve(
            db,
            active_request.id,
            app_state=app_state,
            audit_path=audit_path,
            request_context=context,
        )
    )["dispatch_policy"]
    pending_request = request_dispatch_policy_create_approval(
        db,
        project.name,
        "pending-before-adoption",
        allowed_servers=["worker-a"],
        run_profile_id=legacy.id,
        config=dispatch_config,
        audit_path=audit_path,
        request_context=context,
    )

    adoption_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        operation="adopt",
        expected_revision=1,
        expected_head_run_profile_id=legacy.id,
        expected_head_classification="legacy_raw_command",
        template=_compiler_template().model_copy(
            update={"name": legacy.name}
        ),
    )
    db.apply_run_template_change_decision(
        approval_id=adoption_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )

    pending_result = asyncio.run(
        approve(
            db,
            pending_request.id,
            app_state=app_state,
            audit_path=audit_path,
            request_context=context,
        )
    )
    assert pending_result["approval"].status == "rejected"
    assert "Product v2 compiler" in pending_result["approval"].note
    assert request_auto_placement_approval(
        db,
        policy=active_policy,
        server_name="worker-a",
        config=dispatch_config,
        audit_path=audit_path,
    ) is None
    with pytest.raises(InvalidDispatchPolicyRequestError, match="Product v2 compiler"):
        request_dispatch_policy_create_approval(
            db,
            project.name,
            "new-after-adoption",
            allowed_servers=["worker-a"],
            run_profile_id=legacy.id,
            config=dispatch_config,
            audit_path=audit_path,
            request_context=context,
        )


def test_environment_drift_blocks_template_approval_but_not_typed_archive(db):
    project_id = _seed_project(db)
    environment = _create_environment(db, project_id)
    stale_approval_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    newer_environment = _update_environment(
        db,
        project_id=project_id,
        environment_id=environment["environment_id"],
        head_revision_id=environment["environment_revision_id"],
        expected_revision=1,
    )
    with pytest.raises(ValueError, match="environment head is stale"):
        db.apply_run_template_change_decision(
            approval_id=stale_approval_id,
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="session",
        )
    with db.cursor() as cursor:
        assert cursor.execute(
            "SELECT status FROM approvals WHERE id = ?",
            (stale_approval_id,),
        ).fetchone()[0] == "pending"
        assert cursor.execute(
            "SELECT COUNT(*) FROM run_profiles WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == 0

    create_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=newer_environment["environment_revision_id"],
        template=_compiler_template(),
    )
    template = db.apply_run_template_change_decision(
        approval_id=create_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    newest_environment = _update_environment(
        db,
        project_id=project_id,
        environment_id=environment["environment_id"],
        head_revision_id=newer_environment["environment_revision_id"],
        expected_revision=2,
    )
    assert newest_environment["revision"] == 3
    archive_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=None,
        operation="archive",
        expected_revision=template["revision"],
        expected_head_run_profile_id=template["run_profile_id"],
        expected_head_classification="typed_spec",
        expected_head_spec_digest=template["spec_digest"],
        template=None,
    )
    archived = db.apply_run_template_change_decision(
        approval_id=archive_id,
        decision_actor_id=OWNER_ID,
        decision_mechanism="session",
    )
    assert archived["status"] == "archived"
    assert archived["revision"] == 2


def test_template_request_requires_environment_tags_and_never_falls_back_to_legacy(
    db,
):
    project_id = _seed_project(db)
    environment = _create_environment(db, project_id)
    missing_tags = _compiler_template().model_copy(deep=True)
    missing_tags.resource_requirements.required_tags = []
    with pytest.raises(ValueError, match="omit environment tags"):
        _request_template(
            db,
            project_id=project_id,
            environment_revision_id=environment["environment_revision_id"],
            template=missing_tags,
        )

    create_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    typed = db.apply_run_template_change_decision(
        approval_id=create_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    with db.cursor() as cursor:
        project_name = cursor.execute(
            "SELECT name FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()[0]
    corrupt_head = db.insert_run_profile_revision(
        project_id=project_id,
        project_name=project_name,
        name="train",
        status="approved",
        command="legacy raw command",
        setup_cmd=None,
        require_tag=None,
        supersedes_id=typed["run_profile_id"],
        approval_id=None,
        created_by_actor_id=None,
    )
    heads = db.list_project_run_template_heads_page(
        project_id=project_id,
        after=None,
        limit_plus_one=10,
    )
    assert heads[0]["run_profile_id"] == corrupt_head.id
    assert heads[0]["classification"] == "contract_invalid"
    assert heads[0]["head_spec"] is None
    with pytest.raises(ValueError, match="classification is stale"):
        _request_template(
            db,
            project_id=project_id,
            environment_revision_id=environment["environment_revision_id"],
            operation="adopt",
            expected_revision=2,
            expected_head_run_profile_id=corrupt_head.id,
            expected_head_classification="legacy_raw_command",
            template=_compiler_template(),
        )


def test_template_materialization_rolls_back_when_durable_audit_fails(
    db,
    monkeypatch,
):
    project_id = _seed_project(db)
    environment = _create_environment(db, project_id)
    approval_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    original = db.append_durable_audit_event_in_transaction

    def fail_target_audit(cursor, **kwargs):
        if kwargs.get("action") == "run_profile_spec_created":
            raise RuntimeError("injected audit failure")
        return original(cursor, **kwargs)

    monkeypatch.setattr(
        db,
        "append_durable_audit_event_in_transaction",
        fail_target_audit,
    )
    with pytest.raises(RuntimeError, match="injected audit failure"):
        db.apply_run_template_change_decision(
            approval_id=approval_id,
            decision_actor_id=REVIEWER_ID,
            decision_mechanism="session",
        )
    with db.cursor() as cursor:
        assert cursor.execute(
            "SELECT status FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()[0] == "pending"
        assert cursor.execute(
            "SELECT COUNT(*) FROM run_profiles WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == 0
        assert cursor.execute(
            "SELECT COUNT(*) FROM run_profile_specs WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == 0


def test_defaults_update_history_marks_superseded_exact_references_stale(db):
    project_id = _seed_project(db)
    environment = _create_environment(db, project_id)
    template_create_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    template_v1 = db.apply_run_template_change_decision(
        approval_id=template_create_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    defaults_create_id = _request_defaults(
        db,
        project_id=project_id,
        template_result=template_v1,
        environment_revision_id=environment["environment_revision_id"],
    )
    defaults_v1 = db.apply_project_defaults_change_decision(
        approval_id=defaults_create_id,
        decision_actor_id=OWNER_ID,
        decision_mechanism="session",
    )

    changed = _compiler_template().model_copy(deep=True)
    changed.resource_requirements.min_available_disk_mb = 4096
    template_update_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        operation="update",
        expected_revision=1,
        expected_head_run_profile_id=template_v1["run_profile_id"],
        expected_head_classification="typed_spec",
        expected_head_spec_digest=template_v1["spec_digest"],
        template=changed,
    )
    template_v2 = db.apply_run_template_change_decision(
        approval_id=template_update_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    stale_history = db.list_project_defaults_history_page(
        project_id=project_id,
        after=None,
        limit_plus_one=10,
    )
    assert stale_history[0]["stale"] is True
    assert stale_history[0]["stale_reasons"] == ["template_head_changed"]

    defaults_update_id = _request_defaults(
        db,
        project_id=project_id,
        template_result=template_v2,
        environment_revision_id=environment["environment_revision_id"],
        operation="update",
        expected_revision=1,
        expected_head_revision_id=defaults_v1["defaults_revision_id"],
        expected_head_revision_digest=defaults_v1["revision_digest"],
        epochs=20,
    )
    defaults_v2 = db.apply_project_defaults_change_decision(
        approval_id=defaults_update_id,
        decision_actor_id=OWNER_ID,
        decision_mechanism="session",
    )
    assert defaults_v2["revision"] == 2
    history = db.list_project_defaults_history_page(
        project_id=project_id,
        after=None,
        limit_plus_one=10,
    )
    assert [item["revision"] for item in history] == [2, 1]
    assert history[0]["is_head"] is True
    assert history[0]["stale"] is False
    assert history[1]["is_head"] is False
    assert history[1]["stale"] is True


def test_defaults_materialization_rolls_back_when_durable_audit_fails(
    db,
    monkeypatch,
):
    project_id = _seed_project(db)
    environment = _create_environment(db, project_id)
    template_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    template = db.apply_run_template_change_decision(
        approval_id=template_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    approval_id = _request_defaults(
        db,
        project_id=project_id,
        template_result=template,
        environment_revision_id=environment["environment_revision_id"],
    )
    original = db.append_durable_audit_event_in_transaction

    def fail_target_audit(cursor, **kwargs):
        if kwargs.get("action") == "project_defaults_revision_created":
            raise RuntimeError("injected defaults audit failure")
        return original(cursor, **kwargs)

    monkeypatch.setattr(
        db,
        "append_durable_audit_event_in_transaction",
        fail_target_audit,
    )
    with pytest.raises(RuntimeError, match="injected defaults audit failure"):
        db.apply_project_defaults_change_decision(
            approval_id=approval_id,
            decision_actor_id=OWNER_ID,
            decision_mechanism="session",
        )
    with db.cursor() as cursor:
        assert cursor.execute(
            "SELECT status FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()[0] == "pending"
        assert cursor.execute(
            "SELECT COUNT(*) FROM project_default_revisions WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == 0


def test_defaults_approval_revalidates_exact_template_and_self_decision(db):
    project_id = _seed_project(db)
    environment = _create_environment(db, project_id)
    create_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    template = db.apply_run_template_change_decision(
        approval_id=create_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    defaults_id = _request_defaults(
        db,
        project_id=project_id,
        template_result=template,
        environment_revision_id=environment["environment_revision_id"],
    )
    with pytest.raises(ValueError, match="high_risk_self_decision"):
        db.apply_project_defaults_change_decision(
            approval_id=defaults_id,
            decision_actor_id=OPERATOR_ID,
            decision_mechanism="session",
        )

    updated_input = _compiler_template().model_copy(deep=True)
    updated_input.resource_requirements.min_available_ram_mb = 2048
    update_id = _request_template(
        db,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        operation="update",
        expected_revision=template["revision"],
        expected_head_run_profile_id=template["run_profile_id"],
        expected_head_classification="typed_spec",
        expected_head_spec_digest=template["spec_digest"],
        template=updated_input,
    )
    updated = db.apply_run_template_change_decision(
        approval_id=update_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )
    assert updated["revision"] == 2
    with pytest.raises(ValueError, match="template head is stale"):
        db.apply_project_defaults_change_decision(
            approval_id=defaults_id,
            decision_actor_id=OWNER_ID,
            decision_mechanism="session",
        )
    with db.cursor() as cursor:
        assert cursor.execute(
            "SELECT status FROM approvals WHERE id = ?",
            (defaults_id,),
        ).fetchone()[0] == "pending"
        assert cursor.execute(
            "SELECT COUNT(*) FROM project_default_revisions WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == 0


def test_api_template_defaults_review_and_idempotent_decisions(api_client):
    client, main_module = api_client
    _enable_run_templates(main_module)
    database = main_module.app_state.db
    project_id = _seed_project(database)
    environment = _create_environment(database, project_id)
    _session_for(client, main_module, OPERATOR_ID)

    request_body = {
        "operation": "create",
        "expected_revision": 0,
        "expected_head_run_profile_id": None,
        "expected_head_classification": None,
        "expected_head_spec_digest": None,
        "expected_environment_head_revision_id": (
            environment["environment_revision_id"]
        ),
        "template": _compiler_template().model_dump(mode="json"),
    }
    template_request = client.post(
        f"/api/v2/projects/{project_id}/run-template-change-requests",
        json=request_body,
        headers={"Idempotency-Key": "template-create-0001"},
    )
    assert template_request.status_code == 202, template_request.text
    replay = client.post(
        f"/api/v2/projects/{project_id}/run-template-change-requests",
        json=request_body,
        headers={"Idempotency-Key": "template-create-0001"},
    )
    assert replay.status_code == 202
    assert replay.json()["approval_id"] == template_request.json()["approval_id"]
    assert replay.json()["replayed"] is True
    approval_id = template_request.json()["approval_id"]

    _session_for(client, main_module, REVIEWER_ID)
    detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["kind"] == "run_template_change_v2"
    assert detail.json()["payload_verified"] is True
    assert detail.json()["payload"]["target_revision"]["spec_digest"]
    decision = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "reviewed"},
        headers={"Idempotency-Key": "template-decision-0001"},
    )
    assert decision.status_code == 202, decision.text
    assert decision.json()["status"] == "approved"
    template = decision.json()
    decision_replay = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        json={"decision": "approve", "note": "reviewed"},
        headers={"Idempotency-Key": "template-decision-0001"},
    )
    assert decision_replay.status_code == 202, decision_replay.text
    assert decision_replay.json()["replayed"] is True
    assert decision_replay.json()["spec_digest"] == template["spec_digest"]

    templates = client.get(
        f"/api/v2/projects/{project_id}/run-templates?limit=1"
    )
    assert templates.status_code == 200, templates.text
    assert templates.headers["Cache-Control"] == "no-store"
    item = templates.json()["items"][0]
    assert item["classification"] == "typed_spec"
    assert item["head_spec"]["spec_digest"] == template["spec_digest"]
    assert item["one_click_run_eligible"] is False
    assert "command" not in item

    _session_for(client, main_module, OPERATOR_ID)
    defaults_body = {
        "operation": "create",
        "expected_revision": 0,
        "expected_head_revision_id": None,
        "expected_head_revision_digest": None,
        "expected_template_head_run_profile_id": template["run_profile_id"],
        "expected_template_spec_digest": template["spec_digest"],
        "expected_environment_head_revision_id": (
            environment["environment_revision_id"]
        ),
        "defaults": {
            "parameter_values": {
                "enabled": True,
                "epochs": 10,
                "ratio": "0.25",
                "label": "model",
                "mode": "safe",
            }
        },
    }
    defaults_request = client.post(
        f"/api/v2/projects/{project_id}/default-change-requests",
        json=defaults_body,
        headers={"Idempotency-Key": "defaults-create-0001"},
    )
    assert defaults_request.status_code == 202, defaults_request.text
    defaults_approval_id = defaults_request.json()["approval_id"]
    _session_for(client, main_module, OWNER_ID)
    defaults_decision = client.post(
        f"/api/v2/approvals/{defaults_approval_id}/decisions",
        json={"decision": "approve", "note": "reviewed"},
        headers={"Idempotency-Key": "defaults-decision-0001"},
    )
    assert defaults_decision.status_code == 202, defaults_decision.text
    assert defaults_decision.json()["revision"] == 1
    history = client.get(f"/api/v2/projects/{project_id}/defaults?limit=1")
    assert history.status_code == 200, history.text
    defaults_item = history.json()["items"][0]
    assert defaults_item["is_head"] is True
    assert defaults_item["stale"] is False
    assert defaults_item["contract"]["parameter_values"]["epochs"] == 10


def test_api_flag_off_hides_routes_approvals_and_project_workspace(api_client):
    client, main_module = api_client
    _enable_run_templates(main_module)
    database = main_module.app_state.db
    project_id = _seed_project(database)
    environment = _create_environment(database, project_id)
    approval_id = _request_template(
        database,
        project_id=project_id,
        environment_revision_id=environment["environment_revision_id"],
        template=_compiler_template(),
    )
    _session_for(client, main_module, REVIEWER_ID)
    main_module.app_state.config.run_template_v2_enabled = False
    main_module.app_state.config.project_bootstrap_v2_enabled = True

    assert client.get(
        f"/api/v2/projects/{project_id}/run-templates"
    ).status_code == 404
    assert client.get(
        f"/api/v2/projects/{project_id}/defaults"
    ).status_code == 404
    assert client.get(f"/api/v2/approvals/{approval_id}").status_code == 404
    listed = client.get("/api/v2/approvals?status=pending")
    assert listed.status_code == 200
    assert approval_id not in {item["id"] for item in listed.json()["items"]}
    legacy_listed = client.get("/approvals?status=pending")
    assert legacy_listed.status_code == 200
    assert approval_id not in {item["id"] for item in legacy_listed.json()}
    assert client.get(
        "/approvals?status=pending&kind=run_template_change_v2"
    ).json() == []
    for path in (f"/approve/{approval_id}", f"/reject/{approval_id}"):
        hidden = client.post(path)
        assert hidden.status_code == 404
        assert hidden.json() == {"detail": "approval 不存在"}
    assert database.get_approval(approval_id).status == "pending"

    main_module.app_state.config.run_template_v2_enabled = True
    for path in (f"/approve/{approval_id}", f"/reject/{approval_id}"):
        v2_only = client.post(path)
        assert v2_only.status_code == 409
        assert v2_only.json()["error"]["code"] == "v2_decision_required"
    main_module.app_state.config.run_template_v2_enabled = False
    assert database.get_approval(approval_id).status == "pending"
    workspace = client.get(f"/api/v2/projects/{project_id}/workspace")
    assert workspace.status_code == 200
    assert workspace.json()["run_template"] is None
    assert workspace.json()["defaults"] is None
    assert workspace.json()["run_template_capability"]["state"] == "disabled"


def test_scoped_service_operator_can_request_but_cannot_decide_template(api_client):
    client, main_module = api_client
    _enable_run_templates(main_module)
    database = main_module.app_state.db
    project_id = _seed_project(database)
    environment = _create_environment(database, project_id)
    database.insert_service_account(
        actor_id=SERVICE_ID,
        name=f"template-service-{uuid.uuid4()}",
    )
    issued = generate_service_token()
    database.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=SERVICE_ID,
        secret_hash=issued.secret_hash,
        scopes=["project.operate"],
        expires_at="2099-01-01T00:00:00+00:00",
    )
    main_module.app_state.config.service_token_auth_enabled = True
    client.cookies.clear()
    headers = {
        "Authorization": f"Bearer {issued.raw_token}",
        "Idempotency-Key": "service-template-create-0001",
    }
    body = {
        "operation": "create",
        "expected_revision": 0,
        "expected_head_run_profile_id": None,
        "expected_head_classification": None,
        "expected_head_spec_digest": None,
        "expected_environment_head_revision_id": (
            environment["environment_revision_id"]
        ),
        "template": _compiler_template().model_dump(mode="json"),
    }
    requested = client.post(
        f"/api/v2/projects/{project_id}/run-template-change-requests",
        json=body,
        headers=headers,
    )
    assert requested.status_code == 202, requested.text
    decision = client.post(
        f"/api/v2/approvals/{requested.json()['approval_id']}/decisions",
        json={"decision": "approve"},
        headers={
            "Authorization": f"Bearer {issued.raw_token}",
            "Idempotency-Key": "service-template-decision-0001",
        },
    )
    assert decision.status_code == 403


def test_api_validation_is_value_safe_and_legacy_list_never_leaks_raw_command(
    api_client,
):
    client, main_module = api_client
    _enable_run_templates(main_module)
    database = main_module.app_state.db
    project_id = _seed_project(database)
    environment = _create_environment(database, project_id)
    with database.cursor() as cursor:
        project_name = cursor.execute(
            "SELECT name FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()[0]
    sentinel = "SECRET_SENTINEL_SHOULD_NEVER_REFLECT"
    legacy = database.insert_run_profile_revision(
        project_id=project_id,
        project_name=project_name,
        name="legacy-train",
        status="approved",
        command=f"python train.py --token {sentinel}",
        setup_cmd=f"export TOKEN={sentinel}",
        require_tag=sentinel,
        supersedes_id=None,
        approval_id=None,
        created_by_actor_id=None,
    )
    _session_for(client, main_module, OPERATOR_ID)
    listed = client.get(f"/api/v2/projects/{project_id}/run-templates")
    assert listed.status_code == 200, listed.text
    serialized = listed.text
    assert sentinel not in serialized
    item = next(
        value
        for value in listed.json()["items"]
        if value["run_profile_id"] == legacy.id
    )
    assert item["classification"] == "legacy_raw_command"
    assert item["head_spec"] is None
    assert item["one_click_run_eligible"] is False

    invalid = {
        "operation": "create",
        "expected_revision": 0,
        "expected_environment_head_revision_id": (
            environment["environment_revision_id"]
        ),
        "template": {
            **_compiler_template().model_dump(mode="json"),
            "unexpected_secret": sentinel,
        },
    }
    rejected = client.post(
        f"/api/v2/projects/{project_id}/run-template-change-requests",
        json=invalid,
        headers={"Idempotency-Key": "template-invalid-0001"},
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.headers["Cache-Control"] == "no-store"
    assert sentinel not in rejected.text
    assert rejected.json()["error"]["code"] == "invalid_run_template_request"
    with database.cursor() as cursor:
        assert cursor.execute(
            "SELECT COUNT(*) FROM api_idempotency_keys"
        ).fetchone()[0] == 0
