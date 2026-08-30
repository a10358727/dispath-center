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
from pathlib import Path
from typing import Callable, Optional

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
