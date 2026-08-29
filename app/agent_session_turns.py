"""DG-AGENT-SESSION-V1 P2 (docs/DECISIONS.md 2026-08-24, docs/product/
AGENT_SESSION_V1_PLAN.md §5 P2): the per-turn Claude Code execution channel.

Pure, deterministic script/path/command assembly only — no I/O, no SSH, no
subprocess.  Mirrors the shape of `app.approvals.build_coding_task_script()`
(worktree/preflight/sentinel structure) and
`app.coding_agents.ClaudeCodeExecProvider` (pinned CLI version bounds,
`instruction never in shell string` rule) but is independent code: neither of
those pinned strings is imported or modified here (INV-SSH-2/3; existing
coding-task/job golden-string tests stay byte-unchanged).

Turn model (plan §2): one **persistent** session worktree (`REPO_DIR`, built
once on the first turn, reused by every later turn — `git worktree add`
against the session's fixed `workspace_branch`) plus one **per-turn**
artifact directory (`turns/{n}/`: prompt file, stream-json transcript,
`exit_code` sentinel, extracted `final_message.txt`/`cli_session_id.txt`).
The user message reaches `claude` only via
`< "$TURN_DIR/prompt.txt"` stdin redirect — it is never interpolated into
the script (INV-SSH-2/3).  Sentinel semantics mirror
`app.jobqueue`'s exit_code/tmux protocol (INV-SSH-6): only the exit_code file
decides success/failure; a vanished tmux session with no exit_code is an
honest "interrupted" failure, never silently swallowed (INV-SSH-7 covers the
unreachable-runner case, handled by the orchestration layer in this module,
not the remote script).

**D3 confinement note (read before relying on this for anything beyond the
personal pilot)**: the exact `claude` CLI flag shape below
(`--permission-mode`/`--allowedTools`/`--output-format stream-json
--verbose`/`--resume`) is this reviewer's best-known reading of the pinned
compatible CLI range (`app.coding_agents.CLAUDE_CODE_CLI_MIN_VERSION` /
`_MAX_VERSION_EXCLUSIVE`, i.e. Claude Code CLI 1.x).  It is pinned here by a
golden command-string test (`tests/test_agent_session_turns.py`) so any
accidental drift fails loudly, but it has **not** been exercised against a
real installed CLI — first real-Runner use is the actual validation of this
shape (D3: confinement that cannot be verified fails closed, not silently).
"""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from app.coding_agents import (
    CLAUDE_CODE_CLI_MAX_VERSION_EXCLUSIVE,
    CLAUDE_CODE_CLI_MIN_VERSION,
    CLAUDE_CODE_DEV_LOCAL_ALLOWED_TOOLS,
    PATH_EXTENSION_FRAGMENT,
)

#: Runner-home-relative base directory for every AgentSession's persistent
#: workspace + per-turn artifacts (mirrors `app.jobqueue.AGENT_JOBS_DIR` /
#: the coding-task `{workspace_rel}/tasks/{approval_id}` convention — no
#: `~/` prefix, SFTP does no tilde expansion).
AGENT_SESSION_WORKSPACES_SUBDIR = "agent_sessions"

#: D5: turn timeout is enforced remotely via `timeout <sec>`; the lazy
#: settle path (no background loop) additionally treats a turn as
#: interrupted once `active_turn_started_at + turn_timeout_sec + grace` has
#: elapsed and no sentinel has appeared — this is a local, DB-only fallback
#: for the case the remote `timeout` wrapper itself never got to run
#: (unreachable Runner, dead tmux, etc.), not a duplicate enforcement path.
AGENT_SESSION_TURN_TIMEOUT_GRACE_SEC = 30

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


def _require_turn_no(turn_no: int) -> int:
    if not isinstance(turn_no, int) or isinstance(turn_no, bool) or turn_no < 1:
        raise InvalidAgentSessionTurnInputError(f"invalid turn_no: {turn_no!r}")
    return turn_no


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
    alongside the `--` end-of-options markers in `build_turn_script()`."""

    if isinstance(value, str) and value.startswith("-"):
        raise InvalidAgentSessionTurnInputError(
            f"{name} must not start with '-' (argv flag smuggling guard): {value!r}"
        )
    return value


def tmux_session_name(session_id: str, turn_no: int) -> str:
    """`agent_turn_{sid8}_{n}` — `sid8` is the first 8 hex chars of the
    session UUID (tmux session names are otherwise unbounded but keeping this
    short avoids any Runner-side name-length surprise, same rationale as
    `job_{job_id}` staying numeric in `app.jobqueue`)."""

    session_id = _require_session_id(session_id)
    turn_no = _require_turn_no(turn_no)
    return f"agent_turn_{session_id[:8]}_{turn_no}"


@dataclass(frozen=True)
class AgentSessionTurnPaths:
    """Runner-home-relative paths for one session's persistent workspace and
    one turn's artifacts."""

    session_dir: str
    repo_dir: str
    mirror_path: str
    turn_dir: str
    prompt_file: str
    transcript_file: str
    exit_code_file: str
    final_message_file: str
    cli_session_id_file: str


def turn_dir(workspace_rel: str, session_id: str, turn_no: int) -> str:
    session_id = _require_session_id(session_id)
    turn_no = _require_turn_no(turn_no)
    workspace_rel = workspace_rel.strip("/")
    return f"{workspace_rel}/{AGENT_SESSION_WORKSPACES_SUBDIR}/{session_id}/turns/{turn_no}"


