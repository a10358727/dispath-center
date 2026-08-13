"""No-network tests for the local one-click launcher."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "quickstart.sh"


def _copy_launcher_project(tmp_path: Path) -> Path:
    project = tmp_path / "dispatch-center"
    project.mkdir()
    shutil.copy2(SCRIPT, project / "quickstart.sh")
    shutil.copy2(ROOT / ".env.example", project / ".env.example")
    shutil.copy2(ROOT / "requirements.txt", project / "requirements.txt")
    shutil.copytree(
        ROOT / "app",
        project / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return project


def _safe_environment(project: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHON_BIN": sys.executable,
            "DISPATCH_VENV_DIR": sys.prefix,
            "DISPATCH_RUNTIME_DIR": str(project / ".runtime" / "dispatch-center"),
            "SERVERS_YAML_PATH": str(project / "servers.yaml"),
            "DB_PATH": str(project / "quickstart-test.db"),
            "AUDIT_PATH": str(project / "quickstart-test-audit.jsonl"),
            "AUTO_APPROVE_RULES_PATH": str(project / "no-auto-approve.yaml"),
            "API_HOST": "127.0.0.1",
            "API_PORT": "18080",
            "AUTH_TOKEN": "",
            "OIDC_ENABLED": "false",
            "OIDC_ISSUER": "",
            "OIDC_CLIENT_ID": "",
            "OIDC_CLIENT_SECRET": "",
            "OIDC_REDIRECT_URI": "",
            "AUTHORIZATION_MODE": "off",
            "ANTHROPIC_API_KEY": "",
            "VLLM_BASE_URL": "",
            "VLLM_MODEL": "",
            "VLLM_API_KEY": "",
            "SMTP_HOST": "",
            "SMTP_PORT": "",
            "SMTP_USER": "",
            "SMTP_PASS": "",
            "MAIL_FROM": "",
            "MAIL_TO": "",
            "CODEX_RUNNER_SERVER": "",
        }
    )
    return environment


def _run(
    project: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(project / "quickstart.sh"), *arguments],
        cwd=project,
        env=environment or _safe_environment(project),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _process_starttime(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return raw[raw.rfind(")") + 2 :].split()[19]


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(
        encoding="utf-8"
    ).strip()


def _process_is_running(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    fields_after_comm = raw[raw.rfind(")") + 2 :].split()
    return bool(fields_after_comm) and fields_after_comm[0] != "Z"


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def _read_process_record(runtime_dir: Path) -> tuple[int, str, str]:
    values = (runtime_dir / "app.pid").read_text(encoding="utf-8").splitlines()
    assert len(values) == 3
    return int(values[0]), values[1], values[2]


def _install_no_socket_process_harness(
    project: Path,
    *,
    ready_mode: str = "events",
) -> tuple[dict[str, str], Path, Path, Path, Path]:
    """Install wrappers that exercise real /proc and pidfd but no sockets."""

    events_file = project / "fake-app-events"
    probes_file = project / "fake-probes"
    pip_file = project / "fake-pip-calls"
    runtime_dir = project / ".runtime" / "dispatch-center"

    (project / "app" / "main.py").write_text(
        """import os
import signal
import time

events_path = os.environ["FAKE_APP_EVENTS"]

def emit(kind):
    with open(events_path, "a", encoding="utf-8") as stream:
        stream.write(f"{kind} {os.getpid()}\\n")

def stop(_signal_number, _frame):
    emit("term")
    if os.environ.get("FAKE_IGNORE_TERM") == "1":
        emit("ignored-term")
        return
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
emit("ready")
while True:
    time.sleep(60)
