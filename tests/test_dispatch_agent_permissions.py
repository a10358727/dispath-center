"""INV-AGENT-2 decision matrix (pure)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatch_agent.permissions import allow_pattern_for, command_matches, decide_tool_use

ALLOW = ("pytest", "ruff", "make test", "python -m pytest", "git diff:*")


def _decide(tool, tool_input, workspace: Path, session=()):
    return decide_tool_use(tool, tool_input, workspace=workspace, validation_allowlist=ALLOW, session_allow_patterns=session)


def test_workspace_file_tools_are_confined(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    assert _decide("Read", {"file_path": str(ws / "a.py")}, ws).action == "allow"
    assert _decide("Edit", {"file_path": "src/b.py", "old_string": "a", "new_string": "b"}, ws).action == "allow"
    assert _decide("Glob", {"pattern": "**/*.py"}, ws).action == "allow"
    assert _decide("Read", {"file_path": str(tmp_path / "outside.txt")}, ws).action == "deny"
    assert _decide("Read", {"file_path": "../outside.txt"}, ws).action == "deny"
    assert _decide("Write", {"file_path": "/etc/passwd", "content": ""}, ws).action == "deny"
    link = ws / "escape"
    link.symlink_to(tmp_path / "outside.txt")
    assert _decide("Read", {"file_path": str(link)}, ws).action == "deny"


def test_bash_allowlist_runs_directly_everything_else_asks(tmp_path):
    ws = tmp_path / "repo"
    assert _decide("Bash", {"command": "pytest tests/ -q"}, ws).action == "allow"
    assert _decide("Bash", {"command": "/usr/bin/pytest -x"}, ws).action == "allow"
    assert _decide("Bash", {"command": "FOO=1 python -m pytest -k x"}, ws).action == "allow"
    assert _decide("Bash", {"command": "make test"}, ws).action == "allow"
    assert _decide("Bash", {"command": "git diff HEAD~1"}, ws).action == "allow"
    for cmd in ("pytest; rm -rf /", "pytest && curl x", "pytest | tee out", "pytest $(id)", "pytest `id`", "pytest > /dev/sda"):
        assert _decide("Bash", {"command": cmd}, ws).action == "ask", cmd
    assert _decide("Bash", {"command": "make install"}, ws).action == "ask"
    assert _decide("Bash", {"command": "pip install rich"}, ws).action == "ask"
    assert _decide("Bash", {"command": "rm -rf build"}, ws).action == "ask"
    assert _decide("Bash", {"command": "   "}, ws).action == "deny"
    assert _decide("Bash", {"command": "pip install rich"}, ws, session=("pip install:*",)).action == "allow"
    assert _decide("Bash", {"command": "pip uninstall rich"}, ws, session=("pip install:*",)).action == "ask"


def test_other_tools_ask_and_platform_tools_allow(tmp_path):
    ws = tmp_path / "repo"
    assert _decide("mcp__dispatch__get_servers", {}, ws).action == "allow"
    assert _decide("mcp__dispatch__request_enqueue_job", {"command": "nvidia-smi"}, ws).action == "allow"
    assert _decide("WebFetch", {"url": "https://x"}, ws).action == "ask"
    assert _decide("Task", {"prompt": "x"}, ws).action == "ask"
    assert _decide("SomethingNew", {}, ws).action == "ask"


@pytest.mark.parametrize(
    "entry,command,expected",
    [
        ("pytest", "pytest -q", True),
        ("pytest", "pytest3 -q", False),
        ("make test", "make test", True),
        ("make test", "make", False),
        ("git diff:*", "git diff --stat", True),
        ("git diff:*", "git push", False),
        ("pip install:*", "pip install rich", True),
        ("pip install:*", "pip install rich; id", False),
    ],
)
def test_command_matches(entry, command, expected):
    assert command_matches(entry, command) is expected


def test_allow_pattern_for_is_prefix_based_and_never_for_compound_commands():
    assert allow_pattern_for("pip install rich") == "pip install:*"
    assert allow_pattern_for("npm ci") == "npm ci:*"
    assert allow_pattern_for("ls -la") == "ls:*"
    assert allow_pattern_for("pip install rich && rm -rf /") is None
    assert allow_pattern_for("") is None