def build_dispatch_paths(
    workspace_rel: str, project: str, session_id: str, turn_no: int
) -> AgentSessionTurnPaths:
    session_id = _require_session_id(session_id)
    turn_no = _require_turn_no(turn_no)
    workspace_rel = workspace_rel.strip("/")
    session_dir = f"{workspace_rel}/{AGENT_SESSION_WORKSPACES_SUBDIR}/{session_id}"
    a_turn_dir = f"{session_dir}/turns/{turn_no}"
    return AgentSessionTurnPaths(
        session_dir=session_dir,
        repo_dir=f"{session_dir}/repo",
        mirror_path=f"{workspace_rel}/mirrors/{project}.git",
        turn_dir=a_turn_dir,
        prompt_file=f"{a_turn_dir}/prompt.txt",
        transcript_file=f"{a_turn_dir}/transcript.jsonl",
        exit_code_file=f"{a_turn_dir}/exit_code",
        final_message_file=f"{a_turn_dir}/final_message.txt",
        cli_session_id_file=f"{a_turn_dir}/cli_session_id.txt",
    )


def build_mkdir_command(workspace_rel: str, project: str, session_id: str, turn_no: int) -> str:
    paths = build_dispatch_paths(workspace_rel, project, session_id, turn_no)
    return f"mkdir -p {shlex.quote(paths.turn_dir)}"


def build_launch_command(session_id: str, turn_no: int, run_sh_path: str) -> str:
    name = tmux_session_name(session_id, turn_no)
    return f"tmux new-session -d -s {shlex.quote(name)} 'bash {shlex.quote(run_sh_path)}'"


def build_tmux_check_command(session_id: str, turn_no: int) -> str:
    name = shlex.quote(tmux_session_name(session_id, turn_no))
    return f"tmux has-session -t {name} 2>/dev/null && echo EXISTS || echo GONE"


def build_check_exit_code_command(
    workspace_rel: str, project: str, session_id: str, turn_no: int
) -> str:
    paths = build_dispatch_paths(workspace_rel, project, session_id, turn_no)
    return f"cat {shlex.quote(paths.exit_code_file)} 2>/dev/null"


def build_read_file_command(path: str, *, max_bytes: int = 65536) -> str:
    """Bounded read of a small sentinel-adjacent file (final_message.txt /
    cli_session_id.txt) — same `tail -c` byte cap discipline as
    `app.jobqueue.build_log_tail_command()`."""

    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise InvalidAgentSessionTurnInputError(f"invalid max_bytes: {max_bytes!r}")
    return f"cat {shlex.quote(path)} 2>/dev/null | tail -c {max_bytes}"


def build_transcript_tail_command(
    workspace_rel: str,
    project: str,
    session_id: str,
    turn_no: int,
    *,
    offset: int = 0,
    max_bytes: int = 65536,
) -> str:
    """Live-tail the turn's transcript from a byte `offset` (mirrors
    `GET /jobs/{id}/log`'s live SSH tail, `app.jobqueue.build_log_tail_command`
    — bounded bytes per call, same `tail -c` defense-in-depth cap)."""

    if not isinstance(offset, int) or offset < 0:
        raise InvalidAgentSessionTurnInputError(f"invalid offset: {offset!r}")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise InvalidAgentSessionTurnInputError(f"invalid max_bytes: {max_bytes!r}")
    paths = build_dispatch_paths(workspace_rel, project, session_id, turn_no)
    path = shlex.quote(paths.transcript_file)
    return f"tail -c +{offset + 1} {path} 2>/dev/null | head -c {max_bytes}"


#: DG-AGENT-SESSION-V1 P3 (docs/product/AGENT_SESSION_V1_PLAN.md §5 P3 step
#: 1): read-only remote diff of the session worktree. Same `head -c` byte-cap
#: discipline as the transcript tail / job-log tail commands above.
AGENT_SESSION_DIFF_MAX_BYTES = 65536

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


def build_diff_command(
    workspace_rel: str,
    session_id: str,
    base_commit: str,
    *,
    max_bytes: int = AGENT_SESSION_DIFF_MAX_BYTES,
) -> str:
    """Read-only `git diff <base_commit>` against the current working tree
    (index + worktree) of the session's persistent worktree — a single
    command that captures both any commits made since `base_commit` (none in
    V1 before a checkpoint) and any uncommitted edits Claude's Edit/Write
    tools made (plan §5 P3: "target = current worktree state"). `base_commit`
    is regex-validated (`_require_commit`) so it can never be interpreted as
    a flag, and the trailing bare `--` is defense in depth ending option
    parsing before the (empty) pathspec list, mirroring the P2 `--`
    end-of-options precedent."""

    repo_dir = session_repo_dir(workspace_rel, session_id)
    base_commit = _require_commit(base_commit)
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise InvalidAgentSessionTurnInputError(f"invalid max_bytes: {max_bytes!r}")
    q_repo = shlex.quote(repo_dir)
    q_commit = shlex.quote(base_commit)
    q_marker = shlex.quote(AGENT_SESSION_NO_WORKSPACE_MARKER)
    return (
        f"[ -d {q_repo} ] || {{ echo {q_marker}; exit 0; }}; "
        f"git -C {q_repo} --no-pager diff --no-ext-diff --no-textconv {q_commit} -- "
        f"2>/dev/null | head -c {int(max_bytes)}"
    )


def build_status_command(
    workspace_rel: str,
    session_id: str,
    *,
    max_bytes: int = AGENT_SESSION_DIFF_MAX_BYTES,
) -> str:
    """Read-only `git status --porcelain --untracked-files=all` of the
    session's persistent worktree. `git diff` alone never reports untracked
    files, so the diff endpoint always fetches this too and reports
    dirty/untracked path lists separately — nothing the worktree holds is
    invisible (plan §5 P3 step 1)."""

    repo_dir = session_repo_dir(workspace_rel, session_id)
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise InvalidAgentSessionTurnInputError(f"invalid max_bytes: {max_bytes!r}")
    q_repo = shlex.quote(repo_dir)
    q_marker = shlex.quote(AGENT_SESSION_NO_WORKSPACE_MARKER)
    return (
        f"[ -d {q_repo} ] || {{ echo {q_marker}; exit 0; }}; "
        f"git -C {q_repo} status --porcelain --untracked-files=all -- "
        f"2>/dev/null | head -c {int(max_bytes)}"
    )


