"""Platform-managed runner-agent install and launch (DG-AGENT-RUNNER-INSTALL-v1).

An ``agent_runner_enroll`` card may carry an ``install`` spec. At approval time,
after the credential is minted, the platform provisions the runner over the
already-trusted SSH channel instead of showing the credential to a human:

1. resolve the worker account's home (closed command);
2. upload a source archive of ``dispatch_agent/`` (built on Server A) by SFTP;
3. write ``config.json`` / ``agent.env`` (and optionally ``claude.env``) by
   SFTP, then ``chmod 600`` (closed command);
4. ``pip install --user`` the package, ``dispatch-agent --check``;
5. launch it inside a detached ``tmux`` session (idempotent).

A keepalive tick relaunches managed runners that are not connected. Nothing
here needs ``sudo``, systemd user lingering, or an inbound port on the worker.

Boundaries:

- every remote command is a pure builder over validated values
  (``shlex.quote``); user text never reaches a shell;
- the runner credential and the Claude token exist only in memory inside the
  provisioning call: never in DB, audit, logs, or returned detail text;
- unreachable / failed steps are reported as a classified step; a failed
  install keeps the enrolment (the caller then shows the credential once so the
  operator can finish by hand).
"""

from __future__ import annotations

import io
import json
import re
import shlex
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.sshpool import SSHUnreachableError

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "dispatch_agent"
LAUNCH_MODE = "platform_tmux"
TMUX_SESSION = "dispatch-agent"
CONFIG_DIR_REL = ".config/dispatch-agent"
SRC_DIR_REL = ".local/share/dispatch-agent/src"
ARCHIVE_REL = ".local/share/dispatch-agent/dispatch_agent_src.tar.gz"
BIN_REL = ".local/bin/dispatch-agent"
DEFAULT_WORKSPACE_ROOT = "~/dispatch_workspaces"
MAX_DETAIL_CHARS = 300

INSTALL_TIMEOUT_SEC = 600
CHECK_TIMEOUT_SEC = 60
LAUNCH_TIMEOUT_SEC = 30
HOME_TIMEOUT_SEC = 15

_WORKSPACE_ROOT_RE = re.compile(r"^~/[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}$")
_HOME_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")
_SERVER_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
_RUNNER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CLAUDE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{20,512}$")

STEPS = ("home", "archive", "upload", "config", "install", "check", "launch")


class InvalidRunnerInstallSpecError(ValueError):
    """The ``install`` spec on an enrol card is malformed."""


@dataclass(frozen=True)
class InstallOutcome:
    ok: bool
    step: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "step": self.step, "detail": self.detail}


def validate_install_spec(spec: Any) -> dict[str, Any]:
    """Return the normalized ``install`` payload fragment or raise."""

    if not isinstance(spec, dict):
        raise InvalidRunnerInstallSpecError("install 必須是物件")
    allowed = {"workspace_root", "launch_mode"}
    if set(spec) - allowed:
        raise InvalidRunnerInstallSpecError("install 含有未知欄位")
    workspace_root = spec.get("workspace_root", DEFAULT_WORKSPACE_ROOT)
    if not isinstance(workspace_root, str) or not _WORKSPACE_ROOT_RE.match(workspace_root) or "/../" in workspace_root or workspace_root.endswith("/.."):
        raise InvalidRunnerInstallSpecError("workspace_root 必須是家目錄底下的相對路徑（~/...）")
    launch_mode = spec.get("launch_mode", LAUNCH_MODE)
    if launch_mode != LAUNCH_MODE:
        raise InvalidRunnerInstallSpecError("launch_mode 只支援 platform_tmux")
    return {"workspace_root": workspace_root.rstrip("/"), "launch_mode": LAUNCH_MODE}


def is_managed_install(payload: Any) -> bool:
    install = payload.get("install") if isinstance(payload, dict) else None
    return isinstance(install, dict) and install.get("launch_mode") == LAUNCH_MODE


def validate_server_url(url: str) -> str:
    text = (url or "").strip().rstrip("/")
    if not _SERVER_URL_RE.match(text):
        raise InvalidRunnerInstallSpecError("runner server_url 必須是 http(s)://host[:port]，不含路徑或帳密")
    return text


def validate_claude_token(token: str) -> str:
    text = (token or "").strip()
    if not _CLAUDE_TOKEN_RE.match(text):
        raise InvalidRunnerInstallSpecError("Claude token 格式不正確")
    return text


