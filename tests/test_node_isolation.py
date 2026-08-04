"""Local contract tests for the opt-in PR-10 workload isolation posture."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.isolation import (
    ISOLATION_CONTRACT_VERSION,
    ResourcePolicy,
    build_transient_attempt_argv,
    isolation_manifest,
    safe_unit_name,
    workload_environment,
)


def test_strict_workload_environment_is_an_allowlist_and_never_leaks_node_secrets():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/train",
        "CUDA_VISIBLE_DEVICES": "0",
        "CUSTOM_SECRET": "must-not-pass",
        "DISPATCH_NODE_TOKEN": "node-secret",
        "DISPATCH_NODE_ACTIVATION_NONCE": "activation-secret",
    }

    strict = workload_environment(source, mode="allowlist")

    assert strict == {
        "PATH": "/usr/bin",
        "HOME": "/home/train",
        "CUDA_VISIBLE_DEVICES": "0",
    }
    assert "DISPATCH_NODE_TOKEN" not in strict
    assert "DISPATCH_NODE_ACTIVATION_NONCE" not in strict


def test_compatibility_environment_only_removes_credentials():
    source = {"PATH": "/usr/bin", "CUSTOM_RUNTIME": "kept", "DISPATCH_NODE_TOKEN": "x"}
    assert workload_environment(source) == {
        "PATH": "/usr/bin",
        "CUSTOM_RUNTIME": "kept",
    }


def test_resource_policy_is_bounded_and_translates_to_systemd_properties():
    properties = ResourcePolicy(
        memory_max_bytes=1024 * 1024,
        cpu_quota_percent=200,
        tasks_max=32,
    ).systemd_properties()
    assert "NoNewPrivileges=yes" in properties
    assert "PrivateUsers=yes" in properties
    assert "ProtectProc=invisible" in properties
    assert "KillMode=control-group" in properties
    assert "MemoryMax=1048576" in properties
    assert "CPUQuota=200%" in properties
    assert "TasksMax=32" in properties

    with pytest.raises(ValueError):
        ResourcePolicy(memory_max_bytes=0)
    with pytest.raises(ValueError):
        ResourcePolicy(cpu_quota_percent=0)


def test_transient_attempt_unit_is_detached_and_protects_control_evidence(tmp_path):
    workdir = tmp_path / "attempt"
    control = tmp_path / "control"
    argv = build_transient_attempt_argv(
        attempt_id="attempt-1",
        workdir=workdir,
        control_evidence_dir=control,
        command_sha256="a" * 64,
        resource_policy=ResourcePolicy(memory_max_bytes=4096),
    )
    rendered = " ".join(argv)
    assert argv[:4] == ["systemd-run", "--user", "--no-block", "--collect"]
    assert "--unit=dispatch-attempt-attempt-1" in argv
    assert f"--property=ReadOnlyPaths={control}" in argv
    assert f"--property=ReadWritePaths={workdir}" in argv
    assert "--property=KillMode=control-group" in argv
    assert "--property=MemoryMax=4096" in argv
    assert "cmd.sh" not in rendered
    assert "a" * 64 in rendered


def test_transient_unit_rejects_shared_or_relative_control_paths(tmp_path):
    with pytest.raises(ValueError, match="separate"):
        build_transient_attempt_argv(
            attempt_id="attempt-1",
            workdir=tmp_path / "attempt",
            control_evidence_dir=tmp_path / "attempt",
            command_sha256="a" * 64,
        )
    with pytest.raises(ValueError, match="absolute"):
        build_transient_attempt_argv(
            attempt_id="attempt-1",
            workdir="relative-attempt",
            control_evidence_dir=tmp_path / "control",
            command_sha256="a" * 64,
        )


def test_unit_name_and_manifest_are_safe_and_non_secret(tmp_path):
    assert safe_unit_name("abc-123") == "dispatch-attempt-abc-123"
    with pytest.raises(ValueError):
        safe_unit_name("../escape")

    manifest = isolation_manifest(
        attempt_id="abc-123",
        workdir=tmp_path / "attempt",
        control_evidence_dir=tmp_path / "control",
    )
    assert manifest["contract_version"] == ISOLATION_CONTRACT_VERSION
    assert manifest["unit_name"] == "dispatch-attempt-abc-123"
    assert "DISPATCH_NODE_TOKEN" not in repr(manifest)


def test_supervisor_strict_mode_does_not_inherit_arbitrary_environment(tmp_path):
    from agent.client import command_digest
    from agent.runner import AttemptStore, build_supervisor_argv

    store = AttemptStore(tmp_path / "attempts")
    command = "if [ -n \"${CUSTOM_RUNTIME+x}\" ]; then exit 91; fi; exit 0"
    attempt = store.create(
        attempt_id="strict-env",
        job_id=1,
        command_sha256=command_digest(command),
    )
    store.record_ack(attempt)
    store.write_command(attempt.attempt_id, command)
    env = {
        **os.environ,
        "DISPATCH_WORKLOAD_ENV_MODE": "allowlist",
        "CUSTOM_RUNTIME": "must-not-inherit",
        "DISPATCH_NODE_TOKEN": "must-not-inherit",
    }
    import subprocess

    process = subprocess.run(
        build_supervisor_argv(
            attempt.attempt_id,
            store.attempt_dir(attempt.attempt_id),
            attempt.command_sha256,
        ),
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0
    assert store.attempt_dir(attempt.attempt_id).joinpath("terminal.json").exists()
