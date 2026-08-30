"""INV-AGENT-2 decision matrix for SDK tool use (pure, no I/O).

* Workspace file tools: allowed when every referenced path resolves inside the
  session workspace; denied otherwise.
* ``Bash``: allow-listed validation commands (simple, no shell control
  operators) run directly; commands the owner already granted for this session
  run directly; everything else is **asked** — the owner decides in the browser.
* ``mcp__dispatch__*``: allowed (the platform gates them server-side).
* Anything else (WebFetch, WebSearch, Task/Agent, unknown): asked.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

Action = Literal["allow", "deny", "ask"]

WORKSPACE_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "LS", "NotebookEdit", "NotebookRead"})
_PATH_KEYS = ("file_path", "path", "notebook_path", "directory")
_SHELL_CONTROL_RE = re.compile(r"[;&|`$<>\n]|\(\)")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
MCP_TOOL_PREFIX = "mcp__dispatch__"


@dataclass(frozen=True)
class PermissionDecision:
    action: Action
    reason: str
    summary: str = ""


def _inside(path_text: str, workspace: Path) -> bool:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = Path(os.path.realpath(candidate))
    root = Path(os.path.realpath(workspace))
    return resolved == root or root in resolved.parents


def _command_tokens(command: str) -> Optional[list[str]]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    while tokens and _ENV_ASSIGNMENT_RE.match(tokens[0]):
        tokens = tokens[1:]
    return tokens or None


def command_matches(entry: str, command: str) -> bool:
    """Does ``command`` fall under allow-list ``entry``?

    ``entry`` is a plain command (``"pytest"``, ``"make test"``) matched as a
    token prefix, optionally ending in ``:*`` (Claude-Code rule style, same
    meaning).  A single-token entry also matches by basename (``/usr/bin/pytest``).
    Commands with shell control operators never match: ``pytest; rm -rf /``
    must be asked, not auto-allowed."""

    if _SHELL_CONTROL_RE.search(command):
        return False
    tokens = _command_tokens(command)
    if not tokens:
        return False
    entry_text = entry[:-2] if entry.endswith(":*") else entry
    entry_tokens = _command_tokens(entry_text)
    if not entry_tokens:
        return False
    if len(entry_tokens) == 1:
        return os.path.basename(tokens[0]) == os.path.basename(entry_tokens[0])
    if len(tokens) < len(entry_tokens):
        return False
    head = [os.path.basename(tokens[0])] + tokens[1 : len(entry_tokens)]
    entry_head = [os.path.basename(entry_tokens[0])] + entry_tokens[1:]
    return head == entry_head


def summarize_command(command: str, limit: int = 200) -> str:
    text = " ".join(command.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def decide_tool_use(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    workspace: Path,
    validation_allowlist: Iterable[str],
    session_allow_patterns: Iterable[str] = (),
) -> PermissionDecision:
    """Return allow / deny / ask for one SDK tool call (INV-AGENT-2)."""

    if tool_name.startswith(MCP_TOOL_PREFIX):
        return PermissionDecision("allow", "platform tool; authorised server-side", tool_name)

    if tool_name in WORKSPACE_FILE_TOOLS:
        for key in _PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value and not _inside(value, workspace):
                return PermissionDecision("deny", f"{key} is outside the session workspace", f"{tool_name} {value}")
        return PermissionDecision("allow", "workspace file tool", tool_name)

    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return PermissionDecision("deny", "empty command", "Bash")
        summary = summarize_command(command)
        for entry in validation_allowlist:
            if command_matches(entry, command):
                return PermissionDecision("allow", f"validation allowlist: {entry}", summary)
        for pattern in session_allow_patterns:
            if command_matches(pattern, command):
                return PermissionDecision("allow", f"granted for this session: {pattern}", summary)
        return PermissionDecision("ask", "command outside the validation allowlist", summary)

    return PermissionDecision("ask", f"{tool_name} requires the owner's decision", tool_name)


def allow_pattern_for(command: str) -> Optional[str]:
    """Pattern the owner can grant for the rest of the session: first two
    tokens as a prefix rule (``pip install:*``), or the bare program."""

    if _SHELL_CONTROL_RE.search(command):
        return None
    tokens = _command_tokens(command)
    if not tokens:
        return None
    program = os.path.basename(tokens[0])
    if len(tokens) >= 2 and not tokens[1].startswith("-"):
        return f"{program} {tokens[1]}:*"
    return f"{program}:*"


__all__ = [
    "MCP_TOOL_PREFIX",
    "PermissionDecision",
    "WORKSPACE_FILE_TOOLS",
    "allow_pattern_for",
    "command_matches",
    "decide_tool_use",
    "summarize_command",
]
