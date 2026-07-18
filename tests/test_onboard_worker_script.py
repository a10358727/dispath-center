"""No-network tests for the interactive agentless SSH worker bootstrap."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "onboard-worker.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _fake_environment(tmp_path: Path, *, ssh_exit: int = 0) -> tuple[dict[str, str], Path]:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"

    _write_executable(
        fake_bin / "ssh-keygen",
        """#!/bin/bash
printf 'ssh-keygen' >> "$FAKE_CALLS"
printf ' %q' "$@" >> "$FAKE_CALLS"
printf '\n' >> "$FAKE_CALLS"
exec /usr/bin/ssh-keygen "$@"
""",
    )
    _write_executable(
        fake_bin / "ssh-copy-id",
        """#!/bin/bash
printf 'ssh-copy-id' >> "$FAKE_CALLS"
printf ' %q' "$@" >> "$FAKE_CALLS"
printf '\n' >> "$FAKE_CALLS"
exit 0
""",
    )
    _write_executable(
        fake_bin / "ssh",
        f"""#!/bin/bash
printf 'ssh' >> "$FAKE_CALLS"
printf ' %q' "$@" >> "$FAKE_CALLS"
printf '\n' >> "$FAKE_CALLS"
if [[ {ssh_exit} -eq 0 ]]; then
    printf 'worker prerequisites: ready\n'
fi
exit {ssh_exit}
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_CALLS": str(calls),
        }
    )
    return environment, calls


def _run(
    environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_onboard_worker_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_onboard_worker_generates_key_copies_it_and_prints_disabled_config(
    tmp_path: Path,
) -> None:
    environment, calls_path = _fake_environment(tmp_path)

    result = _run(
        environment,
        "--host",
        "100.64.0.21",
        "--user",
        "train",
        "--name",
        "worker-gpu-01",
        "--gpu",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    private_key = Path(environment["HOME"]) / ".ssh" / "dispatch_worker"
    assert private_key.is_file()
    assert private_key.with_suffix(".pub").is_file()
    assert stat.S_IMODE(private_key.stat().st_mode) == 0o600
    calls = calls_path.read_text(encoding="utf-8")
    assert "ssh-copy-id" in calls
    assert "dispatch_worker.pub" in calls
    assert "train@100.64.0.21" in calls
    assert "BatchMode=yes" in calls
    assert "StrictHostKeyChecking=no" in calls
    assert "UserKnownHostsFile=/dev/null" in calls
    assert "bash" in calls
    assert "tmux" in calls
    assert "rsync" in calls
    assert "nvidia-smi" in calls
    assert "password" not in calls.lower()
    assert "name: 'worker-gpu-01'" in result.stdout
    assert "key: '~/.ssh/dispatch_worker'" in result.stdout
    assert "gpu: true" in result.stdout
    assert "enabled: false" in result.stdout
    assert not (tmp_path / "servers.yaml").exists()


def test_onboard_worker_reuses_matching_existing_keypair(tmp_path: Path) -> None:
    environment, calls_path = _fake_environment(tmp_path)
    arguments = (
        "--host",
        "100.64.0.21",
        "--user",
        "train",
        "--name",
        "worker-01",
    )
    first = _run(environment, *arguments)
    assert first.returncode == 0, first.stdout + first.stderr
    calls_path.write_text("", encoding="utf-8")

    second = _run(environment, *arguments)

    assert second.returncode == 0, second.stdout + second.stderr
    assert "沿用既有專用 SSH key" in second.stdout
    calls = calls_path.read_text(encoding="utf-8")
    assert "ssh-keygen -y" in calls
    assert "ssh-copy-id" in calls


def test_onboard_worker_help_is_read_only(tmp_path: Path) -> None:
    environment, calls_path = _fake_environment(tmp_path)

    result = _run(environment, "--help")

    assert result.returncode == 0
    assert "ssh-copy-id" in result.stdout
    assert "不接受 --password" in result.stdout
    assert not calls_path.exists()
    assert not (Path(environment["HOME"]) / ".ssh").exists()


def test_onboard_worker_rejects_password_arguments_before_any_side_effect(
    tmp_path: Path,
) -> None:
    environment, calls_path = _fake_environment(tmp_path)

    result = _run(
        environment,
        "--host",
        "100.64.0.21",
        "--user",
        "train",
        "--name",
        "worker-01",
        "--password",
        "do-not-store-me",
    )

    assert result.returncode == 1
    assert "拒絕接收密碼" in result.stderr
    assert "do-not-store-me" not in result.stdout
    assert "do-not-store-me" not in result.stderr
    assert not calls_path.exists()
    assert not (Path(environment["HOME"]) / ".ssh").exists()


def test_onboard_worker_missing_tools_routes_to_platform_bootstrap(
    tmp_path: Path,
) -> None:
    """Goal 3 Phase B B3（docs/DECISIONS.md 2026-07-19）：工具缺失（遠端
    檢查 exit 20）不再中止——金鑰已配置完成，缺什麼交給平台的
    `server_bootstrap` 核准流程驗證與回報，真正的守門是 `server_add` 的
    bootstrap-report 閘。SSH 連不上（其他非零值）仍然中止。"""

    environment, _calls_path = _fake_environment(tmp_path, ssh_exit=20)

    result = _run(
        environment,
        "--host",
        "100.64.0.22",
        "--user",
        "train",
        "--name",
        "worker-02",
    )

    assert result.returncode == 0
    assert "缺少部分工具" in result.stdout
    assert "/servers/bootstrap-request" in result.stdout
    assert "servers:" in result.stdout
    assert "enabled: false" in result.stdout


def test_onboard_worker_still_aborts_when_ssh_is_unreachable(
    tmp_path: Path,
) -> None:
    environment, _calls_path = _fake_environment(tmp_path, ssh_exit=255)

    result = _run(
        environment,
        "--host",
        "100.64.0.22",
        "--user",
        "train",
        "--name",
        "worker-02",
    )

    assert result.returncode == 1
    assert "SSH 連線失敗" in result.stderr
    assert "servers:" not in result.stdout


def test_onboard_worker_source_never_handles_password_or_mutates_membership() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "servers.yaml" in source
    assert "不修改 servers.yaml" in source
    assert "ssh-copy-id" in source
    assert "read -s" not in source
    assert "sshpass" not in source
    assert "SSHPASS" not in source
    assert "expect" not in source
    assert "sudo" not in source
    assert "apt-get" not in source
    assert "systemctl" not in source
    assert "Node Agent" in source
