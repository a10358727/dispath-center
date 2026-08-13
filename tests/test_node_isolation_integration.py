"""Real user-systemd smoke coverage for the strict Node launcher.

The test is intentionally skipped on workers without a usable user systemd
manager.  It never contacts the control plane and uses a throwaway attempt
directory under the checkout (``PrivateTmp`` deliberately hides host ``/tmp``
from the transient unit).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from functools import lru_cache
from pathlib import Path

import pytest

from agent.isolation import ResourcePolicy, SystemdTransientLauncher, safe_unit_name
from agent.runner import AttemptStore, launch


@lru_cache(maxsize=1)
def _user_systemd_available() -> bool:
    if shutil.which("systemd-run") is None:
        return False
    # A manager accepting the unit properties is not enough: some CI hosts
    # accept ``PrivateUsers`` while silently leaving bind mounts writable.
    # Probe the actual read-only control boundary and skip the real-systemd
    # tests when this host cannot enforce the contract.
    repository_root = Path(__file__).resolve().parents[1]
    try:
        with tempfile.TemporaryDirectory(
            prefix=".dispatch-systemd-capability-", dir=repository_root
        ) as temporary:
            root = Path(temporary)
            workdir = root / "workload"
            control = root / "control"
            workdir.mkdir()
            control.mkdir()
            probe_path = workdir / "probe.json"
            code = """import json
from pathlib import Path
control = Path({control!r})
visible = control.is_dir()
denied = False
try:
    control.joinpath('write-test').write_text('no')
except OSError:
    denied = True
Path({probe!r}).write_text(json.dumps({{'visible': visible, 'denied': denied}}))
""".format(control=str(control), probe=str(probe_path))
            result = subprocess.run(
                [
                    "systemd-run",
                    "--user",
                    "--wait",
                    "--collect",
                    "--pipe",
                    f"--working-directory={workdir}",
                    f"--property=BindReadOnlyPaths={control}",
                    f"--property=ReadWritePaths={workdir}",
                    "--property=PrivateTmp=yes",
                    "--property=ProtectSystem=strict",
                    "--property=PrivateUsers=yes",
                    "--property=ProtectProc=invisible",
                    "--property=ProcSubset=pid",
                    "--property=KillMode=control-group",
                    "--",
                    sys.executable,
                    "-c",
                    code,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0 or not probe_path.is_file():
                return False
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            return probe == {"visible": True, "denied": True}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False


def _wait_for_terminal(store: AttemptStore, attempt_id: str, timeout: float = 20) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = store.load(attempt_id)
        assert current is not None
        exit_code = store.read_terminal_evidence(current)
        if exit_code is not None:
            return exit_code
        time.sleep(0.2)
    raise AssertionError(f"systemd attempt {attempt_id} did not publish terminal evidence")


def _prepare_attempt(store: AttemptStore, attempt_id: str, command: str):
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
    attempt = store.create(attempt_id=attempt_id, job_id=1, command_sha256=digest)
    store.record_ack(attempt)
    store.write_command(attempt_id, command)
    loaded = store.load(attempt_id)
    assert loaded is not None
    return loaded


def test_real_systemd_attempt_hides_credentials_and_control_writes(monkeypatch):
    if not _user_systemd_available():
        pytest.skip("user systemd transient units are unavailable")

    monkeypatch.setenv("DISPATCH_NODE_TOKEN", "integration-test-token")
    monkeypatch.setenv("DISPATCH_NODE_ACTIVATION_NONCE", "integration-test-nonce")
    repository_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=".dispatch-systemd-test-", dir=repository_root
    ) as temporary:
        store = AttemptStore(Path(temporary) / "attempts")
        attempt_id = f"smoke-{os.getpid()}"
        control = store.control_evidence_dir(attempt_id)
        code = """import json, os
from pathlib import Path
control = Path({control!r})
denied = False
try:
    control.joinpath('workload-write').write_text('no')
except OSError:
    denied = True
