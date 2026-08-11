from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.execution_contract import canonical_json, utf8_sha256
from app.execution_plan_v2 import (
    COMMAND_BRIDGE_VERSION,
    ExecutionPlanV2ApprovalPayload,
    ExecutionPlanV2Request,
    SubmitObservationProvenance,
    build_bash_argv_bridge,
    build_execution_plan_v2_spec,
    project_instance_checkout_digest,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _spec_body() -> dict:
    project_id = _uuid()
    resource_requirements = {
        "required_tags": ["gpu"],
        "min_gpu_count": 1,
        "min_gpu_memory_mb": 1024,
        "min_available_ram_mb": 2048,
        "min_available_disk_mb": 4096,
        "exclusive_worker": True,
    }
    argv_sha = utf8_sha256(canonical_json(["python", "train.py", "10"]))
    return {
        "contract_version": "execution-plan-v2",
        "project_id": project_id,
        "project_version": {
            "project_version_id": _uuid(),
            "git_commit": "a" * 40,
            "bundle_sha256": "b" * 64,
        },
        "project_defaults": None,
        "run_profile": {"run_profile_id": _uuid(), "spec_digest": "c" * 64},
        "environment": {
            "environment_revision_id": _uuid(),
            "revision_digest": "d" * 64,
        },
        "parameter_values": {"epochs": 10},
        "dataset_none": True,
        "dataset_bindings": [],
        "target": {
            "selection_kind": "server_config_revision",
            "dispatch_policy_id": None,
            "dispatch_policy_digest": None,
            "server_config_revision_id": _uuid(),
            "target_identity_sha256": "e" * 64,
            "server_name": "worker-1",
        },
        "backend": "ssh",
        "project_instance": {
            "project_instance_id": "instance-1",
            "checkout_evidence_digest": "f" * 64,
        },
        "resource_requirements": resource_requirements,
        "resource_requirements_digest": utf8_sha256(
            canonical_json(resource_requirements)
        ),
        "observation_max_age_seconds": 60,
        "canonical_argv_sha256": argv_sha,
        "output_declarations_digest": "1" * 64,
        "command_bridge_version": COMMAND_BRIDGE_VERSION,
        "compiled_command_sha256": argv_sha,
        "job_command_sha256": "2" * 64,
    }


def test_execution_plan_v2_digest_is_canonical_and_target_bound() -> None:
    body = _spec_body()
    first = build_execution_plan_v2_spec(**body)
    assert first.plan_digest == utf8_sha256(
        canonical_json(first.model_dump(mode="json", exclude={"plan_digest"}))
    )

    body["target"] = {
        **body["target"],
        "server_config_revision_id": _uuid(),
    }
    second = build_execution_plan_v2_spec(**body)
    assert second.plan_digest != first.plan_digest


def test_submit_observation_is_outside_plan_digest() -> None:
    spec = build_execution_plan_v2_spec(**_spec_body())
    first = SubmitObservationProvenance(
        observation_id=1,
        server_name="worker-1",
        observed_at="2026-08-10T00:00:00.000000Z",
        online=True,
        probe_ok=True,
        gpu_count=1,
        gpu_mem_used_mb=0,
        gpu_mem_total_mb=8192,
        mem_available_bytes=16 * 1024 * 1024,
        disk_avail_bytes=32 * 1024 * 1024,
    )
    second = first.model_copy(update={"observation_id": 2})
    assert first != second
    encoded_spec = canonical_json(spec.model_dump(mode="json"))
    assert "observation_id" not in encoded_spec
    assert "observed_at" not in encoded_spec


def test_execution_plan_v2_request_is_strict_and_sorts_bindings() -> None:
    request = ExecutionPlanV2Request.model_validate(
        {
            "project_version_id": _uuid(),
            "template_selection": {
                "kind": "run_profile_revision",
                "run_profile_id": _uuid(),
            },
            "parameter_overrides": {"z": 1, "a": 2},
            "dataset_selection": {
                "kind": "bindings",
                "bindings": [
                    {
                        "name": "zeta",
                        "asset_id": _uuid(),
                        "selection": {"kind": "snapshot", "snapshot_id": "snap-z"},
                    },
                    {
                        "name": "alpha",
                        "asset_id": _uuid(),
                        "selection": {"kind": "alias", "alias_name": "latest"},
                    },
                ],
            },
            "target_selection": {
                "kind": "server_config_revision",
                "server_config_revision_id": _uuid(),
            },
        }
    )
    assert list(request.parameter_overrides) == ["a", "z"]
    assert [item.name for item in request.dataset_selection.bindings] == [
        "alpha",
        "zeta",
    ]

    payload = request.model_dump(mode="json") | {"unexpected": True}
    with pytest.raises(ValidationError):
        ExecutionPlanV2Request.model_validate(payload)


def test_approval_payload_has_only_the_four_fixed_fields() -> None:
    payload = {
        "contract_version": "execution-plan-v2-approval-v1",
        "execution_plan_id": _uuid(),
        "project_id": _uuid(),
        "plan_digest": "a" * 64,
    }
    assert ExecutionPlanV2ApprovalPayload.model_validate(payload).model_dump() == payload
    with pytest.raises(ValidationError):
        ExecutionPlanV2ApprovalPayload.model_validate(payload | {"command": "no"})


def test_bash_bridge_quotes_generated_values_and_preserves_setup_bytes() -> None:
    setup = "export MODE=pilot\nprintf '%s\\n' setup"
    command = build_bash_argv_bridge(
        checkout_path="/srv/project with space",
        git_commit="a" * 40,
        setup_command=setup,
        argv=("python", "train.py", "a b", "$(touch nope)"),
    )
    assert command.startswith("set -euo pipefail\n")
    assert "cd -- '/srv/project with space'" in command
    assert f'git rev-parse HEAD)" = {"a" * 40}' in command
    assert setup in command
    assert "exec python train.py 'a b' '$(touch nope)'" in command


def test_checkout_evidence_binds_path_without_disclosing_it() -> None:
    values = {
        "project_instance_id": "instance-1",
        "project_id": _uuid(),
        "server_name": "worker-1",
        "git_commit": "a" * 40,
    }
    first = project_instance_checkout_digest(path="/srv/a", **values)
    second = project_instance_checkout_digest(path="/srv/b", **values)
    assert first != second
    assert "/srv/" not in first