def _confinement_allowed_tools(bash_allowlist: Sequence[str]) -> str:
    """`--allowedTools` value (plan §3 dev-local tool set): file tools always
    on, Bash only for the caller-provided validated allowlist (V1 default
    empty = deny all Bash, D4/plan §3). Every allowlist entry is re-validated
    here — this function has no other caller and must not trust a value that
    merely looks pre-validated."""

    tools = list(CLAUDE_CODE_DEV_LOCAL_ALLOWED_TOOLS)
    if bash_allowlist:
        specs = []
        for entry in bash_allowlist:
            if not isinstance(entry, str) or not _BASH_ALLOWLIST_ENTRY_RE.match(entry):
                raise InvalidAgentSessionTurnInputError(
                    f"invalid bash_allowlist entry: {entry!r}"
                )
            specs.append(f"Bash({entry}:*)")
        tools.append(" ".join(specs))
    return " ".join(tools)


def build_turn_script(
    *,
    session_id: str,
    turn_no: int,
    workspace_rel: str,
    project: str,
    project_source_url: str,
    workspace_branch: str,
    base_commit: str,
    prior_cli_session_id: Optional[str],
    turn_timeout_sec: int,
    bash_allowlist: Sequence[str] = (),
    network_access: bool = False,
) -> str:
    """Assemble the full `run.sh` for exactly one turn (plan §5 P2 step 1).

    `project_source_url` is `projects.repo_or_path` (same "hub" mirror
    source the coding-task `mirror` source_kind clones from) — this function
    never accepts an agent- or model-supplied URL. On the first turn
    (`REPO_DIR` absent) the script clones/updates a bare mirror at
    `{workspace_rel}/mirrors/{project}.git` and creates the persistent
    worktree with `git worktree add "$REPO_DIR" -b <workspace_branch>
    <base_commit>`; on every later turn it instead verifies `REPO_DIR` is
    still a git worktree on the expected branch (idempotent — never
    re-creates, never silently switches branch) and fails closed with a
    typed `R_ERROR` otherwise.

    The user's message never appears in this string — it reaches `claude`
    only via `< "$TURN_DIR/prompt.txt"` (written independently by the
    orchestration layer through the existing `ssh_write_file` SFTP path,
    INV-SSH-2/3).
    """

    session_id = _require_session_id(session_id)
    turn_no = _require_turn_no(turn_no)
    workspace_branch = _require_workspace_branch(workspace_branch)
    base_commit = _require_commit(base_commit)
    if not isinstance(project, str) or not project:
        raise InvalidAgentSessionTurnInputError("project is required")
    if not isinstance(project_source_url, str) or not project_source_url:
        raise InvalidAgentSessionTurnInputError("project_source_url is required")
    # `project` (a free-text `projects.name`/`.id`) and `project_source_url`
    # (free-text `projects.repo_or_path`) are the only two values in this
    # function that are both externally influenceable *and* ever placed as a
    # bare positional argument to a git subcommand
    # (`git clone --mirror <url> <dir>`) — a value starting with `-` would
    # otherwise be parsed as a git flag instead of a repository/path
    # (argv flag smuggling). Reject outright rather than relying solely on
    # the `--`-end-of-options markers below (defense in depth: some git
    # subcommands/builds do not honor `--` for every argument shape).
    _reject_leading_dash(project, "project")
    _reject_leading_dash(project_source_url, "project_source_url")
    if not isinstance(turn_timeout_sec, int) or turn_timeout_sec <= 0:
        raise InvalidAgentSessionTurnInputError(
            f"invalid turn_timeout_sec: {turn_timeout_sec!r}"
        )
    if prior_cli_session_id is not None and not _CLI_SESSION_ID_RE.match(
        prior_cli_session_id
    ):
        raise InvalidAgentSessionTurnInputError(
            f"invalid prior_cli_session_id: {prior_cli_session_id!r}"
        )

    paths = build_dispatch_paths(workspace_rel, project, session_id, turn_no)
    q_repo = shlex.quote(paths.repo_dir)
    q_mirror = shlex.quote(paths.mirror_path)
    q_url = shlex.quote(project_source_url)
    q_branch = shlex.quote(workspace_branch)
    q_commit = shlex.quote(base_commit)
    q_prompt = shlex.quote(paths.prompt_file)
    q_transcript = shlex.quote(paths.transcript_file)
    q_exit_code = shlex.quote(paths.exit_code_file)
    q_final_message = shlex.quote(paths.final_message_file)
    q_cli_session_id_file = shlex.quote(paths.cli_session_id_file)
    q_turn_dir = shlex.quote(paths.turn_dir)

    min_bound = (
        CLAUDE_CODE_CLI_MIN_VERSION[0] * 1_000_000
        + CLAUDE_CODE_CLI_MIN_VERSION[1] * 1_000
        + CLAUDE_CODE_CLI_MIN_VERSION[2]
    )
    max_bound = (
        CLAUDE_CODE_CLI_MAX_VERSION_EXCLUSIVE[0] * 1_000_000
        + CLAUDE_CODE_CLI_MAX_VERSION_EXCLUSIVE[1] * 1_000
        + CLAUDE_CODE_CLI_MAX_VERSION_EXCLUSIVE[2]
    )

    # PATH_EXTENSION_FRAGMENT (app.coding_agents): `claude` often lives under
    # `~/.local/bin`/`~/.npm-global/bin`/an nvm `node/*/bin`, none of which a
    # non-interactive SSH shell's default PATH includes (real-runner job 96
    # diagnosis: installed and logged in, still reported not installed).
    # `EXTENDED_PATH` snapshots the final widened `$PATH` for the `env -i`
    # forward below (`env -i` drops the inherited, un-widened `$PATH`
    # otherwise).
    preflight = (
        PATH_EXTENSION_FRAGMENT
        + '  EXTENDED_PATH="$PATH"\n'
        "  command -v claude >/dev/null 2>&1 || fail 'claude CLI not installed"
        "; install and log in on the AgentSession Runner'\n"
        '  R_CLI_VERSION="$(claude --version 2>/dev/null | head -1)"\n'
        '  CLI_VERSION_NUM="$(printf \'%s\\n\' "$R_CLI_VERSION" | '
        "grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1)\"\n"
        '  [ -n "$CLI_VERSION_NUM" ] || fail \'claude CLI version could not be parsed\'\n'
        f"  awk -v v=\"$CLI_VERSION_NUM\" 'BEGIN{{split(v,a,\".\");"
        f"n=a[1]*1000000+a[2]*1000+a[3]; if (n>={min_bound} && n<{max_bound}) "
        "exit 0; exit 1}' || fail 'claude CLI version outside the reviewed compatible range'\n"
        "  claude auth status >/dev/null 2>&1 || fail 'claude is not logged in on this Runner'\n"
        "  [ \"$(id -u)\" != \"0\" ] || fail 'refusing to run claude as root'\n"
        "  git --version >/dev/null 2>&1 || fail 'git not found'\n"
    )

    ensure_workspace = (
        f"  if [ -d {q_repo} ]; then\n"
        f"    git -C {q_repo} rev-parse --is-inside-work-tree >/dev/null 2>&1 "
        "|| fail 'existing session workspace is not a git worktree'\n"
        f'    R_CUR_BRANCH="$(git -C {q_repo} rev-parse --abbrev-ref HEAD)"\n'
        f'    [ "$R_CUR_BRANCH" = {q_branch} ] '
        "|| fail 'existing session workspace is on an unexpected branch'\n"
        "  else\n"
        f"    if [ -d {q_mirror} ]; then\n"
        f"      git --git-dir={q_mirror} remote update --prune "
        "|| fail 'mirror update failed'\n"
        "    else\n"
        f"      git clone --mirror -- {q_url} {q_mirror} || fail 'mirror clone failed'\n"
        "    fi\n"
        f"    git --git-dir={q_mirror} cat-file -e {q_commit}^{{commit}} "
        "|| fail 'mirror does not contain the session base commit'\n"
        f"    git --git-dir={q_mirror} worktree add -b {q_branch} -- {q_repo} {q_commit} "
        "|| fail 'session worktree creation failed'\n"
        "  fi\n"
    )

    allowed_tools = _confinement_allowed_tools(tuple(bash_allowlist))
    resume_flag = (
        f' --resume {shlex.quote(prior_cli_session_id)}' if prior_cli_session_id else ""
    )
    network_note = (
        "network access via the (empty by default) Bash allowlist only"
        if network_access
        else "no network-capable tool granted"
    )
    turn_invocation = (
        f"  # confinement: cwd IS the workspace — Claude Code scopes file tools to\n"
        f"  # the working-directory tree, so running from $HOME would expose the\n"
        f"  # whole home (including ~/.ssh, ~/.claude) to Read/Edit. The subshell\n"
        f"  # cds into the worktree; prompt/transcript paths get an explicit\n"
        f"  # \"$HOME\"/ prefix because every path in this script is home-relative\n"
        f"  # (SFTP does not expand ~, see app/jobqueue.py module docstring).\n"
        f"  # Bash limited to the validated allowlist ({network_note}); no\n"
        f"  # platform/approval tool exists in this CLI's tool set at all (D4).\n"
        f"  [ -d {q_repo} ] || fail 'workspace 目錄不存在'\n"
        f"  ( cd {q_repo} && exec env -i HOME=\"$HOME\" PATH=\"$EXTENDED_PATH\" USER=\"$USER\" "
        "LANG=C.UTF-8 LC_ALL=C.UTF-8 TERM=dumb \\\n"
        f"    timeout {int(turn_timeout_sec)}s claude -p \\\n"
        "    --add-dir . \\\n"
        "    --output-format stream-json --verbose \\\n"
        "    --permission-mode acceptEdits \\\n"
        f"    --allowedTools {shlex.quote(allowed_tools)}{resume_flag} \\\n"
        f"    < \"$HOME\"/{q_prompt} > \"$HOME\"/{q_transcript} )\n"
        "  R_CLAUDE_EXIT=$?\n"
    )

    #: `-c` args passed positionally so bash never has to quote a Python
    #: string literal inside a `<<'PYEOF'` heredoc — the heredoc delimiter is
    #: quoted (no shell expansion of `$1`/`$2`/`$3` inside it), so the paths
    #: are threaded in via `sys.argv` instead (same technique as
    #: `app.approvals.build_coding_task_script()`'s `write_result()`).
    extract_block = (
        "  python3 - "
        f"{q_transcript} {q_final_message} {q_cli_session_id_file} <<'PYEOF'\n"
        "import json, os, sys\n"
        "transcript, final_message_file, cli_session_id_file = sys.argv[1:4]\n"
        "result_text = None\n"
        "cli_session_id = None\n"
        "assistant_fragments = []\n"
        "try:\n"
        "    with open(transcript) as fh:\n"
        "        for line in fh:\n"
        "            line = line.strip()\n"
        "            if not line:\n"
        "                continue\n"
        "            try:\n"
        "                event = json.loads(line)\n"
        "            except Exception:\n"
        "                continue\n"
        "            if not isinstance(event, dict):\n"
        "                continue\n"
        "            sid = event.get('session_id')\n"
        "            if isinstance(sid, str) and sid:\n"
        "                cli_session_id = sid\n"
        "            if event.get('type') == 'result' and isinstance(event.get('result'), str):\n"
        "                result_text = event['result']\n"
        "            if event.get('type') == 'assistant':\n"
        "                message = event.get('message') or {}\n"
        "                for block in message.get('content') or []:\n"
        "                    if isinstance(block, dict) and block.get('type') == 'text':\n"
        "                        text = block.get('text')\n"
        "                        if isinstance(text, str):\n"
        "                            assistant_fragments.append(text)\n"
        "except FileNotFoundError:\n"
        "    pass\n"
        "if result_text is None:\n"
        "    result_text = '\\n'.join(assistant_fragments)\n"
        "with open(final_message_file, 'w') as fh:\n"
        "    fh.write(result_text or '')\n"
        "with open(cli_session_id_file, 'w') as fh:\n"
        "    fh.write(cli_session_id or '')\n"
        "PYEOF\n"
    )

    script = r"""set -u
TURN_DIR="__TURN_DIR__"
log() { echo "[agent_session_turn] $*"; }
export R_ERROR=""
fail() { export R_ERROR="$1"; log "FAIL: $1"; exit 1; }

main() {
  mkdir -p "$TURN_DIR" || { log "cannot create TURN_DIR"; exit 1; }
__PREFLIGHT_BLOCK__
  [ -s __PROMPT_FILE__ ] || fail 'prompt.txt missing (should have been SFTP-written before launch)'

__ENSURE_WORKSPACE_BLOCK__

__TURN_INVOCATION_BLOCK__

__EXTRACT_BLOCK__
  exit "$R_CLAUDE_EXIT"
}
main 2>&1 | tee __TURN_DIR_LOG__
EXIT_CODE="${PIPESTATUS[0]}"
echo "$EXIT_CODE" > __EXIT_CODE_FILE__
exit "$EXIT_CODE"
"""
    script = script.replace("__TURN_DIR__", paths.turn_dir)
    script = script.replace("__PROMPT_FILE__", q_prompt)
    script = script.replace("__PREFLIGHT_BLOCK__", preflight.rstrip("\n"))
    script = script.replace("__ENSURE_WORKSPACE_BLOCK__", ensure_workspace.rstrip("\n"))
    script = script.replace("__TURN_INVOCATION_BLOCK__", turn_invocation.rstrip("\n"))
    script = script.replace("__EXTRACT_BLOCK__", extract_block.rstrip("\n"))
    script = script.replace("__TURN_DIR_LOG__", f"{q_turn_dir}/turn.log")
    script = script.replace("__EXIT_CODE_FILE__", q_exit_code)
    return script


