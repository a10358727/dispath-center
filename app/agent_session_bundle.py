"""AgentSession checkpoint/bundle plumbing (DG-AGENT-SESSION-CHECKPOINT).

Lifted verbatim out of `app.agent_session_turns` in DG-AGENT-RUNTIME-V3
Phase 1b: this half is pure git-over-SSH (commit -> secret-file gate ->
`git bundle create`/`verify` on the runner -> rsync pull -> sha256 on
Server A) and feeds `resolve_promotion_candidate()` /
`engineering_task_promote` under INV-PLANE-1. It contains **no tmux and no
`claude -p`** and therefore survives the retirement of the per-turn
execution channel. V3 Studio sessions reuse it through the explicit
`repo_dir`/`bundle_path` overrides (their worktrees live under the runner
agent's `workspace_root`, reported back at `session/open` time), while the
legacy layout helpers stay byte-identical for existing bundles.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.db import AgentSession, Database

SshRunCallable = Callable[[str, str, float], Awaitable[Any]]

#: Runner-home-relative base directory for every legacy AgentSession's
#: persistent workspace (V1 layout; V3 sessions pass explicit overrides).
AGENT_SESSION_WORKSPACES_SUBDIR = "agent_sessions"

#: retained from the retired turn module: UI diff-preview character cap
AGENT_SESSION_DIFF_PREVIEW_MAX_CHARS = 65536


_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_WORKSPACE_BRANCH_RE = re.compile(
    r"^ai-session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
#: A `claude --resume` CLI session id is itself a UUID in every observed
#: build; validated the same way as our own session id.
_CLI_SESSION_ID_RE = _SESSION_ID_RE
#: Bash allowlist entries become `Bash(<entry>:*)` permission specifiers —
#: restricted to the same safe token shape as `_BASE_BRANCH_RE`
#: (`app.approvals`) so a validation-command name can never itself carry
#: shell metacharacters into the settings we hand the CLI.
_BASH_ALLOWLIST_ENTRY_RE = re.compile(r"^[A-Za-z0-9._/ -]{1,128}$")


class InvalidAgentSessionTurnInputError(ValueError):
    """A caller passed a value that is not a validated identifier."""


def _require_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise InvalidAgentSessionTurnInputError(f"invalid session_id: {session_id!r}")
    return session_id


def _require_workspace_branch(workspace_branch: str) -> str:
    if not isinstance(workspace_branch, str) or not _WORKSPACE_BRANCH_RE.match(
        workspace_branch
    ):
        raise InvalidAgentSessionTurnInputError(
            f"invalid workspace_branch: {workspace_branch!r}"
        )
    return workspace_branch


def _require_commit(base_commit: str) -> str:
    if not isinstance(base_commit, str) or not _COMMIT_RE.match(base_commit):
        raise InvalidAgentSessionTurnInputError(f"invalid base_commit: {base_commit!r}")
    return base_commit


def _reject_leading_dash(value: str, name: str) -> str:
    """Argv flag smuggling guard: `shlex.quote()` makes a value shell-safe
    but does **not** stop a value that starts with `-` from being parsed as
    a command flag by the program it is passed to (`git clone --mirror
    -evil-flag ...` would still shell-quote cleanly). Every externally
    influenceable string this module ever places as a bare positional
    argument to `git` must pass this check first — this is defense in depth
    alongside the `--` end-of-options markers the scripts emit."""

    if isinstance(value, str) and value.startswith("-"):
        raise InvalidAgentSessionTurnInputError(
            f"{name} must not start with '-' (argv flag smuggling guard): {value!r}"
        )
    return value


#: V3 override guard: an explicit runner-side path must be absolute, must
#: not smuggle a flag, and must stay inside a plausible home tree.
_ABSOLUTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/\- ]{1,300}\Z")


def _require_override_path(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ABSOLUTE_PATH_RE.match(value) or "/../" in value or value.endswith("/.."):
        raise InvalidAgentSessionTurnInputError(f"invalid {name}: {value!r}")
    return value


#: Emitted by `build_diff_command()`/`build_status_command()` instead of
#: running `git` at all when the session's persistent worktree has not been
#: created yet (no turn has run) — the caller must not mistake "nothing to
#: diff yet" for "clean worktree" (an actual `git diff` of a missing
#: directory would just silently error and look identical to a clean repo).
AGENT_SESSION_NO_WORKSPACE_MARKER = "__AGENT_SESSION_NO_WORKSPACE__"


def session_repo_dir(workspace_rel: str, session_id: str) -> str:
    """Runner-home-relative path to a session's persistent worktree — the
    same `repo_dir` `build_dispatch_paths()` computes, but usable without a
    turn number for turn-independent operations (diff/status, plan §5 P3)."""

    session_id = _require_session_id(session_id)
    workspace_rel = workspace_rel.strip("/")
    return f"{workspace_rel}/{AGENT_SESSION_WORKSPACES_SUBDIR}/{session_id}/repo"


_CHECKPOINT_SECRET_FILE_GREP = (
    r"^(\.env(\..*)?|\.envrc|auth\.json|credentials.*|secret\..*|secrets.*|"
    r"id_rsa.*|id_ed25519.*)$|\.pem$|\.key$|\.p12$|\.pfx$"
)

#: The checkpoint script's last non-empty stdout lines always include exactly
#: one `STATUS=` line (and, only on `ok`, one `RESULT_COMMIT=` line) — a
#: single-line-prefix marker contract, same discipline as
#: `AGENT_SESSION_NO_WORKSPACE_MARKER` above, chosen so orchestration parsing
#: never has to guess at multi-line/JSON shapes from a plain `ssh_run`.
AGENT_SESSION_CHECKPOINT_STATUS_PREFIX = "AGENT_SESSION_CHECKPOINT_STATUS="
AGENT_SESSION_CHECKPOINT_RESULT_COMMIT_PREFIX = "AGENT_SESSION_CHECKPOINT_RESULT_COMMIT="

#: Bounded local rsync-pull timeout (Server A -> Runner, outbound) — bundles
#: are small deltas, same order of magnitude as other local_run bundle pulls
#: (`app.results.build_result_pull_command` callers use similar bounds).
AGENT_SESSION_CHECKPOINT_PULL_TIMEOUT = 60.0


def checkpoint_commit_message(session_id: str) -> str:
    session_id = _require_session_id(session_id)
    return f"agent-session checkpoint {session_id}"


def checkpoint_bundle_remote_path(workspace_rel: str, session_id: str) -> str:
    """Runner-home-relative path for the checkpoint's `git bundle` — a
    sibling of the session's persistent worktree, not inside it, so the
    bundle file is never itself part of what the next `git status`/`diff`
    call reports."""

    session_id = _require_session_id(session_id)
    workspace_rel = workspace_rel.strip("/")
    return f"{workspace_rel}/{AGENT_SESSION_WORKSPACES_SUBDIR}/{session_id}/checkpoint.bundle"


def checkpoint_bundle_staging_path(local_home_dir: str, approval_id: int) -> str:
    """Server-A-local path the checkpoint bundle is pulled to *before* it is
    hashed/verified and moved into the canonical `results/{job_id}/
    changes.bundle` location that a real `jobs` row reserves (see
    `app.approvals`'s `agent_session_checkpoint` approve branch). Keyed by
    `approval_id` — already exists as the pending approval being decided —
    rather than `session_id`, so two different checkpoint attempts for the
    same session can never collide even if a prior attempt's staged file was
    never cleaned up."""

    if (
        not isinstance(approval_id, int)
        or isinstance(approval_id, bool)
        or approval_id <= 0
    ):
        raise InvalidAgentSessionTurnInputError(f"invalid approval_id: {approval_id!r}")
    base = (local_home_dir or ".").rstrip("/") or "."
    return f"{base}/agent_session_checkpoints/{approval_id}.bundle"


def build_checkpoint_script(
    *,
    session_id: str,
    workspace_rel: str,
    workspace_branch: str,
    base_commit: str,
    repo_dir: Optional[str] = None,
    bundle_path: Optional[str] = None,
) -> str:
    """Bounded, single-shot remote script for one checkpoint attempt: (a)
    `git add -A` + commit the session's persistent worktree with a fixed
    message (no_changes if nothing is staged — never an empty commit); (b)
    the secret-file guard above; (c) `git bundle create`/`bundle verify` of
    exactly the `base_commit..HEAD` range. Every externally-influenceable
    value is regex-validated first (`_require_session_id`/
    `_require_workspace_branch`/`_require_commit`) and only ever appears via
    `shlex.quote()`; the commit message is fixed text plus the
    already-UUID-validated `session_id`, so it can never carry a flag or
    shell metacharacter."""

    session_id = _require_session_id(session_id)
    workspace_branch = _require_workspace_branch(workspace_branch)
    base_commit = _require_commit(base_commit)
    if repo_dir is not None and bundle_path is not None:
        # V3 Studio sessions: the runner agent reported the worktree path at
        # `session/open`; both overrides must arrive together and validated.
        repo_dir = _require_override_path(repo_dir, "repo_dir")
        bundle_path = _require_override_path(bundle_path, "bundle_path")
    elif repo_dir is not None or bundle_path is not None:
        raise InvalidAgentSessionTurnInputError("repo_dir and bundle_path overrides must be provided together")
    else:
        repo_dir = session_repo_dir(workspace_rel, session_id)
        bundle_path = checkpoint_bundle_remote_path(workspace_rel, session_id)
    q_repo = shlex.quote(repo_dir)
    q_branch = shlex.quote(workspace_branch)
    q_base = shlex.quote(base_commit)
    q_bundle = shlex.quote(bundle_path)
    q_marker = shlex.quote(AGENT_SESSION_NO_WORKSPACE_MARKER)
    q_message = shlex.quote(checkpoint_commit_message(session_id))
    q_grep = shlex.quote(_CHECKPOINT_SECRET_FILE_GREP)
    status_prefix = AGENT_SESSION_CHECKPOINT_STATUS_PREFIX
    result_prefix = AGENT_SESSION_CHECKPOINT_RESULT_COMMIT_PREFIX

    return rf'''set -u
[ -d {q_repo} ] || {{ echo {q_marker}; exit 0; }}
CUR_BRANCH="$(git -C {q_repo} rev-parse --abbrev-ref HEAD 2>/dev/null)" || {{
  echo "{status_prefix}branch_unresolved"; exit 1; }}
[ "$CUR_BRANCH" = {q_branch} ] || {{
  echo "{status_prefix}branch_mismatch"; exit 1; }}
git -C {q_repo} -c core.hooksPath=/dev/null -c core.fsmonitor=false add -A || {{
  echo "{status_prefix}add_failed"; exit 1; }}
if git -C {q_repo} diff --cached --quiet; then
  echo "{status_prefix}no_changes"; exit 0
fi
git -C {q_repo} -c core.hooksPath=/dev/null -c core.fsmonitor=false \
    -c commit.gpgsign=false -c user.name='dispatch-center' \
    -c user.email='dispatch@local' commit --no-verify -m {q_message} || {{
  echo "{status_prefix}commit_failed"; exit 1; }}
RESULT_COMMIT="$(git -C {q_repo} rev-parse HEAD)"
SECRET_HITS="$(git -C {q_repo} diff --name-only {q_base}..HEAD -- | awk -F/ '{{print $NF}}' | grep -E -i {q_grep} || true)"
if [ -n "$SECRET_HITS" ]; then
  echo "{status_prefix}secret_violation"; exit 1
fi
git -C {q_repo} bundle create {q_bundle} {q_base}..HEAD || {{
  echo "{status_prefix}bundle_create_failed"; exit 1; }}
git -C {q_repo} bundle verify {q_bundle} >/dev/null 2>&1 || {{
  echo "{status_prefix}bundle_verify_failed"; exit 1; }}
echo "{result_prefix}$RESULT_COMMIT"
echo "{status_prefix}ok"
'''


def build_checkpoint_bundle_pull_command(
    *, remote_bundle_path: str, server: Any, local_staging_path: str
) -> str:
    """Server-A-local rsync pull of the checkpoint bundle — same `mkdir -p`
    + `rsync -a -e <ssh_opts>` shape as
    `app.results.build_result_pull_command`, but a fresh builder rather than
    an import: that function's remote source path is hardcoded to the
    `results/{job_id}/` job-queue convention, which this bundle does not
    live under until *after* this pull succeeds and gets moved into place by
    the caller."""

    from app.datasets import build_ssh_opts

    ssh_opts = build_ssh_opts(server.key_path, server.port)
    remote = f"{server.user}@{server.host}:{remote_bundle_path}"
    dest_dir = str(Path(local_staging_path).parent)
    return (
        f"mkdir -p {shlex.quote(dest_dir)} && "
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(remote)} "
        f"{shlex.quote(local_staging_path)}"
    )


def _sha256_and_size(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class AgentSessionCheckpointResult:
    """One checkpoint attempt's outcome.

    - ``"ok"``: commit + secret-file gate + bundle create/verify all
      succeeded and the bundle has already been pulled to Server A and
      hashed (`result_commit`/`bundle_local_path`/`bundle_sha256`/
      `bundle_size_bytes` all set).
    - ``"no_changes"``: nothing was staged; no commit, no bundle.
    - ``"no_workspace"``: the session's persistent worktree does not exist
      yet (no turn has ever run).
    - ``"branch_mismatch"``/``"branch_unresolved"``/``"add_failed"``/
      ``"commit_failed"``/``"bundle_create_failed"``/
      ``"bundle_verify_failed"``: a remote Git step failed; never a bundle.
    - ``"secret_violation"``: the checkpoint commit touched a protected
      filename; the commit stays on the Runner worktree but no bundle is
      produced or pulled.
    - ``"pull_failed"``: the remote pipeline reported `ok` but pulling the
      bundle back to Server A (or reading/hashing it locally) failed.
    - ``"unreachable"``: the Runner could not be reached at all (INV-SSH-7)
      — never a rejection; the caller must leave the approval pending.
    """

    status: str
    result_commit: Optional[str] = None
    bundle_local_path: Optional[str] = None
    bundle_sha256: Optional[str] = None
    bundle_size_bytes: Optional[int] = None
    detail: Optional[str] = None


async def run_agent_session_checkpoint_pipeline(
    db: Database,
    *,
    session: AgentSession,
    workspace_rel: str,
    runner_server: str,
    server_config: Any,
    local_home_dir: str,
    approval_id: int,
    ssh_run: SshRunCallable,
    local_run: Callable[[str, float], Awaitable[Any]],
    timeout_sec: float,
    repo_dir_override: Optional[str] = None,
    bundle_path_override: Optional[str] = None,
) -> AgentSessionCheckpointResult:
    """Run one checkpoint attempt end to end: remote commit/gate/bundle, then
    local pull + hash. Never touches SQLite — the caller (`app.approvals`'s
    `agent_session_checkpoint` approve branch) decides what a given
    `AgentSessionCheckpointResult` means for the approval and only creates
    bridge rows after this returns `status="ok"`."""

    if session.base_version_id is None:
        raise InvalidAgentSessionTurnInputError(
            "agent session has no base_version_id; cannot resolve a base commit"
        )
    version = db.get_project_version(session.base_version_id)
    if version is None:
        raise InvalidAgentSessionTurnInputError(
            f"base ProjectVersion {session.base_version_id!r} no longer exists"
        )

    script = build_checkpoint_script(
        session_id=session.id,
        workspace_rel=workspace_rel,
        workspace_branch=session.workspace_branch,
        base_commit=version.git_commit,
        repo_dir=repo_dir_override,
        bundle_path=bundle_path_override,
    )
    try:
        remote_result = await ssh_run(runner_server, script, timeout_sec)
    except Exception:  # noqa: BLE001 - any SSH-layer exception means unreachable
        return AgentSessionCheckpointResult(status="unreachable", detail="runner unreachable")

    stdout = remote_result.stdout or ""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if any(line == AGENT_SESSION_NO_WORKSPACE_MARKER for line in lines):
        return AgentSessionCheckpointResult(status="no_workspace")

    status: Optional[str] = None
    result_commit: Optional[str] = None
    for line in lines:
        if line.startswith(AGENT_SESSION_CHECKPOINT_STATUS_PREFIX):
            status = line[len(AGENT_SESSION_CHECKPOINT_STATUS_PREFIX):]
        elif line.startswith(AGENT_SESSION_CHECKPOINT_RESULT_COMMIT_PREFIX):
            result_commit = line[len(AGENT_SESSION_CHECKPOINT_RESULT_COMMIT_PREFIX):].strip()
    if status is None:
        return AgentSessionCheckpointResult(
            status="unreachable", detail="checkpoint script produced no status marker"
        )
    if status != "ok":
        return AgentSessionCheckpointResult(status=status, result_commit=result_commit)
    if not result_commit or not re.fullmatch(r"[0-9a-fA-F]{40,64}", result_commit):
        return AgentSessionCheckpointResult(
            status="pull_failed", detail="missing/invalid result commit"
        )

    remote_bundle_path = (
        bundle_path_override
        if bundle_path_override is not None
        else checkpoint_bundle_remote_path(workspace_rel, session.id)
    )
    staging_path = checkpoint_bundle_staging_path(local_home_dir, approval_id)
    pull_command = build_checkpoint_bundle_pull_command(
        remote_bundle_path=remote_bundle_path,
        server=server_config,
        local_staging_path=staging_path,
    )
    try:
        pull_result = await local_run(pull_command, AGENT_SESSION_CHECKPOINT_PULL_TIMEOUT)
    except Exception:  # noqa: BLE001
        return AgentSessionCheckpointResult(status="unreachable", detail="bundle pull unreachable")
    if getattr(pull_result, "exit_status", None) not in (0, None):
        return AgentSessionCheckpointResult(status="pull_failed", detail="bundle pull failed")

    try:
        bundle_sha256, bundle_size_bytes = _sha256_and_size(staging_path)
    except OSError:
        return AgentSessionCheckpointResult(
            status="pull_failed", detail="pulled bundle missing/unreadable"
        )

    return AgentSessionCheckpointResult(
        status="ok",
        result_commit=result_commit.lower(),
        bundle_local_path=staging_path,
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=bundle_size_bytes,
    )