# --- pure builders -----------------------------------------------------------

def build_home_command() -> str:
    return 'printf %s "$HOME"'


def parse_home(stdout: str) -> str:
    home = (stdout or "").strip()
    if not _HOME_RE.match(home) or "/../" in home:
        raise InvalidRunnerInstallSpecError("worker home directory has an unexpected shape")
    return home.rstrip("/")


def remote_paths(home: str) -> dict[str, str]:
    return {
        "config_dir": f"{home}/{CONFIG_DIR_REL}",
        "config_json": f"{home}/{CONFIG_DIR_REL}/config.json",
        "agent_env": f"{home}/{CONFIG_DIR_REL}/agent.env",
        "claude_env": f"{home}/{CONFIG_DIR_REL}/claude.env",
        "src_dir": f"{home}/{SRC_DIR_REL}",
        "archive": f"{home}/{ARCHIVE_REL}",
        "bin": f"{home}/{BIN_REL}",
    }


def build_prepare_command(home: str) -> str:
    p = remote_paths(home)
    return (
        f"umask 077 && mkdir -p {shlex.quote(p['config_dir'])} {shlex.quote(p['src_dir'])}"
        f" && chmod 700 {shlex.quote(p['config_dir'])}"
    )


def build_secure_files_command(home: str, *, with_claude_env: bool) -> str:
    p = remote_paths(home)
    files = [p["config_json"], p["agent_env"]] + ([p["claude_env"]] if with_claude_env else [])
    return "chmod 600 " + " ".join(shlex.quote(f) for f in files)


def build_install_command(home: str) -> str:
    p = remote_paths(home)
    return (
        f"rm -rf {shlex.quote(p['src_dir'])} && mkdir -p {shlex.quote(p['src_dir'])}"
        f" && tar -xzf {shlex.quote(p['archive'])} -C {shlex.quote(p['src_dir'])}"
        f" && python3 -m pip install --user --quiet --upgrade {shlex.quote(p['src_dir'])}"
        " && echo RUNNER_INSTALLED"
    )


def build_check_command(home: str) -> str:
    p = remote_paths(home)
    return f"{shlex.quote(p['bin'])} --check && echo RUNNER_CHECK_OK"


def build_launch_command(home: str) -> str:
    """Idempotent: an existing session is left alone."""

    p = remote_paths(home)
    inner = shlex.quote(f"exec {p['bin']} run")
    return (
        f"tmux has-session -t {TMUX_SESSION} 2>/dev/null"
        f" || tmux new-session -d -s {TMUX_SESSION} {inner}"
        " && echo RUNNER_LAUNCHED"
    )


def build_restart_command(home: str) -> str:
    p = remote_paths(home)
    inner = shlex.quote(f"exec {p['bin']} run")
    return (
        f"tmux kill-session -t {TMUX_SESSION} 2>/dev/null; "
        f"tmux new-session -d -s {TMUX_SESSION} {inner} && echo RUNNER_LAUNCHED"
    )


def render_config_json(*, server_url: str, runner_name: str, workspace_root: str) -> str:
    if not _RUNNER_NAME_RE.match(runner_name):
        raise InvalidRunnerInstallSpecError("runner_name 形狀不合法")
    return json.dumps(
        {
            "server_url": validate_server_url(server_url),
            "runner_name": runner_name,
            "workspace_root": validate_install_spec({"workspace_root": workspace_root})["workspace_root"],
        },
        indent=2,
    ) + "\n"


def render_agent_env(token: str) -> str:
    if not re.match(r"^dar_[0-9a-f-]{36}\.[A-Za-z0-9_-]{16,256}$", token or ""):
        raise InvalidRunnerInstallSpecError("runner credential 形狀不合法")
    return f"DISPATCH_AGENT_CREDENTIAL={token}\n"


def render_claude_env(token: str) -> str:
    return f"CLAUDE_CODE_OAUTH_TOKEN={validate_claude_token(token)}\n"


def build_source_archive(package_dir: Path = PACKAGE_DIR) -> bytes:
    """tar.gz of the runner package source (pyproject + modules + service unit)."""

    if not (package_dir / "pyproject.toml").is_file():
        raise FileNotFoundError("dispatch_agent package source is not available")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(package_dir.iterdir()):
            if path.name.startswith(".") or path.name == "__pycache__" or path.name.endswith(".egg-info"):
                continue
            if path.is_file() and (path.suffix in {".py", ".toml", ".service", ".md"}):
                archive.add(path, arcname=path.name, recursive=False)
    return buffer.getvalue()