__all__ = [
    "AGENT_SESSION_DIFF_MAX_BYTES",
    "AGENT_SESSION_NO_WORKSPACE_MARKER",
    "AGENT_SESSION_TURN_TIMEOUT_GRACE_SEC",
    "AGENT_SESSION_WORKSPACES_SUBDIR",
    "AgentSessionCheckpointResult",
    "AgentSessionDiffResult",
    "AgentSessionTurnLaunchResult",
    "AgentSessionTurnPaths",
    "AgentSessionTurnStatus",
    "InvalidAgentSessionTurnInputError",
    "build_check_exit_code_command",
    "build_checkpoint_bundle_pull_command",
    "build_checkpoint_script",
    "build_diff_command",
    "build_dispatch_paths",
    "build_launch_command",
    "build_mkdir_command",
    "build_read_file_command",
    "build_status_command",
    "build_tmux_check_command",
    "build_transcript_tail_command",
    "build_turn_script",
    "checkpoint_bundle_remote_path",
    "checkpoint_bundle_staging_path",
    "checkpoint_commit_message",
    "collect_agent_session_diff",
    "converge_agent_session_turn",
    "launch_agent_session_turn",
    "run_agent_session_checkpoint_pipeline",
    "session_repo_dir",
    "tmux_session_name",
    "turn_dir",
]


