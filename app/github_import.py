"""GitHub-only project import (DG-PROJECT-GITHUB-IMPORT-v1, V0.2 WP3).

The user's rule: a project can only be added from a GitHub repository (an
existing one, or a new empty one the user creates on GitHub with a README),
and the repository must carry a non-empty ``README.md``.

This module owns the closed request contract and the pure command builders.
The approval flow (``kind=project_github_import``) lives in
``app.approvals.approve()`` next to ``project_deploy`` and follows the same
durable intent → remote effects → outcome pattern:

1. request time: validate the GitHub URL (G-1), the project name, the target
   server and the destination path (same rules as ``project_deploy``); create
   the approval card only — nothing is cloned yet;
2. approve time: on the target worker ``mkdir -p`` the destination parent,
   ``git clone`` into a staging directory that only this approval creates
   (``<dest>.dispatch-import-<approval_id>``), check ``README.md`` is present
   and non-empty, move the staging directory to ``dest_path``, record HEAD,
   then commit project + instance + version + outcome in one transaction.
   A missing README rejects the card and removes only that staging directory
   (G-2); any other failure rejects and leaves the site for manual inspection.

Server A never holds or forwards GitHub credentials (G-4): only public
repositories or workers with their own deploy key / credential helper work.
``DG-GITHUB-PUBLISH`` (real GitHub publication adapter) stays a reserved gate.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Optional
from urllib.parse import urlsplit

from app.audit import append_audit, audit_actor_from_request_context
from app.datasets import InvalidNameError, validate_name_component
from app.db import Approval, Database
from app.identity import RequestContext
from app.inventory import is_forbidden_root

KIND = "project_github_import"
STAGING_SUFFIX = ".dispatch-import-"

#: G-1: only canonical HTTPS GitHub repository URLs. No userinfo, no query,
#: no fragment, no nested paths; an optional ``.git`` suffix / trailing slash.
_GITHUB_HTTPS_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$"
)
#: Remotes recorded by ``git remote get-url origin`` on scanned candidates.
_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+?(?:\.git)?/?$"
)
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class InvalidGithubImportRequestError(ValueError):
    """Request-time validation failure (caller maps to 400)."""


def parse_github_repo_url(url: str) -> tuple[str, str]:
    """Return ``(owner, repo)`` for a canonical GitHub HTTPS URL or raise."""

    if not isinstance(url, str):
        raise InvalidGithubImportRequestError("repo_url 必須是字串")
    text = url.strip()
    if text != url or any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise InvalidGithubImportRequestError("repo_url 含有空白或控制字元")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise InvalidGithubImportRequestError("repo_url 不是合法網址") from exc
    if parts.username or parts.password or parts.query or parts.fragment:
        raise InvalidGithubImportRequestError("repo_url 不可包含帳密、query 或 fragment")
    match = _GITHUB_HTTPS_RE.match(text)
    if match is None:
        raise InvalidGithubImportRequestError(
            "只接受 https://github.com/<owner>/<repo> 形式的 GitHub 專案網址"
        )
    repo = match.group("repo")
    if repo in {".", ".."} or repo.endswith(".git"):
        raise InvalidGithubImportRequestError("repo 名稱不合法")
    return match.group("owner"), repo


def canonical_repo_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}"


def is_github_remote(remote: Optional[str]) -> bool:
    """True when a scanned candidate's ``origin`` points at github.com."""

    if not isinstance(remote, str):
        return False
    return _GITHUB_REMOTE_RE.match(remote.strip()) is not None


def is_github_source_reference(kind: Optional[str], reference: Optional[str]) -> bool:
    """Bootstrap ``ProjectSourceReference`` check for the GitHub-only policy."""

    return kind == "repo" and is_github_remote(reference)


def has_readme(readme_excerpt: Optional[str]) -> bool:
    return isinstance(readme_excerpt, str) and bool(readme_excerpt.strip())


def default_project_name(repo: str) -> str:
    return repo[:-4] if repo.endswith(".git") else repo


def staging_path(dest_path: str, approval_id: int) -> str:
    return f"{dest_path.rstrip('/')}{STAGING_SUFFIX}{int(approval_id)}"


def build_clone_command(repo_url: str, ref: Optional[str], staging: str) -> str:
    """``git clone`` into the staging directory; ``ref`` is validated first."""

    owner, repo = parse_github_repo_url(repo_url)
    url = canonical_repo_url(owner, repo)
    branch = ""
    if ref:
        if not _REF_RE.match(ref):
            raise InvalidGithubImportRequestError(f"ref 含不合法字元：{ref!r}")
        branch = f"-b {shlex.quote(ref)} "
    return f"git clone {branch}{shlex.quote(url)} {shlex.quote(staging)}"