# --- orchestration -----------------------------------------------------------

def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, "<redacted>")
    out = out.strip().replace("\n", " ")
    return out[-MAX_DETAIL_CHARS:]


async def install_runner(
    *,
    server_name: str,
    ssh_run: Callable[[str, str, float], Awaitable[Any]],
    ssh_write_file: Callable[[str, str, str], Awaitable[Any]],
    ssh_put_file: Callable[[str, str, str], Awaitable[Any]],
    server_url: str,
    runner_name: str,
    workspace_root: str,
    credential: str,
    claude_token: Optional[str] = None,
    package_dir: Path = PACKAGE_DIR,
) -> InstallOutcome:
    """Provision and launch the runner on ``server_name``. Never raises."""

    secrets = (credential, claude_token or "")
    try:
        config_json = render_config_json(server_url=server_url, runner_name=runner_name, workspace_root=workspace_root)
        agent_env = render_agent_env(credential)
        claude_env = render_claude_env(claude_token) if claude_token else None
    except InvalidRunnerInstallSpecError as exc:
        return InstallOutcome(False, "config", str(exc))

    async def run(step: str, command: str, timeout: float, marker: str) -> Optional[InstallOutcome]:
        try:
            result = await ssh_run(server_name, command, timeout)
        except SSHUnreachableError:
            return InstallOutcome(False, step, "SSH 連線失敗（主機不可達或身分未信任）")
        except Exception as exc:  # noqa: BLE001 - never leak transport internals
            return InstallOutcome(False, step, _scrub(type(exc).__name__, secrets))
        stdout = str(getattr(result, "stdout", "") or "")
        if getattr(result, "exit_status", None) != 0 or marker not in stdout:
            stderr = str(getattr(result, "stderr", "") or "")
            return InstallOutcome(False, step, _scrub(stderr or stdout or f"exit {getattr(result, 'exit_status', None)}", secrets))
        return None

    try:
        home_result = await ssh_run(server_name, build_home_command(), HOME_TIMEOUT_SEC)
        home = parse_home(str(getattr(home_result, "stdout", "") or ""))
    except SSHUnreachableError:
        return InstallOutcome(False, "home", "SSH 連線失敗（主機不可達或身分未信任）")
    except Exception as exc:  # noqa: BLE001
        return InstallOutcome(False, "home", _scrub(str(exc), secrets))
    paths = remote_paths(home)

    failure = await run("home", build_prepare_command(home), HOME_TIMEOUT_SEC, "")
    if failure:
        return failure

    try:
        archive = build_source_archive(package_dir)
    except Exception as exc:  # noqa: BLE001
        return InstallOutcome(False, "archive", _scrub(str(exc), secrets))
    try:
        with tempfile.NamedTemporaryFile(prefix="dispatch_agent_src_", suffix=".tar.gz", delete=True) as handle:
            handle.write(archive)
            handle.flush()
            await ssh_put_file(server_name, handle.name, paths["archive"])
    except SSHUnreachableError:
        return InstallOutcome(False, "upload", "SFTP 上傳失敗")
    except Exception as exc:  # noqa: BLE001
        return InstallOutcome(False, "upload", _scrub(type(exc).__name__, secrets))

    try:
        await ssh_write_file(server_name, paths["config_json"], config_json)
        await ssh_write_file(server_name, paths["agent_env"], agent_env)
        if claude_env:
            await ssh_write_file(server_name, paths["claude_env"], claude_env)
    except SSHUnreachableError:
        return InstallOutcome(False, "config", "SFTP 寫入設定失敗")
    except Exception as exc:  # noqa: BLE001
        return InstallOutcome(False, "config", _scrub(type(exc).__name__, secrets))
    finally:
        del agent_env, claude_env
    failure = await run("config", build_secure_files_command(home, with_claude_env=bool(claude_token)), HOME_TIMEOUT_SEC, "")
    if failure:
        return failure

    for step, command, timeout, marker in (
        ("install", build_install_command(home), INSTALL_TIMEOUT_SEC, "RUNNER_INSTALLED"),
        ("check", build_check_command(home), CHECK_TIMEOUT_SEC, "RUNNER_CHECK_OK"),
        ("launch", build_launch_command(home), LAUNCH_TIMEOUT_SEC, "RUNNER_LAUNCHED"),
    ):
        failure = await run(step, command, timeout, marker)
        if failure:
            return failure
    return InstallOutcome(True, "launched", "runner 已安裝並在 tmux 中啟動")