# ---------------------------------------------------------------------------
# Orchestration (plan §5 P2 step 2) — the only I/O in this module.  Every
# function below takes `ssh_run`/`ssh_write_file` as injected async
# callables (same shape as `app.main.AppState.ssh_run`/`ssh_write_file`:
# `await ssh_run(server_name, command, timeout) -> CommandResult` with a
# `.stdout` attribute, `await ssh_write_file(server_name, path, content)`),
# never `app_state` directly — this keeps the module trivially testable with
# `FakeSSH` and matches `app.conversations.run_conversation_turn()`'s
# injected-callable style.
# ---------------------------------------------------------------------------

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from app.db import AgentSession, AIConversationMessage, Database, Project
from app.engineering_tasks import redact_engineering_text

SshRunCallable = Callable[[str, str, float], Awaitable[Any]]
SshWriteFileCallable = Callable[[str, str, str], Awaitable[Any]]

#: D5: message body byte cap (plan §5 P2 route step 3, "content bounded
#: 64KiB") — separate constant from
#: `Database.AI_CONVERSATION_MESSAGE_MAX_BYTES` (CV-2a's chat turns) because
#: the two surfaces are independently reviewed even though they currently
#: share the same value.
AGENT_SESSION_MESSAGE_MAX_BYTES = 65536

#: A single lazy-settle SSH probe (exit_code/tmux/file read) budget — same
#: 15s used throughout `app.jobqueue`'s reconcile helpers.
AGENT_SESSION_SSH_PROBE_TIMEOUT = 15.0


class AgentSessionRunnerUnavailableError(RuntimeError):
    """No configured Runner is available for AgentSession turns (INV-SSH-7:
    this is a degraded-typed-result condition for callers, not a 5xx)."""


@dataclass(frozen=True)
class AgentSessionTurnLaunchResult:
    turn_no: int
    session: AgentSession
    user_message: AIConversationMessage