Path('probe.json').write_text(json.dumps({{'token_present': bool(os.environ.get('DISPATCH_NODE_TOKEN')), 'nonce_present': bool(os.environ.get('DISPATCH_NODE_ACTIVATION_NONCE')), 'control_write_denied': denied}}))
""".format(control=str(control))
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        attempt = _prepare_attempt(store, attempt_id, command)

        launch(store, attempt, launcher=SystemdTransientLauncher())
        exit_code = _wait_for_terminal(store, attempt_id)

        probe_path = store.workload_dir(attempt_id) / "probe.json"
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        assert exit_code == 0
        assert probe == {
            "control_write_denied": True,
            "nonce_present": False,
            "token_present": False,
        }
        assert {
            "isolation.json",
            "launch-receipt.json",
            "terminal.json",
        } <= {path.name for path in control.iterdir()}
        assert not (control / "workload-write").exists()


def test_real_systemd_stop_terminates_the_transient_workload_cgroup():
    if not _user_systemd_available():
        pytest.skip("user systemd transient units are unavailable")

    repository_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=".dispatch-systemd-stop-", dir=repository_root
    ) as temporary:
        store = AttemptStore(Path(temporary) / "attempts")
        attempt_id = f"stop-{os.getpid()}"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(60)')}"
        attempt = _prepare_attempt(store, attempt_id, command)
        launcher = SystemdTransientLauncher()
        launched = launch(store, attempt, launcher=launcher)
        unit_name = safe_unit_name(attempt_id)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (store.workload_dir(attempt_id) / "supervisor.json").exists():
                    break
                time.sleep(0.1)
            assert (store.workload_dir(attempt_id) / "supervisor.json").exists()
            assert launcher.stop(launched, signal.SIGTERM)
            exit_code = _wait_for_terminal(store, attempt_id)
            # SIGTERM is normalized by the supervisor and proves the unit
            # stopped the supervised workload rather than returning success.
            assert exit_code != 0
        finally:
            subprocess.run(
                ["systemctl", "--user", "stop", unit_name],
                capture_output=True,
                timeout=10,
                check=False,
            )


def test_real_systemd_restart_store_recovers_supervisor_terminal_evidence():
    if not _user_systemd_available():
        pytest.skip("user systemd transient units are unavailable")

    repository_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=".dispatch-systemd-recovery-", dir=repository_root
    ) as temporary:
        root = Path(temporary) / "attempts"
        store = AttemptStore(root)
        attempt_id = f"recovery-{os.getpid()}"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(0.2)')}"
        attempt = _prepare_attempt(store, attempt_id, command)
        launch(store, attempt, launcher=SystemdTransientLauncher())

        # A new store represents an Agent process after restart.  It must use
        # supervisor-owned evidence, not the short-lived systemd-run client PID.
        restarted = AttemptStore(root)
        exit_code = _wait_for_terminal(restarted, attempt_id)
        assert exit_code == 0
        control = restarted.control_evidence_dir(attempt_id)
        assert (control / "terminal.json").exists()


def test_real_systemd_attempt_applies_resource_limits():
    if not _user_systemd_available():
        pytest.skip("user systemd transient units are unavailable")

    repository_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=".dispatch-systemd-resource-", dir=repository_root
    ) as temporary:
        store = AttemptStore(Path(temporary) / "attempts")
        attempt_id = f"resource-{os.getpid()}"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(5)')}"
        attempt = _prepare_attempt(store, attempt_id, command)
        launcher = SystemdTransientLauncher(
            resource_policy=ResourcePolicy(
                memory_max_bytes=64 * 1024 * 1024,
                cpu_quota_percent=100,
                tasks_max=32,
            )
        )
        launch(store, attempt, launcher=launcher)
        unit_name = safe_unit_name(attempt_id)
        try:
            deadline = time.monotonic() + 10
            properties = ""
            while time.monotonic() < deadline:
                result = subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        unit_name,
                        "--property=MemoryMax",
                        "--property=CPUQuotaPerSecUSec",
                        "--property=TasksMax",
                        "--value",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if result.returncode == 0:
                    properties = result.stdout
                    if "67108864" in properties and "32" in properties:
                        break
                time.sleep(0.1)
            assert "67108864" in properties
            assert "32" in properties
        finally:
            subprocess.run(
                ["systemctl", "--user", "stop", unit_name],
                capture_output=True,
                timeout=10,
                check=False,
            )