async def relaunch_runner(
    *,
    server_name: str,
    ssh_run: Callable[[str, str, float], Awaitable[Any]],
) -> InstallOutcome:
    """Keepalive: idempotently (re)start the tmux session. Never raises."""

    try:
        home_result = await ssh_run(server_name, build_home_command(), HOME_TIMEOUT_SEC)
        home = parse_home(str(getattr(home_result, "stdout", "") or ""))
        result = await ssh_run(server_name, build_launch_command(home), LAUNCH_TIMEOUT_SEC)
    except SSHUnreachableError:
        return InstallOutcome(False, "launch", "SSH 連線失敗（主機不可達或身分未信任）")
    except Exception as exc:  # noqa: BLE001
        return InstallOutcome(False, "launch", _scrub(type(exc).__name__, ()))
    stdout = str(getattr(result, "stdout", "") or "")
    if getattr(result, "exit_status", None) == 0 and "RUNNER_LAUNCHED" in stdout:
        return InstallOutcome(True, "launch", "tmux session 已就緒")
    return InstallOutcome(False, "launch", _scrub(str(getattr(result, "stderr", "") or stdout), ()))


async def set_claude_token(
    *,
    server_name: str,
    ssh_run: Callable[[str, str, float], Awaitable[Any]],
    ssh_write_file: Callable[[str, str, str], Awaitable[Any]],
    claude_token: str,
) -> InstallOutcome:
    """Write ``claude.env`` (0600) and restart the managed tmux session."""

    try:
        content = render_claude_env(claude_token)
    except InvalidRunnerInstallSpecError as exc:
        return InstallOutcome(False, "config", str(exc))
    try:
        home_result = await ssh_run(server_name, build_home_command(), HOME_TIMEOUT_SEC)
        home = parse_home(str(getattr(home_result, "stdout", "") or ""))
        paths = remote_paths(home)
        prepare = await ssh_run(server_name, build_prepare_command(home), HOME_TIMEOUT_SEC)
        if getattr(prepare, "exit_status", None) != 0:
            return InstallOutcome(False, "config", "無法建立設定目錄")
        await ssh_write_file(server_name, paths["claude_env"], content)
        secure = await ssh_run(server_name, "chmod 600 " + shlex.quote(paths["claude_env"]), HOME_TIMEOUT_SEC)
        if getattr(secure, "exit_status", None) != 0:
            return InstallOutcome(False, "config", "無法設定檔案權限")
        result = await ssh_run(server_name, build_restart_command(home), LAUNCH_TIMEOUT_SEC)
    except SSHUnreachableError:
        return InstallOutcome(False, "config", "SSH 連線失敗（主機不可達或身分未信任）")
    except Exception as exc:  # noqa: BLE001
        return InstallOutcome(False, "config", _scrub(type(exc).__name__, (claude_token,)))
    finally:
        del content
    stdout = str(getattr(result, "stdout", "") or "")
    if getattr(result, "exit_status", None) == 0 and "RUNNER_LAUNCHED" in stdout:
        return InstallOutcome(True, "launch", "Claude token 已寫入，runner 已重新啟動")
    return InstallOutcome(False, "launch", _scrub(str(getattr(result, "stderr", "") or stdout), (claude_token,)))


__all__ = [
    "DEFAULT_WORKSPACE_ROOT",
    "InstallOutcome",
    "InvalidRunnerInstallSpecError",
    "LAUNCH_MODE",
    "TMUX_SESSION",
    "build_check_command",
    "build_home_command",
    "build_install_command",
    "build_launch_command",
    "build_prepare_command",
    "build_restart_command",
    "build_secure_files_command",
    "build_source_archive",
    "install_runner",
    "is_managed_install",
    "parse_home",
    "relaunch_runner",
    "remote_paths",
    "render_agent_env",
    "render_claude_env",
    "render_config_json",
    "set_claude_token",
    "validate_claude_token",
    "validate_install_spec",
    "validate_server_url",
]