async def launch_agent_session_turn(
    db: Database,
    *,
    session: AgentSession,
    project: Project,
    content: str,
    workspace_rel: str,
    runner_server: str,
    ssh_run: SshRunCallable,
    ssh_write_file: SshWriteFileCallable,
    bash_allowlist: Sequence[str] = (),
    network_access: bool = False,
) -> AgentSessionTurnLaunchResult:
    """Claim the next turn number, persist the user's message, and launch one
    bounded headless turn on the Runner (plan §5 P2 step 2).

    Raises `AgentSessionNotActiveError`/`AgentSessionTurnConflictError`/
    `AgentSessionTurnLimitError` (`app.db`) before anything reaches the
    network — the route layer maps all three to 404/409 (see
    `app.main.post_agent_session_message_endpoint`).  Once the turn is
    claimed, an SSH/SFTP failure propagates as-is; the caller (route layer)
    must treat that as a degraded typed result and leave the session
    `active` (INV-SSH-7) — the claimed turn self-heals via
    `converge_agent_session_turn()`'s lazy timeout branch once
    `turn_timeout_sec` + grace has elapsed, without any background loop or
    explicit rollback path."""

    if session.base_version_id is None:
        raise InvalidAgentSessionTurnInputError(
            "agent session has no base_version_id; cannot resolve a base commit"
        )
    version = db.get_project_version(session.base_version_id)
    if version is None:
        raise InvalidAgentSessionTurnInputError(
            f"base ProjectVersion {session.base_version_id!r} no longer exists"
        )

    claimed = db.begin_agent_session_turn(session.id)
    turn_no = claimed.active_turn_no
    assert turn_no is not None  # begin_agent_session_turn() always sets it

    user_message = db.append_conversation_message(
        session.conversation_id, role="user", content=content, refs=None
    )

    script = build_turn_script(
        session_id=session.id,
        turn_no=turn_no,
        workspace_rel=workspace_rel,
        project=project.name,
        project_source_url=project.repo_or_path,
        workspace_branch=session.workspace_branch,
        base_commit=version.git_commit,
        prior_cli_session_id=claimed.cli_session_id,
        turn_timeout_sec=session.turn_timeout_sec,
        bash_allowlist=bash_allowlist,
        network_access=network_access,
    )
    paths = build_dispatch_paths(workspace_rel, project.name, session.id, turn_no)
    run_sh_path = f"{paths.turn_dir}/run.sh"

    await ssh_run(
        runner_server,
        build_mkdir_command(workspace_rel, project.name, session.id, turn_no),
        AGENT_SESSION_SSH_PROBE_TIMEOUT,
    )
    await ssh_write_file(runner_server, paths.prompt_file, content)
    await ssh_write_file(runner_server, run_sh_path, script)
    await ssh_run(
        runner_server,
        build_launch_command(session.id, turn_no, run_sh_path),
        AGENT_SESSION_SSH_PROBE_TIMEOUT,
    )

    return AgentSessionTurnLaunchResult(
        turn_no=turn_no, session=claimed, user_message=user_message
    )


@dataclass(frozen=True)
class AgentSessionTurnStatus:
    """`GET .../transcript`'s status classification.

    - ``"running"``: tmux session alive, no sentinel yet.
    - ``"done"``/``"failed"``: sentinel converged this call (exit_code 0/
      nonzero); `assistant_message` is the newly-persisted reply.
    - ``"interrupted"``: sentinel converged this call via the tmux-gone-
      no-exit-code branch or the local timeout fallback (INV-SSH-6/7 —
      honest failure, never silently swallowed).
    - ``"settled"``: `turn_no` was already converged by an earlier call
      (no new assistant message produced here).
    - ``"not_running"``: `turn_no` was never this session's active turn.
    - ``"unreachable"``: the Runner could not be reached this call; the
      session is left untouched (INV-SSH-7).
    """

    status: str
    turn_no: int
    exit_code: Optional[int] = None
    assistant_message: Optional[AIConversationMessage] = None
    detail: Optional[str] = None


def _elapsed_seconds(started_at: Optional[str]) -> Optional[float]:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


async def _finalize_turn(
    db: Database,
    session: AgentSession,
    turn_no: int,
    *,
    status: str,
    content: str,
    exit_code: Optional[int],
    cli_session_id: Optional[str] = None,
    detail: Optional[str] = None,
) -> AgentSessionTurnStatus:
    settled = db.settle_agent_session_turn(
        session.id, turn_no, cli_session_id=cli_session_id
    )
    if settled is None:
        # A concurrent caller (another poller) already converged this turn —
        # no-op here, report "settled" rather than double-posting a message.
        return AgentSessionTurnStatus(status="settled", turn_no=turn_no, detail=detail)
    message = db.append_conversation_message(
        session.conversation_id, role="assistant", content=content, refs=None
    )
    return AgentSessionTurnStatus(
        status=status,
        turn_no=turn_no,
        exit_code=exit_code,
        assistant_message=message,
        detail=detail,
    )