def build_readme_check_command(staging: str) -> str:
    """Prints ``README_OK`` only when ``README.md`` exists and is non-empty."""

    readme = f"{staging.rstrip('/')}/README.md"
    return f"test -s {shlex.quote(readme)} && echo README_OK || echo README_MISSING"


def build_move_command(staging: str, dest_path: str) -> str:
    return f"mv {shlex.quote(staging)} {shlex.quote(dest_path)}"


def build_staging_cleanup_command(staging: str, approval_id: int) -> str:
    """Remove ONLY the staging directory this approval created (G-2)."""

    expected_suffix = f"{STAGING_SUFFIX}{int(approval_id)}"
    if not staging.endswith(expected_suffix) or "/" not in staging or staging.rstrip("/") == "/":
        raise ValueError("refusing to clean a path that is not this approval's staging directory")
    return f"rm -rf {shlex.quote(staging)}"


async def request_github_import_approval(
    db: Database,
    *,
    repo_url: str,
    project: Optional[str],
    target_server: str,
    dest_path: Optional[str] = None,
    ref: Optional[str] = None,
    server_configs: dict,
    ssh_run,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create the ``project_github_import`` card; nothing is cloned here."""

    owner, repo = parse_github_repo_url(repo_url)
    url = canonical_repo_url(owner, repo)
    name = (project or "").strip() or default_project_name(repo)
    try:
        validate_name_component(name, field="project")
    except InvalidNameError as exc:
        raise InvalidGithubImportRequestError(str(exc)) from exc
    if ref is not None:
        ref = ref.strip() or None
        if ref is not None and not _REF_RE.match(ref):
            raise InvalidGithubImportRequestError(f"ref 含不合法字元：{ref!r}")

    target_cfg = (server_configs or {}).get(target_server)
    if target_cfg is None or not getattr(target_cfg, "enabled", True):
        raise InvalidGithubImportRequestError(f"未知或未啟用的機器：{target_server}")
    if db.get_project(name) is not None:
        raise InvalidGithubImportRequestError(f"專案 {name} 已存在")

    if dest_path:
        raw_dest = dest_path.strip()
    else:
        roots = list(getattr(target_cfg, "project_roots", []) or [])
        if not roots:
            raise InvalidGithubImportRequestError(
                f"機器 {target_server} 未設定 project_roots，請明確提供 dest_path"
            )
        raw_dest = f"{roots[0].rstrip('/')}/{name}"
    if not raw_dest.startswith("/"):
        raise InvalidGithubImportRequestError("dest_path 必須是絕對路徑（以 / 開頭）")
    if ".." in raw_dest.split("/"):
        raise InvalidGithubImportRequestError("dest_path 不可包含 .. 片段")
    normalized_dest = raw_dest.rstrip("/") or "/"
    if normalized_dest == "/" or is_forbidden_root(normalized_dest):
        raise InvalidGithubImportRequestError(f"禁止匯入到的路徑：{normalized_dest}")

    if ssh_run is None:
        raise ValueError("request_github_import_approval 需要 ssh_run，呼叫端未提供")
    exist_check = await ssh_run(
        target_server, f"test -e {shlex.quote(normalized_dest)} || echo NOT_EXIST", 10
    )
    if "NOT_EXIST" not in (exist_check.stdout or ""):
        empty_check = await ssh_run(target_server, f"ls -A {shlex.quote(normalized_dest)}", 10)
        if getattr(empty_check, "exit_status", 1) != 0 or (empty_check.stdout or "").strip():
            raise InvalidGithubImportRequestError(
                f"目標路徑已存在且非空目錄：{normalized_dest}"
            )

    payload: dict[str, Any] = {
        "repo_url": url,
        "owner": owner,
        "repo": repo,
        "project": name,
        "target_server": target_server,
        "dest_path": normalized_dest,
        "ref": ref,
    }
    approval_id = db.insert_approval(
        kind=KIND,
        payload=payload,
        requester_actor_id=(request_context.actor_id if request_context is not None else None),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": KIND,
            "project": name,
            "repo_url": url,
            "target_server": target_server,
            "dest_path": normalized_dest,
            "ref": ref,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    approval = db.get_approval(approval_id)
    assert approval is not None  # just inserted
    return approval


__all__ = [
    "KIND",
    "InvalidGithubImportRequestError",
    "build_clone_command",
    "build_move_command",
    "build_readme_check_command",
    "build_staging_cleanup_command",
    "canonical_repo_url",
    "default_project_name",
    "has_readme",
    "is_github_remote",
    "is_github_source_reference",
    "parse_github_repo_url",
    "request_github_import_approval",
    "staging_path",
]
