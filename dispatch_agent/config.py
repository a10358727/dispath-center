"""Runner configuration (files under ``~/.config/dispatch-agent/``).

* ``config.json`` — non-secret settings.
* ``agent.env`` — ``DISPATCH_AGENT_CREDENTIAL=dar_<uuid>.<secret>`` (0600).
* ``claude.env`` — ``CLAUDE_CODE_OAUTH_TOKEN=…`` from ``claude setup-token`` (0600).

Secrets are read into memory only.  They are never exported into the process
environment: the Claude Agent SDK spawns its engine with the *inherited*
environment plus ``options.env``, so anything in ``os.environ`` would reach the
model's shell (INV-AGENT-1).  ``scrub_environment()`` enforces that at startup.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

CONFIG_FILE = "config.json"
AGENT_ENV_FILE = "agent.env"
CLAUDE_ENV_FILE = "claude.env"
CREDENTIAL_ENV_KEY = "DISPATCH_AGENT_CREDENTIAL"
CLAUDE_TOKEN_ENV_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
CREDENTIAL_PREFIX = "dar_"

_RUNNER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_CREDENTIAL_RE = re.compile(r"^dar_[0-9a-f-]{36}\.[A-Za-z0-9_-]{16,256}\Z")
_ALLOWLIST_ENTRY_RE = re.compile(r"^[A-Za-z0-9._/@-][A-Za-z0-9._/@ =:*-]{0,199}\Z")
DEFAULT_VALIDATION_ALLOWLIST = ("pytest", "ruff", "mypy", "python -m pytest", "make test", "npm test", "git status", "git diff", "git log")


class ConfigError(ValueError):
    """Configuration is missing, malformed, or unsafe (fail closed)."""


@dataclass(frozen=True)
class AgentConfig:
    server_url: str
    runner_name: str
    workspace_root: Path
    credential: str = field(repr=False)
    claude_oauth_token: Optional[str] = field(default=None, repr=False)
    validation_allowlist: tuple[str, ...] = DEFAULT_VALIDATION_ALLOWLIST
    max_turns: int = 50
    max_budget_usd: Optional[float] = None
    heartbeat_sec: float = 15.0
    permission_timeout_sec: float = 300.0
    model: Optional[str] = None
    runner_python: str = "python3"
    mcp_bridge_path: Optional[Path] = None

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.server_url.startswith("https://") else "ws"
        return f"{scheme}://{self.server_url.split('://', 1)[1].rstrip('/')}/agent-runner/ws"


def _parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _require_private_file(path: Path, *, stat_fn: Callable[[Path], os.stat_result]) -> None:
    try:
        info = stat_fn(path)
    except FileNotFoundError:
        raise ConfigError(f"{path.name} missing") from None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path.name} must be mode 0600 (group/other bits set)")


def load_config(
    config_dir: Path,
    *,
    home: Optional[Path] = None,
    euid: Optional[int] = None,
    read_text: Callable[[Path], str] = lambda p: p.read_text(encoding="utf-8"),
    stat_fn: Callable[[Path], os.stat_result] = lambda p: p.stat(),
) -> AgentConfig:
    """Load and validate the runner configuration; every failure is a ``ConfigError``."""

    if (os.geteuid() if euid is None else euid) == 0:
        raise ConfigError("dispatch-agent refuses to run as root (INV-AGENT-1)")
    home = Path(home or Path.home()).resolve()
    try:
        raw: Any = json.loads(read_text(config_dir / CONFIG_FILE))
    except FileNotFoundError:
        raise ConfigError(f"{CONFIG_FILE} missing in {config_dir}") from None
    except ValueError as exc:
        raise ConfigError(f"{CONFIG_FILE} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_FILE} must be a JSON object")

    server_url = str(raw.get("server_url") or "").strip().rstrip("/")
    parts = urlsplit(server_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("server_url must be an http(s) URL with a host and no credentials/query")
    runner_name = str(raw.get("runner_name") or "").strip()
    if not _RUNNER_NAME_RE.match(runner_name):
        raise ConfigError("runner_name must match the servers.yaml name shape")
    workspace_root = Path(os.path.expanduser(str(raw.get("workspace_root") or "~/dispatch_workspaces"))).resolve()
    if home not in workspace_root.parents and workspace_root != home:
        raise ConfigError("workspace_root must be inside the runner user's home")
    if workspace_root == home:
        raise ConfigError("workspace_root must not be the home directory itself")
    allowlist_raw = raw.get("validation_allowlist", list(DEFAULT_VALIDATION_ALLOWLIST))
    if not isinstance(allowlist_raw, list) or not all(isinstance(x, str) and _ALLOWLIST_ENTRY_RE.match(x) for x in allowlist_raw):
        raise ConfigError("validation_allowlist must be a list of simple command strings")
    max_turns = raw.get("max_turns", 50)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or not 1 <= max_turns <= 500:
        raise ConfigError("max_turns must be an integer between 1 and 500")
    max_budget = raw.get("max_budget_usd")
    if max_budget is not None and (isinstance(max_budget, bool) or not isinstance(max_budget, (int, float)) or max_budget <= 0):
        raise ConfigError("max_budget_usd must be a positive number or null")
    heartbeat = float(raw.get("heartbeat_sec", 15.0))
    if not 5.0 <= heartbeat <= 300.0:
        raise ConfigError("heartbeat_sec must be between 5 and 300")
    permission_timeout = float(raw.get("permission_timeout_sec", 300.0))
    if not 30.0 <= permission_timeout <= 3600.0:
        raise ConfigError("permission_timeout_sec must be between 30 and 3600")
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not re.match(r"^[A-Za-z0-9._-]{1,64}\Z", model)):
        raise ConfigError("model must be a short model identifier or null")
    runner_python = str(raw.get("runner_python") or "python3")
    if not re.match(r"^[A-Za-z0-9._/-]{1,128}\Z", runner_python):
        raise ConfigError("runner_python has an invalid shape")
    bridge = raw.get("mcp_bridge_path")
    mcp_bridge_path = Path(os.path.expanduser(str(bridge))).resolve() if bridge else None

    agent_env_path = config_dir / AGENT_ENV_FILE
    _require_private_file(agent_env_path, stat_fn=stat_fn)
    credential = _parse_env_file(read_text(agent_env_path)).get(CREDENTIAL_ENV_KEY, "")
    if not _CREDENTIAL_RE.match(credential):
        raise ConfigError(f"{AGENT_ENV_FILE} must define a valid {CREDENTIAL_ENV_KEY}")
    claude_token: Optional[str] = None
    claude_env_path = config_dir / CLAUDE_ENV_FILE
    try:
        _require_private_file(claude_env_path, stat_fn=stat_fn)
    except ConfigError as exc:
        if "missing" not in str(exc):
            raise
    else:
        claude_token = _parse_env_file(read_text(claude_env_path)).get(CLAUDE_TOKEN_ENV_KEY) or None

    return AgentConfig(
        server_url=server_url,
        runner_name=runner_name,
        workspace_root=workspace_root,
        credential=credential,
        claude_oauth_token=claude_token,
        validation_allowlist=tuple(allowlist_raw),
        max_turns=max_turns,
        max_budget_usd=float(max_budget) if max_budget is not None else None,
        heartbeat_sec=heartbeat,
        permission_timeout_sec=permission_timeout,
        model=model,
        runner_python=runner_python,
        mcp_bridge_path=mcp_bridge_path,
    )


SCRUBBED_ENV_PREFIXES = ("DISPATCH_AGENT_", "DISPATCH_NODE_", "AUTH_TOKEN", "ANTHROPIC_API_KEY")


def scrub_environment(environ: Optional[Mapping[str, str]] = None) -> list[str]:
    """Remove platform/runner secrets from the process environment.

    The SDK engine inherits ``os.environ`` (INV-AGENT-1), so nothing the model
    must not see may live there.  Returns the removed keys for logging."""

    target = os.environ if environ is None else environ
    removed = [key for key in list(target) if key.startswith(SCRUBBED_ENV_PREFIXES) or key == "CLAUDECODE"]
    for key in removed:
        try:
            del target[key]  # type: ignore[attr-defined]
        except (KeyError, TypeError):
            pass
    return removed


def sdk_environment(config: AgentConfig, *, base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Explicit variables passed to the SDK engine via ``options.env``.

    Only what the engine needs: locale, the Claude subscription token, and a
    marker so the runner can recognise its own engine processes."""

    env: dict[str, str] = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "DISPATCH_RUNNER": config.runner_name}
    if config.claude_oauth_token:
        env[CLAUDE_TOKEN_ENV_KEY] = config.claude_oauth_token
    source = os.environ if base is None else base
    for key in ("HOME", "PATH", "USER", "TERM"):
        if key in source:
            env[key] = source[key]
    return env


__all__ = [
    "AGENT_ENV_FILE",
    "AgentConfig",
    "CLAUDE_ENV_FILE",
    "CLAUDE_TOKEN_ENV_KEY",
    "CONFIG_FILE",
    "CREDENTIAL_ENV_KEY",
    "CREDENTIAL_PREFIX",
    "ConfigError",
    "DEFAULT_VALIDATION_ALLOWLIST",
    "load_config",
    "scrub_environment",
    "sdk_environment",
]
