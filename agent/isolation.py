"""Opt-in workload-isolation contract for the independently packaged agent.

The normal supervisor path remains the compatibility rollback path.  When a
worker has a user-level systemd instance available, this module builds a
transient *attempt* unit so the workload cgroup is independent from the agent
daemon.  The unit also mounts control evidence read-only and applies bounded
resource properties.  All builders are pure argv/property functions and are
therefore testable without systemd, root, a container runtime, or a worker
machine.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


ISOLATION_CONTRACT_VERSION = "node-workload-isolation-v1"
WORKLOAD_ENV_MODE_COMPAT = "compat"
WORKLOAD_ENV_MODE_ALLOWLIST = "allowlist"
WORKLOAD_ENV_MODES = frozenset(
    {WORKLOAD_ENV_MODE_COMPAT, WORKLOAD_ENV_MODE_ALLOWLIST}
)

# A workload receives only ordinary runtime hints in strict mode.  Node
# credentials and all DISPATCH control-plane values are deliberately absent.
WORKLOAD_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PYTHONPATH",
        "TMPDIR",
        "VIRTUAL_ENV",
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
    }
)
WORKLOAD_SECRET_ENV_NAMES = frozenset(
    {
        "DISPATCH_NODE_TOKEN",
        "DISPATCH_NODE_ACTIVATION_NONCE",
    }
)


@dataclass(frozen=True)
class ResourcePolicy:
    """Bounded systemd resource controls for one workload attempt."""

    memory_max_bytes: Optional[int] = None
    cpu_quota_percent: Optional[int] = None
    tasks_max: Optional[int] = None

    def __post_init__(self) -> None:
        if self.memory_max_bytes is not None and (
            isinstance(self.memory_max_bytes, bool)
            or not isinstance(self.memory_max_bytes, int)
            or self.memory_max_bytes < 1
        ):
            raise ValueError("memory_max_bytes must be a positive integer")
        if self.cpu_quota_percent is not None and (
            isinstance(self.cpu_quota_percent, bool)
            or not isinstance(self.cpu_quota_percent, int)
            or not 1 <= self.cpu_quota_percent <= 100_000
        ):
            raise ValueError("cpu_quota_percent must be between 1 and 100000")
        if self.tasks_max is not None and (
            isinstance(self.tasks_max, bool)
            or not isinstance(self.tasks_max, int)
            or self.tasks_max < 1
        ):
            raise ValueError("tasks_max must be a positive integer")

    def systemd_properties(self) -> tuple[str, ...]:
        properties = [
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            # A transient workload gets a private user namespace so a process
            # cannot signal or ptrace the host-UID agent daemon.
            "PrivateUsers=yes",
            "ProtectProc=invisible",
            "ProcSubset=pid",
            "RestrictSUIDSGID=yes",
            "RestrictRealtime=yes",
            "LockPersonality=yes",
            "KillMode=control-group",
        ]
        if self.memory_max_bytes is not None:
            properties.append(f"MemoryMax={self.memory_max_bytes}")
        if self.cpu_quota_percent is not None:
            properties.append(f"CPUQuota={self.cpu_quota_percent}%")
        if self.tasks_max is not None:
            properties.append(f"TasksMax={self.tasks_max}")
        return tuple(properties)


def workload_environment(
    environment: Mapping[str, str], *, mode: str = WORKLOAD_ENV_MODE_COMPAT
) -> dict[str, str]:
    """Return a credential-hygienic environment for an approved workload.

    ``compat`` preserves historical non-secret variables while removing Node
    credentials. ``allowlist`` is the deployable isolation posture and only
    passes the bounded runtime names above (plus locale variables).
    """

    if mode not in WORKLOAD_ENV_MODES:
        raise ValueError(f"unsupported workload environment mode: {mode!r}")
    if mode == WORKLOAD_ENV_MODE_COMPAT:
        return {
            str(name): str(value)
            for name, value in environment.items()
            if name not in WORKLOAD_SECRET_ENV_NAMES
        }
    return {
        str(name): str(value)
        for name, value in environment.items()
        if name in WORKLOAD_ENV_ALLOWLIST or name.startswith("LC_")
    }


def safe_unit_name(attempt_id: str) -> str:
    """Map an attempt identifier to a systemd-safe, bounded unit name."""

    if (
        not isinstance(attempt_id, str)
        or not attempt_id
        or len(attempt_id) > 128
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in attempt_id)
    ):
        raise ValueError("unsafe attempt id for transient unit")
    return f"dispatch-attempt-{attempt_id}"


def _absolute_directory(value: str | Path, label: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return os.path.normpath(str(path))


def build_transient_attempt_argv(
    *,
    attempt_id: str,
    workdir: str | Path,
    control_evidence_dir: str | Path,
    command_sha256: str,
    resource_policy: ResourcePolicy = ResourcePolicy(),
) -> list[str]:
    """Build a detached user-systemd command for one isolated attempt.

    ``control_evidence_dir`` must not be the workload directory: systemd
    mounts it read-only inside the unit, preventing a workload from replacing
    terminal evidence.  The command contains only identifiers, paths and the
    approved digest; command bytes remain in ``cmd.sh``.
    """

    from agent.runner import build_supervisor_argv

    unit_name = safe_unit_name(attempt_id)
    workdir_value = _absolute_directory(workdir, "workdir")
    evidence_value = _absolute_directory(control_evidence_dir, "control_evidence_dir")
    if evidence_value == workdir_value:
        raise ValueError("control evidence must be separate from workload directory")
    if (
        not isinstance(command_sha256, str)
        or len(command_sha256) != 64
        or any(character not in "0123456789abcdef" for character in command_sha256.lower())
    ):
        raise ValueError("command digest is not SHA-256")

    supervisor = build_supervisor_argv(attempt_id, workdir_value, command_sha256)
    argv = [
        "systemd-run",
        "--user",
        "--no-block",
        "--collect",
        f"--unit={unit_name}",
        f"--working-directory={workdir_value}",
        f"--property=ReadOnlyPaths={evidence_value}",
        f"--property=ReadWritePaths={workdir_value}",
    ]
    argv.extend(f"--property={value}" for value in resource_policy.systemd_properties())
    argv.extend(["--", *supervisor])
    return argv


def isolation_manifest(
    *,
    attempt_id: str,
    workdir: str | Path,
    control_evidence_dir: str | Path,
    resource_policy: ResourcePolicy = ResourcePolicy(),
) -> dict[str, object]:
    """Return safe, non-secret evidence describing the selected policy."""

    return {
        "contract_version": ISOLATION_CONTRACT_VERSION,
        "attempt_id": attempt_id,
        "unit_name": safe_unit_name(attempt_id),
        "workdir": _absolute_directory(workdir, "workdir"),
        "control_evidence_dir": _absolute_directory(
            control_evidence_dir, "control_evidence_dir"
        ),
        "resource_properties": list(resource_policy.systemd_properties()),
        "agent_executable": sys.executable,
    }
