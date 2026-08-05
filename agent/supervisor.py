"""Durable per-attempt workload supervisor.

The Node Agent itself may restart while a workload keeps running.  A tiny
separate process therefore owns the workload, records its boot-scoped process
identity before launch, and atomically writes terminal evidence after wait().
No command bytes are accepted through argv; they are read from the already
fsynced ``cmd.sh`` and verified against the approved digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

from agent.runner import (
    SUPERVISOR_CONTRACT_VERSION,
    SUPERVISOR_METADATA_FILENAME,
    TERMINAL_EVIDENCE_FILENAME,
    _is_safe_identifier,
    build_launcher_argv,
    read_process_identity,
)
from agent.isolation import workload_environment


def _atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _normalized_exit_code(returncode: int) -> int:
    if returncode < 0:
        return min(255, 128 + abs(returncode))
    return min(255, returncode)


def run_supervisor(
    *, attempt_id: str, workdir: Path, command_sha256: str
) -> int:
    if not _is_safe_identifier(attempt_id):
        return 64
    try:
        workdir = workdir.resolve(strict=True)
    except OSError:
        return 66
    command_path = workdir / "cmd.sh"
    try:
        command_bytes = command_path.read_bytes()
    except OSError:
        return 66
    if hashlib.sha256(command_bytes).hexdigest() != command_sha256:
        return 65

    identity = read_process_identity(os.getpid())
    if identity is None:
        return 70
    evidence_base = {
        "contract_version": SUPERVISOR_CONTRACT_VERSION,
        "attempt_id": attempt_id,
        "command_sha256": command_sha256,
        "supervisor_pid": os.getpid(),
        "process_boot_id": identity[0],
        "process_start_time_ticks": identity[1],
    }
    child: Optional[subprocess.Popen] = None
    pending_signal: Optional[int] = None

    def _forward(received: int, _frame) -> None:
        nonlocal pending_signal
        workload_signal = signal.SIGKILL if received == signal.SIGUSR1 else signal.SIGTERM
        pending_signal = workload_signal
        if child is None:
            return
        try:
            os.killpg(child.pid, workload_signal)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGUSR1, _forward)
    signal.signal(signal.SIGINT, _forward)

    # Publish supervisor identity only after the handlers are armed.  The
    # metadata file is the readiness signal used by the agent when it sends a
    # stop request; writing it first leaves a small window where the default
    # signal action can kill this process before terminal evidence is written.
    # If a signal arrives while the metadata is being fsynced, ``_forward``
    # records it in ``pending_signal`` and the child launch below applies it.
    _atomic_json(workdir / SUPERVISOR_METADATA_FILENAME, evidence_base)

    try:
        # Node credentials authorize the control-plane channel, not the user
        # workload.  The compatibility default strips only those secrets;
        # the systemd template opts into the strict bounded allowlist.
        workload_env = workload_environment(
            os.environ,
            mode=os.environ.get("DISPATCH_WORKLOAD_ENV_MODE", "compat"),
        )
        child = subprocess.Popen(
            build_launcher_argv(attempt_id, workdir),
            cwd=str(workdir),
            shell=False,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            env=workload_env,
        )
        if pending_signal is not None:
            try:
                os.killpg(child.pid, pending_signal)
            except OSError:
                pass
        returncode = child.wait()
        exit_code = _normalized_exit_code(returncode)
    except Exception:  # noqa: BLE001 - terminal evidence must survive launch failure
        exit_code = 127

    _atomic_json(
        workdir / TERMINAL_EVIDENCE_FILENAME,
        {**evidence_base, "exit_code": exit_code},
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.supervisor")
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--command-sha256", required=True)
    args = parser.parse_args(argv)
    if (
        len(args.command_sha256) != 64
        or any(char not in "0123456789abcdef" for char in args.command_sha256)
    ):
        return 64
    return run_supervisor(
        attempt_id=args.attempt_id,
        workdir=Path(args.workdir),
        command_sha256=args.command_sha256,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
