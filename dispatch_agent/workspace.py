"""Local worktree management on the runner (no shell; argv lists only).

Mirrors ``app.agent_session_turns``'s reviewed shapes: a bare mirror per
project under ``<root>/mirrors/<project>.git`` and one worktree per session
under ``<root>/sessions/<session_id>/repo`` on branch ``session/<id8>``.
The model never chooses paths (INV-AGENT-1); every identifier is validated.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SESSION_RE = re.compile(r"^[0-9a-f-]{8,36}\Z")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}\Z")
GIT_TIMEOUT_SEC = 300.0


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionPaths:
    mirror: Path
    session_dir: Path
    repo: Path
    branch: str


def session_paths(root: Path, project: str, session_id: str) -> SessionPaths:
    if not _PROJECT_RE.match(project):
        raise WorkspaceError("invalid project name")
    if not _SESSION_RE.match(session_id):
        raise WorkspaceError("invalid session id")
    session_dir = root / "sessions" / session_id
    return SessionPaths(
        mirror=root / "mirrors" / f"{project}.git",
        session_dir=session_dir,
        repo=session_dir / "repo",
        branch=f"session/{session_id.replace('-', '')[:8]}",
    )


def mirror_clone_argv(source: str, mirror: Path) -> list[str]:
    if source.startswith("-"):
        raise WorkspaceError("source must not look like a flag")
    return ["git", "clone", "--mirror", "--", source, str(mirror)]


def mirror_update_argv(mirror: Path) -> list[str]:
    return ["git", f"--git-dir={mirror}", "remote", "update", "--prune"]


def bundle_fetch_argv(mirror: Path, bundle: Path) -> list[str]:
    return ["git", f"--git-dir={mirror}", "fetch", "--", str(bundle), "+refs/heads/*:refs/heads/*"]


def commit_exists_argv(mirror: Path, commit: str) -> list[str]:
    if not _COMMIT_RE.match(commit):
        raise WorkspaceError("invalid commit")
    return ["git", f"--git-dir={mirror}", "cat-file", "-e", f"{commit}^{{commit}}"]


def worktree_add_argv(paths: SessionPaths, commit: str) -> list[str]:
    if not _COMMIT_RE.match(commit):
        raise WorkspaceError("invalid commit")
    return ["git", f"--git-dir={paths.mirror}", "worktree", "add", "-b", paths.branch, "--", str(paths.repo), commit]


def diff_argv(repo: Path) -> list[str]:
    return ["git", "-C", str(repo), "diff", "--no-color", "--no-ext-diff", "--", "."]


def status_argv(repo: Path) -> list[str]:
    return ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all", "--", "."]


def head_argv(repo: Path) -> list[str]:
    return ["git", "-C", str(repo), "rev-parse", "HEAD"]


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run_git(argv: list[str], *, timeout: float = GIT_TIMEOUT_SEC) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603 - argv list, no shell


def ensure_workspace(
    *,
    root: Path,
    project: str,
    session_id: str,
    base_commit: str,
    source: Optional[str],
    bundle: Optional[Path],
    run: Optional[Runner] = None,
) -> SessionPaths:
    """Create (or reuse) the session worktree at ``base_commit``.

    ``source`` is a runner-local repository path (projects imported from this
    machine); ``bundle`` is a bundle Server A delivered.  At least one must
    provide the commit."""

    run = run or run_git  # resolved at call time so tests can substitute the module attribute
    paths = session_paths(root, project, session_id)
    paths.mirror.parent.mkdir(parents=True, exist_ok=True)
    paths.session_dir.mkdir(parents=True, exist_ok=True)
    if paths.repo.exists():
        return paths
    if not paths.mirror.exists():
        if source:
            _check(run(mirror_clone_argv(source, paths.mirror)), "mirror clone failed")
        else:
            _check(run(["git", "init", "--bare", "--", str(paths.mirror)]), "mirror init failed")
    elif source:
        _check(run(mirror_update_argv(paths.mirror)), "mirror update failed")
    if bundle is not None:
        _check(run(bundle_fetch_argv(paths.mirror, bundle)), "bundle fetch failed")
    _check(run(commit_exists_argv(paths.mirror, base_commit)), "base commit not in mirror")
    _check(run(worktree_add_argv(paths, base_commit)), "worktree add failed")
    return paths


def _check(result: "subprocess.CompletedProcess[str]", message: str) -> None:
    if result.returncode != 0:
        raise WorkspaceError(f"{message}: {(result.stderr or '').strip()[:500]}")


MAX_DIFF_BYTES = 256 * 1024


def collect_diff(repo: Path, *, run: Optional[Runner] = None) -> dict[str, object]:
    run = run or run_git
    diff = run(diff_argv(repo))
    status = run(status_argv(repo))
    head = run(head_argv(repo))
    patch = diff.stdout or ""
    truncated = len(patch.encode("utf-8")) > MAX_DIFF_BYTES
    if truncated:
        patch = patch.encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
    return {
        "patch": patch,
        "truncated": truncated,
        "status": [line for line in (status.stdout or "").splitlines() if line.strip()],
        "head": (head.stdout or "").strip(),
        "ok": diff.returncode == 0 and status.returncode == 0,
    }


__all__ = [
    "GIT_TIMEOUT_SEC",
    "MAX_DIFF_BYTES",
    "SessionPaths",
    "WorkspaceError",
    "bundle_fetch_argv",
    "collect_diff",
    "commit_exists_argv",
    "diff_argv",
    "ensure_workspace",
    "head_argv",
    "mirror_clone_argv",
    "mirror_update_argv",
    "run_git",
    "session_paths",
    "status_argv",
    "worktree_add_argv",
]


# --------------------------------------------------------------------------
# DG-STUDIO-UI v1 Phase 2 (P2-2): project context for the SDK session.
#
# CLAUDE.md is appended to the claude_code system-prompt preset, and the
# repo's `.claude/skills` / `.claude/commands` are copied into a generated
# per-session *plugin* directory (outside the repo) so the SDK loads them
# without `setting_sources` — which means the repo's `settings.json`,
# `hooks/`, `agents/` and `.mcp.json` are **never** loaded (INV-AGENT-2:
# repo content must not execute anything outside the permission prompts).
# Two extra sanitations for the same reason: `allowed-tools`/`hooks` keys are
# stripped from copied frontmatter (they could pre-authorize tools past
# `can_use_tool`) and `` !`…` `` bash-preprocessing lines are removed from
# command files (the CLI would execute them at expansion time).
# --------------------------------------------------------------------------

MAX_INSTRUCTIONS_BYTES = 65536
MAX_PLUGIN_FILE_BYTES = 262144
MAX_PLUGIN_TOTAL_BYTES = 1048576
MAX_PLUGIN_FILES = 64
_FRONTMATTER_DROP_KEYS = ("allowed-tools", "allowed_tools", "hooks")


def read_project_instructions(repo: Path) -> str:
    """Bounded CLAUDE.md content (root, then `.claude/CLAUDE.md`)."""

    parts: list[str] = []
    budget = MAX_INSTRUCTIONS_BYTES
    for candidate in (repo / "CLAUDE.md", repo / ".claude" / "CLAUDE.md"):
        if budget <= 0 or not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            raw = candidate.read_bytes()[:budget]
        except OSError:
            continue
        text = raw.decode("utf-8", errors="replace").strip()
        if text:
            parts.append(f"# Project instructions ({candidate.relative_to(repo)})\n\n{text}")
            budget -= len(raw)
    return "\n\n".join(parts)


def _sanitize_markdown(text: str, *, strip_bash_lines: bool) -> str:
    lines = text.splitlines()
    out: list[str] = []
    in_frontmatter = False
    dropping_key = False
    for index, line in enumerate(lines):
        if index == 0 and line.strip() == "---":
            in_frontmatter = True
            out.append(line)
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
                dropping_key = False
                out.append(line)
                continue
            key = line.split(":", 1)[0].strip().lower() if ":" in line else None
            if line[:1] not in (" ", "\t"):
                dropping_key = key in _FRONTMATTER_DROP_KEYS
            if dropping_key:
                continue
            out.append(line)
            continue
        if strip_bash_lines and line.lstrip().startswith("!`"):
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def build_workspace_plugin(repo: Path, session_dir: Path) -> Optional[Path]:
    """Copy the repo's skills/commands into a sanitized plugin dir; None if empty."""

    skills_src = repo / ".claude" / "skills"
    commands_src = repo / ".claude" / "commands"
    plugin_dir = session_dir / "workspace-plugin"
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    copied = 0
    total = 0

    def _copy_file(source: Path, target: Path, *, sanitize: bool, strip_bash: bool) -> None:
        nonlocal copied, total
        if copied >= MAX_PLUGIN_FILES or source.is_symlink() or not source.is_file():
            return
        try:
            raw = source.read_bytes()
        except OSError:
            return
        if len(raw) > MAX_PLUGIN_FILE_BYTES or total + len(raw) > MAX_PLUGIN_TOTAL_BYTES:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if sanitize:
            target.write_text(_sanitize_markdown(raw.decode("utf-8", errors="replace"), strip_bash_lines=strip_bash), encoding="utf-8")
        else:
            target.write_bytes(raw)
        copied += 1
        total += len(raw)

    if skills_src.is_dir() and not skills_src.is_symlink():
        for skill_dir in sorted(path for path in skills_src.iterdir() if path.is_dir() and not path.is_symlink()):
            for source in sorted(path for path in skill_dir.rglob("*") if path.is_file()):
                relative = source.relative_to(skill_dir)
                _copy_file(
                    source,
                    plugin_dir / "skills" / skill_dir.name / relative,
                    sanitize=source.name == "SKILL.md",
                    strip_bash=False,
                )
    if commands_src.is_dir() and not commands_src.is_symlink():
        for source in sorted(commands_src.glob("*.md")):
            _copy_file(source, plugin_dir / "commands" / source.name, sanitize=True, strip_bash=True)
    if copied == 0:
        return None
    manifest_dir = plugin_dir / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {"name": "workspace", "description": "repo skills/commands (hooks and tool pre-authorizations stripped)", "version": "0.0.0"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plugin_dir


def build_session_context(repo: Path, session_dir: Path) -> dict[str, Any]:
    """Everything the SDK options need from the workspace (pure filesystem)."""

    context: dict[str, Any] = {}
    instructions = read_project_instructions(repo)
    if instructions:
        context["system_prompt_append"] = instructions
    plugin_dir = build_workspace_plugin(repo, session_dir)
    if plugin_dir is not None:
        context["plugin_dir"] = str(plugin_dir)
    return context


# --------------------------------------------------------------------------
# P2-3: attachments and @file mentions.
# --------------------------------------------------------------------------

MAX_MENTION_FILES = 8
MAX_MENTION_BYTES = 262144
MAX_LISTED_FILES = 2000
_MENTION_RE = re.compile(r"@([A-Za-z0-9_./-]{1,200})")


def list_workspace_files(repo: Path, *, run: Optional[Runner] = None) -> list[str]:
    """Tracked + untracked-but-not-ignored files, capped, for @-autocomplete."""

    run = run or run_git
    result = run(["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard"])
    files = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return files[:MAX_LISTED_FILES]


def expand_file_mentions(repo: Path, text: str) -> list[str]:
    """Return `<file>` context blocks for `@relative/path` mentions.

    Confined to the workspace by realpath (INV-AGENT-2's file confinement),
    bounded in count and bytes; anything unresolvable is silently skipped —
    the mention still reaches the model as plain text."""

    blocks: list[str] = []
    seen: set[str] = set()
    repo_real = repo.resolve()
    for mention in _MENTION_RE.findall(text):
        if len(blocks) >= MAX_MENTION_FILES:
            break
        cleaned = mention.rstrip(".,;:!?")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        candidate = (repo / cleaned)
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not str(resolved).startswith(str(repo_real) + "/") or not resolved.is_file():
            continue
        try:
            raw = resolved.read_bytes()
        except OSError:
            continue
        clipped = raw[:MAX_MENTION_BYTES]
        suffix = "\n… (truncated)" if len(raw) > len(clipped) else ""
        blocks.append(f'<file path="{cleaned}">\n{clipped.decode("utf-8", errors="replace")}{suffix}\n</file>')
    return blocks