async def converge_agent_session_turn(
    db: Database,
    *,
    session: AgentSession,
    project_name: str,
    turn_no: int,
    workspace_rel: str,
    runner_server: str,
    ssh_run: SshRunCallable,
) -> AgentSessionTurnStatus:
    """Lazy settle for one turn (plan §5 P2 step 2/3, checked on every
    transcript/message request — no background loop, mirrors
    `Database._expire_agent_session_if_idle()`'s read-path convergence and
    `app.jobqueue.reconcile_job()`'s exit_code → tmux → race-recheck shape).
    """

    if session.active_turn_no != turn_no:
        if turn_no <= session.turn_count:
            return AgentSessionTurnStatus(status="settled", turn_no=turn_no)
        return AgentSessionTurnStatus(status="not_running", turn_no=turn_no)

    elapsed = _elapsed_seconds(session.active_turn_started_at)
    timeout_ceiling = session.turn_timeout_sec + AGENT_SESSION_TURN_TIMEOUT_GRACE_SEC
    if elapsed is not None and elapsed > timeout_ceiling:
        return await _finalize_turn(
            db,
            session,
            turn_no,
            status="interrupted",
            content=(
                "（此輪逾時未收斂：已超過 "
                f"{session.turn_timeout_sec} 秒 timeout，Runner 未回報結果，"
                "視為中斷，並未判定為成功或失敗）"
            ),
            exit_code=None,
            detail="turn timed out (local lazy-settle, no sentinel observed)",
        )

    exit_code_cmd = build_check_exit_code_command(
        workspace_rel, project_name, session.id, turn_no
    )
    try:
        exit_res = await ssh_run(
            runner_server, exit_code_cmd, AGENT_SESSION_SSH_PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001 - any SSH-layer exception means unreachable
        return AgentSessionTurnStatus(
            status="unreachable", turn_no=turn_no, detail="runner unreachable"
        )

    settled = await _settle_from_exit_code_output(
        db,
        session=session,
        project_name=project_name,
        turn_no=turn_no,
        workspace_rel=workspace_rel,
        runner_server=runner_server,
        ssh_run=ssh_run,
        raw_exit_code=(exit_res.stdout or ""),
    )
    if settled is not None:
        return settled

    try:
        tmux_res = await ssh_run(
            runner_server,
            build_tmux_check_command(session.id, turn_no),
            AGENT_SESSION_SSH_PROBE_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return AgentSessionTurnStatus(
            status="unreachable", turn_no=turn_no, detail="runner unreachable"
        )
    if "EXISTS" in (tmux_res.stdout or ""):
        return AgentSessionTurnStatus(status="running", turn_no=turn_no)

    # tmux reports gone — re-check exit_code once to close the race window
    # between the first check and now (same rationale as
    # `app.jobqueue.reconcile_job()`).
    try:
        exit_res2 = await ssh_run(
            runner_server, exit_code_cmd, AGENT_SESSION_SSH_PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001
        return AgentSessionTurnStatus(
            status="unreachable", turn_no=turn_no, detail="runner unreachable"
        )
    settled = await _settle_from_exit_code_output(
        db,
        session=session,
        project_name=project_name,
        turn_no=turn_no,
        workspace_rel=workspace_rel,
        runner_server=runner_server,
        ssh_run=ssh_run,
        raw_exit_code=(exit_res2.stdout or ""),
    )
    if settled is not None:
        return settled

    return await _finalize_turn(
        db,
        session,
        turn_no,
        status="interrupted",
        content=(
            "（此輪中斷：tmux session 已結束但未留下結果，Runner 端可能重啟或被中止，"
            "並未判定為成功或失敗）"
        ),
        exit_code=None,
        detail="tmux session gone without exit_code (INV-SSH-6)",
    )


async def _settle_from_exit_code_output(
    db: Database,
    *,
    session: AgentSession,
    project_name: str,
    turn_no: int,
    workspace_rel: str,
    runner_server: str,
    ssh_run: SshRunCallable,
    raw_exit_code: str,
) -> Optional[AgentSessionTurnStatus]:
    output = raw_exit_code.strip()
    if not output:
        return None
    try:
        code = int(output.splitlines()[0].strip())
    except ValueError:
        code = None

    paths = build_dispatch_paths(workspace_rel, project_name, session.id, turn_no)
    final_text = ""
    cli_session_id: Optional[str] = None
    try:
        final_res = await ssh_run(
            runner_server,
            build_read_file_command(paths.final_message_file),
            AGENT_SESSION_SSH_PROBE_TIMEOUT,
        )
        final_text = final_res.stdout or ""
    except Exception:  # noqa: BLE001 - best-effort; sentinel already converged
        final_text = ""
    try:
        cli_res = await ssh_run(
            runner_server,
            build_read_file_command(paths.cli_session_id_file),
            AGENT_SESSION_SSH_PROBE_TIMEOUT,
        )
        cli_session_id = (cli_res.stdout or "").strip() or None
    except Exception:  # noqa: BLE001
        cli_session_id = None
    if cli_session_id is not None and not _CLI_SESSION_ID_RE.match(cli_session_id):
        # A malformed/truncated read is not trusted as a real CLI session id
        # — better to lose `--resume` continuity on the next turn than to
        # persist a garbage value that later fails validation mid-turn.
        cli_session_id = None

    if code == 0:
        content = final_text.strip() or "（此輪完成，但沒有擷取到任何回覆文字）"
        return await _finalize_turn(
            db,
            session,
            turn_no,
            status="done",
            content=content,
            exit_code=code,
            cli_session_id=cli_session_id,
        )
    detail_suffix = f"：{final_text.strip()}" if final_text.strip() else ""
    content = f"（此輪失敗，exit code {code}{detail_suffix}）"
    return await _finalize_turn(
        db,
        session,
        turn_no,
        status="failed",
        content=content,
        exit_code=code,
        cli_session_id=cli_session_id,
        detail=f"claude exited {code}",
    )


# ---------------------------------------------------------------------------
# DG-AGENT-SESSION-V1 P3 (docs/product/AGENT_SESSION_V1_PLAN.md §5 P3 step 1):
# the session diff — a read-only remote probe, not a mutation, so unlike the
# turn channel it needs no session-state claim/settle machinery of its own.
# ---------------------------------------------------------------------------

#: Same char cap the engineering-task diff endpoint uses
#: (`app.main.get_engineering_task_diff_endpoint`'s `max_chars=65536`) —
#: independently pinned here because the two surfaces are reviewed
#: separately even though they currently share the value.
AGENT_SESSION_DIFF_PREVIEW_MAX_CHARS = 65536


@dataclass(frozen=True)
class AgentSessionDiffResult:
    """Mirrors the response shape `GET /engineering-tasks/{task_id}/diff`
    already returns (`available`/`status`/`summary`/`patch`/`truncated`/
    `redacted`/`withheld`) so the existing diff-viewer rendering contract
    extends to AgentSession without inventing a second shape; `dirty_files`/
    `untracked_files` are additive (plan §5 P3: "nothing invisible")."""

    status: str
    available: bool
    summary: Optional[str] = None
    patch: Optional[str] = None
    truncated: bool = False
    redacted: bool = False
    withheld: bool = False
    dirty_files: tuple = ()
    untracked_files: tuple = ()
    detail: Optional[str] = None


def _diff_summary_text(patch: str) -> Optional[str]:
    """File/`+`/`-` counter, independently pinned but algorithmically
    identical to `app.main._engineering_diff_summary` — kept as a private
    duplicate rather than an import because `app.main` imports this module
    (importing back would be a cycle) and this is a small, stable, pure
    function."""

    if not patch:
        return None
    files: set[str] = set()
    additions = 0
    removals = 0
    for line in patch.splitlines():
        if line.startswith(("+++ ", "--- ")):
            name = line[4:].strip()
            if name.startswith(("a/", "b/")):
                name = name[2:]
            if name and name != "/dev/null":
                files.add(name)
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            removals += 1
    return f"{len(files)} 個檔案變更，+{additions} -{removals}"


def _parse_status_porcelain(raw: str) -> tuple[list[str], list[str]]:
    """Split `git status --porcelain` lines into (dirty, untracked) path
    lists. Porcelain v1 lines are `XY PATH` (or `XY ORIG -> PATH` for a
    rename, kept whole rather than split further — this is a display list,
    not a machine-consumed contract)."""

    dirty: list[str] = []
    untracked: list[str] = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:]
        if not path:
            continue
        if code == "??":
            untracked.append(path)
        else:
            dirty.append(path)
    return dirty, untracked


async def collect_agent_session_diff(
    db: Database,
    *,
    session: AgentSession,
    workspace_rel: str,
    runner_server: str,
    ssh_run: SshRunCallable,
) -> AgentSessionDiffResult:
    """Read-only remote diff of the session's persistent worktree (plan §5
    P3 step 1). base = the session's base ProjectVersion commit; target =
    current worktree state, so uncommitted edits Claude's Edit/Write tools
    made are visible without requiring a checkpoint commit first (plan:
    "include uncommitted changes"). An unreachable Runner degrades to
    `status="unreachable"` rather than raising (INV-SSH-7) — no session
    state is touched by this read-only probe either way."""

    if session.base_version_id is None:
        raise InvalidAgentSessionTurnInputError(
            "agent session has no base_version_id; cannot resolve a base commit"
        )
    version = db.get_project_version(session.base_version_id)
    if version is None:
        raise InvalidAgentSessionTurnInputError(
            f"base ProjectVersion {session.base_version_id!r} no longer exists"
        )
    base_commit = version.git_commit

    diff_cmd = build_diff_command(workspace_rel, session.id, base_commit)
    status_cmd = build_status_command(workspace_rel, session.id)

    try:
        diff_res = await ssh_run(
            runner_server, diff_cmd, AGENT_SESSION_SSH_PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001 - any SSH-layer exception means unreachable
        return AgentSessionDiffResult(
            status="unreachable", available=False, detail="runner unreachable"
        )
    diff_stdout = diff_res.stdout or ""
    if diff_stdout.strip() == AGENT_SESSION_NO_WORKSPACE_MARKER:
        return AgentSessionDiffResult(
            status="no_workspace",
            available=False,
            detail="session workspace has not been created yet (no turn has run)",
        )

    try:
        status_res = await ssh_run(
            runner_server, status_cmd, AGENT_SESSION_SSH_PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001
        return AgentSessionDiffResult(
            status="unreachable", available=False, detail="runner unreachable"
        )
    status_stdout = status_res.stdout or ""
    if status_stdout.strip() == AGENT_SESSION_NO_WORKSPACE_MARKER:
        status_stdout = ""
    dirty_files, untracked_files = _parse_status_porcelain(status_stdout)

    preview = redact_engineering_text(
        diff_stdout, max_chars=AGENT_SESSION_DIFF_PREVIEW_MAX_CHARS
    )
    if preview["withheld"]:
        return AgentSessionDiffResult(
            status="withheld",
            available=False,
            withheld=True,
            dirty_files=tuple(dirty_files),
            untracked_files=tuple(untracked_files),
            detail=preview.get("reason"),
        )
    patch_text = preview["content"] or ""
    return AgentSessionDiffResult(
        status="available",
        available=True,
        summary=_diff_summary_text(patch_text),
        patch=patch_text,
        truncated=bool(preview["truncated"]),
        redacted=bool(preview["redacted"]),
        withheld=False,
        dirty_files=tuple(dirty_files),
        untracked_files=tuple(untracked_files),
    )


# ---------------------------------------------------------------------------
# DG-AGENT-SESSION-CHECKPOINT (docs/DECISIONS.md 2026-08-24: A 核准;
# docs/DG_AGENT_SESSION_CHECKPOINT_DECISION.md §3): the checkpoint pipeline
# that packages a session worktree's current commit into a bundle Server A
# can verify and bridge into the existing, byte-unchanged
# `engineering_task_promote` promotion flow. This runs via one bounded
# `ssh_run` call (bundle packaging is fast; no tmux/sentinel needed, unlike a
# full Claude turn) plus one local rsync pull — never through the job queue,
# same D2 posture as the turn channel above.
# ---------------------------------------------------------------------------

#: Same protected-filename guard `app.approvals.build_coding_task_script()`'s
#: `legacy_finalize` fragment uses (see that function's docstring, "鐵律 8").
#: Reused byte-for-byte as regex *text* rather than imported: that fragment
#: lives inside a job-queue-specific mega-script string assembled only for a
#: dispatched `jobs` row (`$TASK_DIR`/`$REPO_DIR` set up by a preamble this
#: session-checkpoint script does not have), not exposed as a standalone
#: importable function. This is deliberately the ONLY security-relevant
#: check the checkpoint pipeline performs in this slice — narrower than the
#: v2 `path-policy-verifier.py` allowed/prohibited-path contract, which
#: requires a structured request AgentSession does not have in V1. A change
#: to one regex without the other is a reviewable diff, not a silent drift.
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

    remote_bundle_path = checkpoint_bundle_remote_path(workspace_rel, session.id)
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