""",
        encoding="utf-8",
    )

    venv_dir = project / "fake-venv"
    (venv_dir / "bin").mkdir(parents=True)
    venv_python = venv_dir / "bin" / "python"
    venv_python.write_text(
        "\n".join(
            (
                "#!/usr/bin/env bash",
                'if [[ "${1-}" == "-m" && "${2-}" == "pip" ]]; then',
                '  printf "%s\\n" "$*" >> "$FAKE_PIP_CALLS"',
                "  exit 0",
                "fi",
                'if [[ "${1-}" == "-m" && "${2-}" == "app.main" ]]; then',
                f"  exec -a \"$0\" {shlex.quote(sys.executable)} \"$@\"",
                "fi",
                f"exec {shlex.quote(sys.executable)} \"$@\"",
                "",
            )
        ),
        encoding="utf-8",
    )
    venv_python.chmod(0o755)

    bootstrap_python = project / "fake-bootstrap-python"
    bootstrap_python.write_text(
        "\n".join(
            (
                "#!/usr/bin/env bash",
                'if [[ "${1-}" != "-" ]]; then',
                f"  exec {shlex.quote(sys.executable)} \"$@\"",
                "fi",
                'payload="$(mktemp \"${TMPDIR:-/tmp}/dispatch-probe.XXXXXX\")"',
                'trap \'rm -f -- "$payload"\' EXIT',
                'cat > "$payload"',
                'if grep -Fq "http.client.HTTPConnection" "$payload"; then',
                '  printf "http\\n" >> "$FAKE_PROBE_LOG"',
                '  if [[ "$FAKE_HTTP_READY_MODE" == "never" ]]; then',
                "    exit 1",
                "  fi",
                '  pid=""',
                '  if [[ -r "$DISPATCH_RUNTIME_DIR/app.pid" ]]; then',
                '    IFS= read -r pid < "$DISPATCH_RUNTIME_DIR/app.pid"',
                "  fi",
                '  [[ -n "$pid" && -r "$FAKE_APP_EVENTS" ]] || exit 1',
                '  grep -Fqx "ready $pid" "$FAKE_APP_EVENTS"',
                "  exit $?",
                "fi",
                'if grep -Fq "socket.create_connection" "$payload"; then',
                '  printf "port\\n" >> "$FAKE_PROBE_LOG"',
                '  : > "$FAKE_PORT_PROBED"',
                '  if [[ "$FAKE_PORT_OPEN" == "1" ]]; then exit 0; fi',
                "  exit 1",
                "fi",
                'if grep -Fq "print(fields_after_comm[19])" "$payload"; then',
                '  if [[ "$FAKE_DELAY_CHILD_STARTTIME" == "1"',
                '        && -e "$FAKE_PORT_PROBED"',
                '        && ! -e "$FAKE_DELAY_DONE" ]]; then',
                '    : > "$FAKE_DELAY_DONE"',
                '    sleep "$FAKE_DELAY_SECONDS"',
                "  fi",
                "fi",
                f"{shlex.quote(sys.executable)} \"$@\" < \"$payload\"",
                "status=$?",
                "exit $status",
                "",
            )
        ),
        encoding="utf-8",
    )
    bootstrap_python.chmod(0o755)

    environment = _safe_environment(project)
    environment.update(
        {
            "PYTHON_BIN": str(bootstrap_python),
            "DISPATCH_VENV_DIR": str(venv_dir),
            "DISPATCH_RUNTIME_DIR": str(runtime_dir),
            "DISPATCH_START_TIMEOUT_SEC": "20",
            "FAKE_APP_EVENTS": str(events_file),
            "FAKE_PROBE_LOG": str(probes_file),
            "FAKE_PIP_CALLS": str(pip_file),
            "FAKE_HTTP_READY_MODE": ready_mode,
            "FAKE_IGNORE_TERM": "0",
            "FAKE_PORT_OPEN": "0",
            "FAKE_PORT_PROBED": str(project / "fake-port-probed"),
            "FAKE_DELAY_CHILD_STARTTIME": "0",
            "FAKE_DELAY_DONE": str(project / "fake-delay-done"),
            "FAKE_DELAY_SECONDS": "2",
            "PIP_NO_INDEX": "1",
        }
    )
    return environment, runtime_dir, events_file, probes_file, pip_file


def _terminate_test_process(pid: int, project: Path) -> None:
    if not _process_is_running(pid):
        return
    try:
        cwd = os.path.realpath(f"/proc/{pid}/cwd")
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, PermissionError):
        return
    expected_argv = [
        os.fsencode(project / "fake-venv" / "bin" / "python"),
        b"-m",
        b"app.main",
    ]
    if cwd != str(project) or argv[:3] != expected_argv:
        return
    try:
        pidfd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        except OSError:
            return
        try:
            _wait_until(lambda: not _process_is_running(pid), timeout=3)
        except AssertionError:
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            except OSError:
                pass
    finally:
        os.close(pidfd)


def test_quickstart_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_quickstart_help_is_read_only(tmp_path: Path) -> None:
    project = tmp_path / "help-project"
    project.mkdir()
    shutil.copy2(SCRIPT, project / "quickstart.sh")

    result = _run(project, "--help", environment=os.environ.copy())

    assert result.returncode == 0
    assert "一鍵安裝/更新依賴" in result.stdout
    assert "只啟動 app.main；不會開 MCP、cloudflared 或 worker process" in result.stdout
    assert "新建的空 servers.yaml 不探測主機" in result.stdout
    assert sorted(path.name for path in project.iterdir()) == ["quickstart.sh"]


def test_quickstart_automatically_skips_incompatible_path_python(
    tmp_path: Path,
) -> None:
    system_python = Path("/usr/bin/python3")
    support_probe = subprocess.run(
        [
            str(system_python),
            "-c",
            (
                "import os, signal, sys; "
                "raise SystemExit(0 if sys.version_info >= (3, 10) "
                "and hasattr(os, 'pidfd_open') "
                "and hasattr(signal, 'pidfd_send_signal') else 1)"
            ),
        ],
        check=False,
    )
    if support_probe.returncode != 0:
        pytest.skip("system /usr/bin/python3 does not support the launcher")

    project = _copy_launcher_project(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    incompatible_python = fake_bin / "python3"
    incompatible_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    incompatible_python.chmod(0o755)
    environment = _safe_environment(project)
    environment.pop("PYTHON_BIN")
    environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"

    result = _run(project, "status", environment=environment)

    assert result.returncode == 1
    assert "使用 Python 建立／管理專案環境：/usr/bin/python3" in result.stdout
    assert "狀態：stopped" in result.stdout
    assert "找不到支援" not in result.stderr


def test_quickstart_rejects_explicit_incompatible_python_without_fallback(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    incompatible_python = tmp_path / "incompatible-python"
    incompatible_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    incompatible_python.chmod(0o755)
    environment = _safe_environment(project)
    environment["PYTHON_BIN"] = str(incompatible_python)

    result = _run(project, "status", environment=environment)

    assert result.returncode == 1
    assert "PYTHON_BIN 指定的 Python 不符合需求" in result.stderr
    assert "/usr/bin/python3" not in result.stdout


def test_quickstart_setup_creates_private_safe_defaults(tmp_path: Path) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    environment["AUTH_TOKEN"] = "TEST-TOKEN-MUST-NOT-PRINT"

    result = _run(project, "setup", "--skip-deps", environment=environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "TEST-TOKEN-MUST-NOT-PRINT" not in result.stdout
    assert "TEST-TOKEN-MUST-NOT-PRINT" not in result.stderr
    assert (project / "servers.yaml").read_text(encoding="utf-8") == "servers: []\n"
    assert stat.S_IMODE((project / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((project / "servers.yaml").stat().st_mode) == 0o600
    assert stat.S_IMODE(
        (project / ".runtime" / "dispatch-center").stat().st_mode
    ) == 0o700


def test_quickstart_never_sources_dotenv(tmp_path: Path) -> None:
    project = _copy_launcher_project(tmp_path)
    marker = tmp_path / "dotenv-was-executed"
    (project / ".env.example").write_text(
        "\n".join(
            (
                "API_HOST=127.0.0.1",
                "API_PORT=18080",
                "AUTH_TOKEN=",
                "OIDC_ENABLED=false",
                f"UNUSED_VALUE=$(touch {marker})",
                "",
            )
        ),
        encoding="utf-8",
    )

    result = _run(project, "setup", "--skip-deps")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()


def test_quickstart_rejects_open_non_loopback_bind(tmp_path: Path) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    environment["API_HOST"] = "192.168.50.10"

    result = _run(project, "setup", "--skip-deps", environment=environment)

    assert result.returncode == 1
    assert "open-development 只准綁 loopback" in result.stderr


def test_quickstart_rejects_public_bind_even_with_token(tmp_path: Path) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    environment["API_HOST"] = "192.0.2.10"
    environment["AUTH_TOKEN"] = "TEST-TOKEN-MUST-NOT-PRINT"

    result = _run(project, "setup", "--skip-deps", environment=environment)

    assert result.returncode == 1
    assert (
        "API_HOST must resolve only to loopback/private addresses" in result.stderr
    )
    assert "TEST-TOKEN-MUST-NOT-PRINT" not in result.stdout + result.stderr


def test_quickstart_rejects_wildcard_bind_even_with_token(tmp_path: Path) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    environment["API_HOST"] = "0.0.0.0"
    environment["AUTH_TOKEN"] = "TEST-TOKEN-MUST-NOT-PRINT"

    result = _run(project, "setup", "--skip-deps", environment=environment)

    assert result.returncode == 1
    assert (
        "API_HOST must resolve only to loopback/private addresses" in result.stderr
    )
    assert "TEST-TOKEN-MUST-NOT-PRINT" not in result.stdout + result.stderr


def test_quickstart_rejects_expanded_ipv6_unspecified_bind(tmp_path: Path) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    environment["API_HOST"] = "0:0:0:0:0:0:0:0"
    environment["AUTH_TOKEN"] = "TEST-TOKEN-MUST-NOT-PRINT"

    result = _run(project, "setup", "--skip-deps", environment=environment)

    assert result.returncode == 1
    assert (
        "API_HOST must resolve only to loopback/private addresses" in result.stderr
    )
    assert "TEST-TOKEN-MUST-NOT-PRINT" not in result.stdout + result.stderr


def test_quickstart_accepts_expanded_ipv6_loopback_without_auth(tmp_path: Path) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    environment["API_HOST"] = "0:0:0:0:0:0:0:1"

    result = _run(project, "setup", "--skip-deps", environment=environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "open-development" in result.stdout


def test_quickstart_rejects_dotenv_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    target = tmp_path / "outside-dotenv"
    target.write_text("DO_NOT_TOUCH=yes\n", encoding="utf-8")
    target.chmod(0o644)
    (project / ".env").symlink_to(target)

    result = _run(project, "setup", "--skip-deps")

    assert result.returncode == 1
    assert "拒絕 symlink .env" in result.stderr
    assert target.read_text(encoding="utf-8") == "DO_NOT_TOUCH=yes\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_quickstart_rejects_servers_yaml_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    target = tmp_path / "outside-servers.yaml"
    target.write_text("servers:\n  - do-not-touch\n", encoding="utf-8")
    (project / "servers.yaml").symlink_to(target)

    result = _run(project, "setup", "--skip-deps")

    assert result.returncode == 1
    assert "拒絕 symlink servers.yaml" in result.stderr
    assert target.read_text(encoding="utf-8") == "servers:\n  - do-not-touch\n"


def test_quickstart_rejects_runtime_directory_symlink(tmp_path: Path) -> None:
    project = _copy_launcher_project(tmp_path)
    outside_runtime = tmp_path / "outside-runtime"
    outside_runtime.mkdir()
    runtime_parent = project / ".runtime"
    runtime_parent.mkdir()
    (runtime_parent / "dispatch-center").symlink_to(
        outside_runtime,
        target_is_directory=True,
    )

    result = _run(project, "setup", "--skip-deps")

    assert result.returncode == 1
    assert "symlink" in result.stderr
    assert "runtime directory" in result.stderr
    assert list(outside_runtime.iterdir()) == []


@pytest.mark.parametrize(
    "leaf_name",
    ("app.pid", "app.endpoint", "app.log", "app.log.1", "manager.lock"),
)
@pytest.mark.parametrize("node_kind", ("directory", "fifo"))
def test_quickstart_rejects_non_regular_runtime_metadata_without_mutation(
    tmp_path: Path,
    leaf_name: str,
    node_kind: str,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    runtime_dir = Path(environment["DISPATCH_RUNTIME_DIR"])
    runtime_dir.mkdir(parents=True)
    runtime_dir.chmod(0o700)
    leaf = runtime_dir / leaf_name

    if node_kind == "directory":
        leaf.mkdir()
        leaf.chmod(0o751)
        (leaf / "sentinel").write_text("DO-NOT-MOVE\n", encoding="utf-8")
    else:
        os.mkfifo(leaf, mode=0o640)

    companion_log: Path | None = None
    if leaf_name == "app.log.1":
        companion_log = runtime_dir / "app.log"
        companion_log.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        companion_log.chmod(0o640)
    elif leaf_name == "app.log":
        companion_log = runtime_dir / "app.log.1"
        companion_log.write_text("OLD-LOG-MUST-STAY\n", encoding="utf-8")
        companion_log.chmod(0o640)

    leaf_before = leaf.lstat()
    companion_before = companion_log.stat() if companion_log else None

    result = _run(project, "status", environment=environment)

    assert result.returncode == 1
    assert "runtime metadata 必須是一般檔案" in result.stderr
    leaf_after = leaf.lstat()
    assert stat.S_IFMT(leaf_after.st_mode) == stat.S_IFMT(leaf_before.st_mode)
    assert stat.S_IMODE(leaf_after.st_mode) == stat.S_IMODE(leaf_before.st_mode)
    if node_kind == "directory":
        assert (leaf / "sentinel").read_text(encoding="utf-8") == "DO-NOT-MOVE\n"
    else:
        assert stat.S_ISFIFO(leaf_after.st_mode)
    if companion_log is not None and companion_before is not None:
        companion_after = companion_log.stat()
        assert stat.S_ISREG(companion_after.st_mode)
        assert companion_after.st_size == companion_before.st_size
        assert stat.S_IMODE(companion_after.st_mode) == stat.S_IMODE(
            companion_before.st_mode
        )


@pytest.mark.parametrize(
    "leaf_name",
    ("app.pid", "app.endpoint", "app.log", "app.log.1", "manager.lock"),
)
def test_quickstart_rejects_metadata_leaf_symlink_without_touching_target(
    tmp_path: Path,
    leaf_name: str,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    runtime_dir = Path(environment["DISPATCH_RUNTIME_DIR"])
    runtime_dir.mkdir(parents=True)
    runtime_dir.chmod(0o700)
    target = tmp_path / f"outside-{leaf_name}"
    target.write_text(f"{leaf_name}: DO-NOT-TOUCH\n", encoding="utf-8")
    target.chmod(0o644)
    target_before = target.stat()
    leaf = runtime_dir / leaf_name
    leaf.symlink_to(target)

    result = _run(project, "status", environment=environment)

    assert result.returncode == 1
    assert "拒絕 symlink runtime metadata" in result.stderr
    assert leaf.is_symlink()
    assert Path(os.readlink(leaf)) == target
    assert target.read_text(encoding="utf-8") == f"{leaf_name}: DO-NOT-TOUCH\n"
    target_after = target.stat()
    assert target_after.st_ino == target_before.st_ino
    assert target_after.st_size == target_before.st_size
    assert target_after.st_mtime_ns == target_before.st_mtime_ns
    assert stat.S_IMODE(target_after.st_mode) == stat.S_IMODE(target_before.st_mode)


def test_quickstart_manager_lock_is_atomic_and_rejects_concurrent_command(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    runtime_dir = Path(environment["DISPATCH_RUNTIME_DIR"])
    runtime_dir.mkdir(parents=True)
    runtime_dir.chmod(0o700)
    lock_file = runtime_dir / "manager.lock"
    marker = project / "lock-held"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys, time; "
                "stream=open(sys.argv[1], 'a'); "
                "fcntl.flock(stream, fcntl.LOCK_EX); "
                "pathlib.Path(sys.argv[2]).write_text('held'); "
                "time.sleep(60)"
            ),
            str(lock_file),
            str(marker),
        ],
        cwd=project,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until(marker.is_file)

        result = _run(project, "status", environment=environment)

        assert result.returncode == 1
        assert "另一個 quickstart 管理命令正在執行" in result.stderr
        assert holder.poll() is None
        assert lock_file.is_file()
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    after_release = _run(project, "status", environment=environment)
    assert after_release.returncode == 1
    assert "狀態：stopped" in after_release.stdout
    assert "另一個 quickstart" not in after_release.stderr


def test_quickstart_setup_refuses_to_mutate_live_service_venv(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    # This test process has the exact managed argv/cwd shape but performs no
    # imports, socket operations, or external I/O.
    (project / "app" / "main.py").write_text(
        "import time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    (project / "requirements.txt").write_text("", encoding="utf-8")

    venv_dir = project / "test-venv"
    (venv_dir / "bin").mkdir(parents=True)
    venv_python = venv_dir / "bin" / "python"
    pip_marker = project / "pip-was-invoked"
    venv_python.write_text(
        "\n".join(
            (
                "#!/usr/bin/env bash",
                'if [[ "$1" == "-m" && "$2" == "pip" ]]; then',
                f"  printf '%s\\n' pip >> {shlex.quote(str(pip_marker))}",
                "  exit 0",
                "fi",
                'if [[ "$1" == "-m" && "$2" == "app.main" ]]; then',
                f"  exec -a \"$0\" {shlex.quote(sys.executable)} \"$@\"",
                "fi",
                f"exec {shlex.quote(sys.executable)} \"$@\"",
                "",
            )
        ),
        encoding="utf-8",
    )
    venv_python.chmod(0o755)
    stamp = venv_dir / ".dispatch-requirements.sha256"
    stamp.write_text("DO-NOT-CHANGE\n", encoding="utf-8")

    environment = _safe_environment(project)
    environment.update(
        {
            "DISPATCH_VENV_DIR": str(venv_dir),
            "PIP_NO_INDEX": "1",
        }
    )
    process = subprocess.Popen(
        [str(venv_python), "-m", "app.main"],
        cwd=project,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        expected_cmdline = [
            os.fsencode(venv_python),
            b"-m",
            b"app.main",
        ]
        cmdline: list[bytes] = []
        for _ in range(200):
            cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes().split(
                b"\0"
            )
            if cmdline[:3] == expected_cmdline:
                break
            assert process.poll() is None
            time.sleep(0.01)
        assert cmdline[:3] == expected_cmdline
        raw_stat = Path(f"/proc/{process.pid}/stat").read_text(encoding="utf-8")
        starttime = raw_stat[raw_stat.rfind(")") + 2 :].split()[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
        runtime_dir = Path(environment["DISPATCH_RUNTIME_DIR"])
        runtime_dir.mkdir(parents=True)
        runtime_dir.chmod(0o700)
        (runtime_dir / "app.pid").write_text(
            f"{process.pid}\n{starttime}\n{boot_id}\n",
            encoding="utf-8",
        )

        result = _run(
            project,
            "setup",
            "--force-deps",
            environment=environment,
        )

        assert result.returncode == 1
        assert "服務仍在運行" in result.stderr
        assert stamp.read_text(encoding="utf-8") == "DO-NOT-CHANGE\n"
        assert not pip_marker.exists()
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_quickstart_no_socket_background_lifecycle_uses_pidfd_and_is_idempotent(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment, runtime_dir, events_file, probes_file, pip_file = (
        _install_no_socket_process_harness(project)
    )
    managed_pids: list[int] = []
    try:
        started = _run(
            project,
            "start",
            "--skip-deps",
            environment=environment,
        )
        assert started.returncode == 0, started.stdout + started.stderr
        first_pid, first_starttime, first_boot_id = _read_process_record(
            runtime_dir
        )
        managed_pids.append(first_pid)
        assert first_starttime == _process_starttime(first_pid)
        assert first_boot_id == _boot_id()
        assert f"ready {first_pid}" in events_file.read_text(encoding="utf-8")
        assert _process_is_running(first_pid)
        inherited_fds = {
            os.path.realpath(path)
            for path in Path(f"/proc/{first_pid}/fd").iterdir()
        }
        assert str(runtime_dir / "manager.lock") not in inherited_fds

        status_result = _run(project, "status", environment=environment)
        assert status_result.returncode == 0, (
            status_result.stdout + status_result.stderr
        )
        assert f"PID {first_pid}" in status_result.stdout

        pip_before = pip_file.read_text(encoding="utf-8")
        idempotent = _run(
            project,
            "start",
            "--force-deps",
            environment=environment,
        )
        assert idempotent.returncode == 0, idempotent.stdout + idempotent.stderr
        assert "服務已在運行" in idempotent.stdout
        assert _read_process_record(runtime_dir)[0] == first_pid
        assert pip_file.read_text(encoding="utf-8") == pip_before

        restarted = _run(
            project,
            "restart",
            "--skip-deps",
            environment=environment,
        )
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        second_pid, second_starttime, second_boot_id = _read_process_record(
            runtime_dir
        )
        managed_pids.append(second_pid)
        assert second_pid != first_pid
        assert second_starttime == _process_starttime(second_pid)
        assert second_boot_id == first_boot_id
        events = events_file.read_text(encoding="utf-8")
        assert f"term {first_pid}" in events
        assert f"ready {second_pid}" in events
        assert not _process_is_running(first_pid)

        stopped = _run(project, "stop", environment=environment)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "服務已停止" in stopped.stdout
        events = events_file.read_text(encoding="utf-8")
        assert f"term {second_pid}" in events
        assert not _process_is_running(second_pid)
        assert not (runtime_dir / "app.pid").exists()
        assert not (runtime_dir / "app.endpoint").exists()
        assert (runtime_dir / "manager.lock").is_file()
        assert stat.S_IMODE((runtime_dir / "manager.lock").stat().st_mode) == 0o600
        assert stat.S_IMODE((runtime_dir / "app.log").stat().st_mode) == 0o600

        probes = probes_file.read_text(encoding="utf-8").splitlines()
        assert "port" in probes
        assert "http" in probes
        assert set(probes) == {"port", "http"}
    finally:
        for pid in managed_pids:
            _terminate_test_process(pid, project)


def test_quickstart_stop_rejects_foreign_live_pid_and_leaves_it_running(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment = _safe_environment(project)
    runtime_dir = Path(environment["DISPATCH_RUNTIME_DIR"])
    runtime_dir.mkdir(parents=True)
    runtime_dir.chmod(0o700)
    foreign = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=project,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        record = f"{foreign.pid}\n{_process_starttime(foreign.pid)}\n{_boot_id()}\n"
        record_path = runtime_dir / "app.pid"
        record_path.write_text(record, encoding="utf-8")
        record_path.chmod(0o600)
        endpoint_path = runtime_dir / "app.endpoint"
        endpoint_path.write_text("127.0.0.1\n18080\n", encoding="utf-8")
        endpoint_path.chmod(0o600)

        result = _run(project, "stop", environment=environment)

        assert result.returncode == 1
        assert "存活但不相符的行程" in result.stderr
        assert foreign.poll() is None
        assert _process_is_running(foreign.pid)
        assert record_path.read_text(encoding="utf-8") == record
        assert endpoint_path.read_text(encoding="utf-8") == "127.0.0.1\n18080\n"
    finally:
        foreign.terminate()
        foreign.wait(timeout=10)


def test_quickstart_refuses_foreign_open_port_without_spawning_app(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment, runtime_dir, events_file, probes_file, _ = (
        _install_no_socket_process_harness(project)
    )
    environment["FAKE_PORT_OPEN"] = "1"

    result = _run(project, "start", "--skip-deps", environment=environment)

    assert result.returncode == 1
    assert "已被其他行程使用" in result.stderr
    assert not events_file.exists()
    assert not (runtime_dir / "app.pid").exists()
    assert probes_file.read_text(encoding="utf-8").splitlines() == ["port"]


def test_quickstart_background_readiness_timeout_terminates_and_cleans_metadata(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment, runtime_dir, events_file, probes_file, _pip_file = (
        _install_no_socket_process_harness(project, ready_mode="never")
    )
    environment["DISPATCH_START_TIMEOUT_SEC"] = "1"
    managed_pid: int | None = None
    try:
        result = _run(
            project,
            "start",
            "--skip-deps",
            environment=environment,
        )

        assert result.returncode == 1
        assert "啟動逾時" in result.stdout
        assert "1s 內未 ready" in result.stderr
        event_lines = events_file.read_text(encoding="utf-8").splitlines()
        ready_line = next(line for line in event_lines if line.startswith("ready "))
        managed_pid = int(ready_line.split()[1])
        assert f"term {managed_pid}" in event_lines
        assert not _process_is_running(managed_pid)
        assert not (runtime_dir / "app.pid").exists()
        assert not (runtime_dir / "app.endpoint").exists()
        assert (runtime_dir / "manager.lock").is_file()
        probes = probes_file.read_text(encoding="utf-8").splitlines()
        assert "port" in probes
        assert "http" in probes
    finally:
        if managed_pid is not None:
            _terminate_test_process(managed_pid, project)


def test_quickstart_stop_escalates_verified_child_to_pidfd_sigkill(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment, runtime_dir, events_file, _, _ = (
        _install_no_socket_process_harness(project)
    )
    environment["FAKE_IGNORE_TERM"] = "1"
    managed_pid: int | None = None
    try:
        started = _run(project, "start", "--skip-deps", environment=environment)
        assert started.returncode == 0, started.stdout + started.stderr
        managed_pid = _read_process_record(runtime_dir)[0]

        stopped = _run(project, "stop", environment=environment)

        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "SIGKILL" in stopped.stderr
        assert f"term {managed_pid}" in events_file.read_text(
            encoding="utf-8"
        ).splitlines()
        assert f"ignored-term {managed_pid}" in events_file.read_text(
            encoding="utf-8"
        ).splitlines()
        assert not _process_is_running(managed_pid)
        assert not (runtime_dir / "app.pid").exists()
        managed_pid = None
    finally:
        if managed_pid is not None:
            _terminate_test_process(managed_pid, project)


def test_quickstart_sigterm_during_background_start_cleans_child_and_metadata(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment, runtime_dir, events_file, probes_file, _pip_file = (
        _install_no_socket_process_harness(project, ready_mode="never")
    )
    environment["FAKE_DELAY_CHILD_STARTTIME"] = "1"
    environment["FAKE_DELAY_SECONDS"] = "4"
    manager = subprocess.Popen(
        ["bash", str(project / "quickstart.sh"), "start", "--skip-deps"],
        cwd=project,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    managed_pid: int | None = None
    try:
        def child_exists_before_metadata_publication() -> bool:
            nonlocal managed_pid
            if manager.poll() is not None:
                return False
            if not events_file.exists() or (runtime_dir / "app.pid").exists():
                return False
            ready_lines = [
                line
                for line in events_file.read_text(encoding="utf-8").splitlines()
                if line.startswith("ready ")
            ]
            if not ready_lines or not probes_file.exists():
                return False
            managed_pid = int(ready_lines[-1].split()[1])
            probes = probes_file.read_text(encoding="utf-8")
            return "port\n" in probes

        _wait_until(child_exists_before_metadata_publication)
        assert managed_pid is not None
        assert not (runtime_dir / "app.pid").exists()
        contender = _run(project, "status", environment=environment)
        assert contender.returncode == 1
        assert "另一個 quickstart 管理命令正在執行" in contender.stderr
        assert not (runtime_dir / "app.pid").exists()
        manager.send_signal(signal.SIGTERM)
        stdout, stderr = manager.communicate(timeout=15)

        assert manager.returncode == 143, stdout + stderr
        assert "啟動管理器收到 signal" in stderr
        assert f"term {managed_pid}" in events_file.read_text(encoding="utf-8")
        assert not _process_is_running(managed_pid)
        assert not (runtime_dir / "app.pid").exists()
        assert not (runtime_dir / "app.endpoint").exists()
        assert (runtime_dir / "manager.lock").is_file()
    finally:
        if manager.poll() is None:
            manager.kill()
            manager.communicate(timeout=10)
        if managed_pid is not None:
            _terminate_test_process(managed_pid, project)


def test_quickstart_foreground_exec_and_stale_metadata_recovery(
    tmp_path: Path,
) -> None:
    project = _copy_launcher_project(tmp_path)
    environment, runtime_dir, events_file, _, _ = (
        _install_no_socket_process_harness(project)
    )
    foreground = subprocess.Popen(
        ["bash", str(project / "quickstart.sh"), "foreground", "--skip-deps"],
        cwd=project,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    foreground_pid = foreground.pid
    try:
        _wait_until(
            lambda: (runtime_dir / "app.pid").is_file()
            and events_file.is_file()
            and f"ready {foreground_pid}"
            in events_file.read_text(encoding="utf-8").splitlines()
        )
        assert _read_process_record(runtime_dir)[0] == foreground_pid
        status_result = _run(project, "status", environment=environment)
        assert status_result.returncode == 0, (
            status_result.stdout + status_result.stderr
        )

        foreground.send_signal(signal.SIGINT)
        foreground.communicate(timeout=10)
        assert not _process_is_running(foreground_pid)
        assert (runtime_dir / "app.pid").exists()

        cleanup = _run(project, "stop", environment=environment)
        assert cleanup.returncode == 0, cleanup.stdout + cleanup.stderr
        assert "stale metadata 已清除" in cleanup.stdout
        assert not (runtime_dir / "app.pid").exists()
        assert not (runtime_dir / "app.endpoint").exists()
    finally:
        if foreground.poll() is None:
            foreground.terminate()
            foreground.communicate(timeout=10)
        _terminate_test_process(foreground_pid, project)


def test_quickstart_source_has_no_secret_output_or_dotenv_eval() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "MCP_BRIDGE_PATH_SECRET" not in source
    assert "source $ENV_FILE" not in source
    assert "source \"$ENV_FILE\"" not in source
    assert re.search(r"^\s*eval\b", source, re.MULTILINE) is None
    assert 'nohup "$VENV_PYTHON" -m app.main' in source
    assert "os.pidfd_open" in source
    assert "signal.pidfd_send_signal" in source
    assert 'f"/proc/{pid}/cmdline"' in source
    assert 'f"/proc/{pid}/cwd"' in source
    assert re.search(r"\bread_pid\b", source) is None
    assert re.search(r"\bpid_is_alive\b", source) is None
    assert re.search(r"\bpid_is_owned\b", source) is None
