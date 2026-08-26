"""DG-ASSISTANT-CLAUDE-TURN v1 C1 (docs/DECISIONS.md 2026-08-26,
`compiled-prancing-salamander.md` plan §C1): the assistant chat-turn
execution channel — WS `/ws` answers a user message with exactly one bounded
`claude -p` turn run on the Runner already used for Codex (`config.
codex_runner_server`).

This module is deliberately small and independent of
`app.agent_session_turns`: it reuses that module's *shape* (tmux one-shot +
`exit_code` sentinel + `env -i` + dedicated empty cwd + pinned CLI version
range from `app.coding_agents`) but not its code, because the confinement
here is strictly tighter (D3/D4 in that module vs. **zero tools, zero
platform access** here — no `--add-dir`, no file/Bash tool grants at all,
`claude -p` is asked a plain-text question and gives a plain-text answer,
nothing else). Pure script/path assembly is separated from the one function
that actually does I/O (`run_assistant_turn`, injected `ssh_run`/
`ssh_write_file`, same calling convention as
`app.agent_session_turns.launch_agent_session_turn`) so the shell string is
golden-string-testable without any SSH.

**Confinement flags chosen (documented here, not just in code comments)**:
    - `--allowedTools ""` — the *tightest expressible form* of "zero tools"
      given the CLI's allow-list design (`app.agent_session_turns`'s D4
      confinement always grants at least `Read Edit Write Grep Glob`; an
      empty string is the same mechanism denying everything, not a
      different, unreviewed flag).
    - No `--permission-mode` flag at all (unlike AgentSession's
      `acceptEdits`) — we want no implicit accept-anything semantics; with
      an empty allow-list there is nothing left to accept regardless.
    - No `--add-dir` — the invocation's cwd is a brand-new, dedicated, empty
      per-turn directory (`.../cwd`), never the Runner's `$HOME` and never a
      project workspace; even if a future CLI version silently expanded
      what an empty `--allowedTools` permits, there is nothing sensitive to
      reach from that directory.
    - `env -i HOME="$HOME" PATH="$EXTENDED_PATH" USER="$USER" LANG=C.UTF-8
      LC_ALL=C.UTF-8 TERM=dumb` — identical shape to
      `app.agent_session_turns.build_turn_script()`'s invocation (a bare
      `env -i HOME PATH USER LANG LC_ALL TERM=dumb` without `=value` is not
      valid POSIX `env` syntax — unassigned bare names after `-i` are parsed
      as the command to execute, not "inherit this variable" — so the
      explicit-assignment form is used here too). `EXTENDED_PATH` is the
      preflight-computed PATH after `app.coding_agents.PATH_EXTENSION_FRAGMENT`
      widens it with `$HOME/.local/bin`, `$HOME/bin`, `$HOME/.npm-global/bin`,
      and any nvm-managed `node/*/bin` (real-runner job 96 diagnosis: `claude`
      lives in `~/.local/bin`, which a non-interactive SSH shell's default
      `PATH` omits) — forwarded explicitly because `env -i` discards the
      inherited, un-widened `$PATH` otherwise.
    - `--output-format json`, no `--verbose`, no `stream-json` — a single
      one-shot turn has no need for an event stream; the whole reply is one
      JSON document read back after the process exits.

**Preflight reason surfacing (fixes the AgentSession gap)**: `fail()` in the
AgentSession turn script only exports `R_ERROR` inside the same shell
process that then exits — nothing persists that string anywhere the
orchestration layer can read it back after the process is gone. Here every
`fail()` call additionally writes its message to a dedicated `reason.txt`
next to the `exit_code` sentinel *before* exiting, and the first token of
that message (`NOT_INSTALLED:`/`VERSION_UNSUPPORTED:`/`NOT_LOGGED_IN:`/
`ROOT_REFUSED:`/`INTERNAL:`) is a stable, English, machine-classifiable tag
the Python side switches on — never the raw claude/git output, so nothing
sensitive (account email, tokens) can leak through it.

INV-SSH-2/3: the user's message (and bounded history) reach `claude` only
through `< "$HOME"/prompt.txt` stdin redirect, written independently via the
existing `ssh_write_file()` SFTP path — never interpolated into this shell
string. INV-LLM-1/2/3: this channel has no tool of any kind, so it cannot
execute anything, approve anything, or read/write platform state; it can
only turn text into text.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

from app.coding_agents import (
    CLAUDE_CODE_CLI_MAX_VERSION_EXCLUSIVE,
    CLAUDE_CODE_CLI_MIN_VERSION,
    PATH_EXTENSION_FRAGMENT,
)

#: Runner-home-relative base directory for every assistant-chat turn's
#: artifacts (mirrors `app.agent_session_turns.AGENT_SESSION_WORKSPACES_SUBDIR`
#: — no `~/` prefix, SFTP does no tilde expansion).
ASSISTANT_CHAT_SUBDIR = "assistant_chat"

#: One bounded turn's hard ceiling (plan §C1: "timeout 用 120s").
ASSISTANT_TURN_TIMEOUT_SEC = 120

#: Local poll-loop grace beyond the remote `timeout <n>s` wrapper, same
#: rationale/value as `app.agent_session_turns.
#: AGENT_SESSION_TURN_TIMEOUT_GRACE_SEC` — the remote `timeout` should
#: always win the race; this only guards against a wedged SSH channel.
ASSISTANT_TURN_TIMEOUT_GRACE_SEC = 30

#: Single lazy-settle SSH probe budget, identical constant to
#: `app.agent_session_turns.AGENT_SESSION_SSH_PROBE_TIMEOUT`.
ASSISTANT_SSH_PROBE_TIMEOUT = 15.0

#: Default busy-poll interval while waiting for the sentinel to appear.
#: Deliberately small — the loop's exit is driven by an injected `sleep`
#: callable (tests pass a no-op) so this never makes a real test slow.
ASSISTANT_TURN_POLL_INTERVAL_SEC = 2.0

_SESSION_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


class InvalidAssistantTurnInputError(ValueError):
    """A caller passed a value that is not a validated identifier."""


def _require_session_key(session_key: str) -> str:
    if not isinstance(session_key, str) or not _SESSION_KEY_RE.match(session_key):
        raise InvalidAssistantTurnInputError(f"invalid session_key: {session_key!r}")
    return session_key


def _require_turn_no(turn_no: int) -> int:
    if not isinstance(turn_no, int) or isinstance(turn_no, bool) or turn_no < 1:
        raise InvalidAssistantTurnInputError(f"invalid turn_no: {turn_no!r}")
    return turn_no


def tmux_session_name(session_key: str, turn_no: int) -> str:
    """`assistant_chat_{key8}_{n}` — short, bounded, same rationale as
    `app.agent_session_turns.tmux_session_name()`."""

    session_key = _require_session_key(session_key)
    turn_no = _require_turn_no(turn_no)
    return f"assistant_chat_{session_key[:8]}_{turn_no}"


@dataclass(frozen=True)
class AssistantTurnPaths:
    turn_dir: str
    cwd_dir: str
    prompt_file: str
    reply_file: str
    exit_code_file: str
    reason_file: str
    log_file: str


def build_dispatch_paths(
    workspace_rel: str, session_key: str, turn_no: int
) -> AssistantTurnPaths:
    session_key = _require_session_key(session_key)
    turn_no = _require_turn_no(turn_no)
    workspace_rel = (workspace_rel or "").strip("/")
    base = f"{ASSISTANT_CHAT_SUBDIR}/{session_key}"
    if workspace_rel:
        base = f"{workspace_rel}/{base}"
    turn_dir = f"{base}/turns/{turn_no}"
    return AssistantTurnPaths(
        turn_dir=turn_dir,
        cwd_dir=f"{turn_dir}/cwd",
        prompt_file=f"{turn_dir}/prompt.txt",
        reply_file=f"{turn_dir}/reply.json",
        exit_code_file=f"{turn_dir}/exit_code",
        reason_file=f"{turn_dir}/reason.txt",
        log_file=f"{turn_dir}/turn.log",
    )


def build_mkdir_command(workspace_rel: str, session_key: str, turn_no: int) -> str:
    paths = build_dispatch_paths(workspace_rel, session_key, turn_no)
    return f"mkdir -p {shlex.quote(paths.turn_dir)}"


def build_launch_command(session_key: str, turn_no: int, run_sh_path: str) -> str:
    name = tmux_session_name(session_key, turn_no)
    return f"tmux new-session -d -s {shlex.quote(name)} 'bash {shlex.quote(run_sh_path)}'"


def build_tmux_check_command(session_key: str, turn_no: int) -> str:
    name = shlex.quote(tmux_session_name(session_key, turn_no))
    return f"tmux has-session -t {name} 2>/dev/null && echo EXISTS || echo GONE"


def build_check_exit_code_command(
    workspace_rel: str, session_key: str, turn_no: int
) -> str:
    paths = build_dispatch_paths(workspace_rel, session_key, turn_no)
    return f"cat {shlex.quote(paths.exit_code_file)} 2>/dev/null"


def build_read_file_command(path: str, *, max_bytes: int = 65536) -> str:
    """Bounded read of a small sentinel-adjacent file (reply.json/
    reason.txt) — same `tail -c` byte cap discipline as
    `app.agent_session_turns.build_read_file_command()`."""

    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise InvalidAssistantTurnInputError(f"invalid max_bytes: {max_bytes!r}")
    return f"cat {shlex.quote(path)} 2>/dev/null | tail -c {max_bytes}"


# ---------------------------------------------------------------------------
# Prompt assembly (pure)
# ---------------------------------------------------------------------------

#: Told to the model on every turn: it is a Dispatch Center assistant, it has
#: zero tools/execution ability, and job requests must go through the
#: platform's own approval flow (「跑 <指令>」語法或到工作區操作).
ASSISTANT_SYSTEM_PREAMBLE = (
    "你是 Dispatch Center 的助手。你完全無法執行任何指令、無法存取任何檔案、"
    "伺服器或系統狀態，也沒有任何工具可用——這是一個單純的文字問答回合。"
    "如果使用者想要跑任務，請回覆請使用者用「跑 <指令>」語法或到工作區操作，"
    "那個請求會走平台既有的核准流程，你自己完全無法代為核准或執行。"
)


def build_assistant_prompt(history: Optional[Sequence[dict]], user_text: str) -> str:
    """Bounded conversation history (already trimmed by the caller, see
    `app.agent_runtime.trim_history()`) + the new user message + the system
    preamble, as one plain-text document — this is the entire content of
    `prompt.txt`, never interpolated into the shell (INV-SSH-2/3)."""

    lines = [ASSISTANT_SYSTEM_PREAMBLE]
    for turn in history or []:
        role = "使用者" if turn.get("role") == "user" else "助手"
        content = turn.get("content") or ""
        lines.append(f"{role}：{content}")
    lines.append(f"使用者：{user_text}")
    lines.append("助手：")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Script assembly (pure)
# ---------------------------------------------------------------------------


def build_assistant_turn_script(
    *,
    session_key: str,
    turn_no: int,
    workspace_rel: str,
    turn_timeout_sec: int = ASSISTANT_TURN_TIMEOUT_SEC,
) -> str:
    """Assemble the full `run.sh` for exactly one assistant chat turn."""

    session_key = _require_session_key(session_key)
    turn_no = _require_turn_no(turn_no)
    if not isinstance(turn_timeout_sec, int) or turn_timeout_sec <= 0:
        raise InvalidAssistantTurnInputError(
            f"invalid turn_timeout_sec: {turn_timeout_sec!r}"
        )

    paths = build_dispatch_paths(workspace_rel, session_key, turn_no)
    q_prompt = shlex.quote(paths.prompt_file)
    q_reply = shlex.quote(paths.reply_file)
    q_cwd = shlex.quote(paths.cwd_dir)
    q_reason = shlex.quote(paths.reason_file)

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
        "  command -v claude >/dev/null 2>&1 || "
        "fail 'NOT_INSTALLED: claude CLI not installed on this Runner'\n"
        '  R_CLI_VERSION="$(claude --version 2>/dev/null | head -1)"\n'
        '  CLI_VERSION_NUM="$(printf \'%s\\n\' "$R_CLI_VERSION" | '
        "grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1)\"\n"
        "  [ -n \"$CLI_VERSION_NUM\" ] || "
        "fail 'VERSION_UNSUPPORTED: claude CLI version could not be parsed'\n"
        f"  awk -v v=\"$CLI_VERSION_NUM\" 'BEGIN{{split(v,a,\".\");"
        f"n=a[1]*1000000+a[2]*1000+a[3]; if (n>={min_bound} && n<{max_bound}) "
        "exit 0; exit 1}' || "
        "fail 'VERSION_UNSUPPORTED: claude CLI version outside the reviewed "
        "compatible range'\n"
        "  claude auth status >/dev/null 2>&1 || "
        "fail 'NOT_LOGGED_IN: claude is not logged in on this Runner'\n"
        "  [ \"$(id -u)\" != \"0\" ] || "
        "fail 'ROOT_REFUSED: refusing to run claude as root'\n"
    )

    turn_invocation = (
        "  # confinement: brand-new dedicated empty cwd (never $HOME, never a\n"
        "  # project workspace); zero tools (--allowedTools \"\"); env -i drops\n"
        "  # every inherited variable except the five explicitly re-set below.\n"
        f"  mkdir -p {q_cwd} || fail 'INTERNAL: cannot create dedicated empty cwd'\n"
        f"  [ -s {q_prompt} ] || "
        "fail 'INTERNAL: prompt.txt missing (should have been SFTP-written "
        "before launch)'\n"
        f"  ( cd {q_cwd} && exec env -i HOME=\"$HOME\" PATH=\"$EXTENDED_PATH\" "
        "USER=\"$USER\" LANG=C.UTF-8 LC_ALL=C.UTF-8 TERM=dumb \\\n"
        f"    timeout {int(turn_timeout_sec)}s claude -p --output-format json "
        "--allowedTools \"\" \\\n"
        f"    < \"$HOME\"/{q_prompt} > \"$HOME\"/{q_reply} )\n"
        "  R_CLAUDE_EXIT=$?\n"
    )

    script = r"""set -u
TURN_DIR="__TURN_DIR__"
REASON_FILE="__REASON_FILE__"
log() { echo "[assistant_turn] $*"; }
fail() { printf '%s' "$1" > "$REASON_FILE" 2>/dev/null || true; log "FAIL: $1"; exit 1; }

main() {
  mkdir -p "$TURN_DIR" || { log "cannot create TURN_DIR"; exit 1; }
__PREFLIGHT_BLOCK__
__TURN_INVOCATION_BLOCK__
  exit "$R_CLAUDE_EXIT"
}
main 2>&1 | tee __LOG_FILE__
EXIT_CODE="${PIPESTATUS[0]}"
echo "$EXIT_CODE" > __EXIT_CODE_FILE__
exit "$EXIT_CODE"
"""
    script = script.replace("__TURN_DIR__", paths.turn_dir)
    script = script.replace("__REASON_FILE__", paths.reason_file)
    script = script.replace("__PREFLIGHT_BLOCK__", preflight.rstrip("\n"))
    script = script.replace("__TURN_INVOCATION_BLOCK__", turn_invocation.rstrip("\n"))
    script = script.replace("__LOG_FILE__", paths.log_file)
    script = script.replace("__EXIT_CODE_FILE__", paths.exit_code_file)
    return script


# ---------------------------------------------------------------------------
# Orchestration (the only I/O in this module) — injected `ssh_run`/
# `ssh_write_file` callables, same shape as
# `app.agent_session_turns`'s orchestration functions.
# ---------------------------------------------------------------------------

SshRunCallable = Callable[[str, str, float], Awaitable[Any]]
SshWriteFileCallable = Callable[[str, str, str], Awaitable[Any]]
SleepCallable = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class AssistantTurnResult:
    """Outcome of one bounded assistant chat turn.

    - ``"ok"``: the turn completed and produced `text`.
    - ``"unreachable"``: the Runner could not be reached (INV-SSH-7 —
      degraded, not a failure judgment).
    - ``"not_logged_in"``: preflight failed specifically because `claude` is
      not logged in on the Runner; `reason` is the human-readable message.
    - ``"timeout"``: no sentinel appeared within `turn_timeout_sec` + grace.
    - ``"failed"``: any other typed failure (not installed, unsupported
      version, refused to run as root, claude itself exited non-zero, or the
      reply could not be parsed); `reason` explains why.
    """

    status: str
    text: Optional[str] = None
    reason: Optional[str] = None


def _parse_reply_json(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Defensively extract `(text, error)` from `claude -p --output-format
    json`'s stdout. Returns `(text, None)` on a recognizable success shape,
    `(None, reason)` otherwise — never raises."""

    stripped = (raw or "").strip()
    if not stripped:
        return None, "reply.json is empty"
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        # Some CLI builds may write plain text despite the flag; fall back to
        # the raw content rather than treating it as an unconditional failure.
        return stripped, None
    if not isinstance(data, dict):
        return None, "reply.json top level is not a JSON object"
    if data.get("is_error"):
        result = data.get("result")
        return None, result if isinstance(result, str) and result else "claude reported is_error=true"
    result = data.get("result")
    if isinstance(result, str):
        return result, None
    return None, "reply.json has no string 'result' field"


def _classify_reason_file(raw: str) -> tuple[str, str]:
    """`(status, reason)` from the preflight `reason.txt` tag (see module
    docstring's tag list) — falls back to a generic 'failed' classification
    for any untagged/unknown content instead of guessing."""

    text = (raw or "").strip()
    if text.startswith("NOT_LOGGED_IN:"):
        return "not_logged_in", text.split(":", 1)[1].strip()
    if text:
        return "failed", text
    return "failed", "claude turn failed (no reason recorded)"


async def run_assistant_turn(
    *,
    runner_server: str,
    workspace_rel: str,
    session_key: str,
    turn_no: int,
    history: Optional[Sequence[dict]],
    user_text: str,
    ssh_run: SshRunCallable,
    ssh_write_file: SshWriteFileCallable,
    turn_timeout_sec: int = ASSISTANT_TURN_TIMEOUT_SEC,
    poll_interval_sec: float = ASSISTANT_TURN_POLL_INTERVAL_SEC,
    sleep: SleepCallable,
) -> AssistantTurnResult:
    """Launch one bounded turn and block until it converges (or times out).

    Unlike `app.agent_session_turns`'s lazy per-request settle (an
    AgentSession turn must survive page reloads and is polled by a *later*
    request), a WS chat turn is answered synchronously within the same
    message handler — there is no separate "check status" call, so this
    function itself busy-polls the sentinel via the injected `sleep`
    callable (tests inject a no-op so the loop runs instantly; `elapsed` is
    tracked as `iterations * poll_interval_sec`, never wall-clock time, so
    no test needs to wait out a real 120s window).
    """

    session_key = _require_session_key(session_key)
    turn_no = _require_turn_no(turn_no)
    paths = build_dispatch_paths(workspace_rel, session_key, turn_no)
    prompt = build_assistant_prompt(history, user_text)
    script = build_assistant_turn_script(
        session_key=session_key,
        turn_no=turn_no,
        workspace_rel=workspace_rel,
        turn_timeout_sec=turn_timeout_sec,
    )
    run_sh_path = f"{paths.turn_dir}/run.sh"

    try:
        await ssh_run(
            runner_server,
            build_mkdir_command(workspace_rel, session_key, turn_no),
            ASSISTANT_SSH_PROBE_TIMEOUT,
        )
        await ssh_write_file(runner_server, paths.prompt_file, prompt)
        await ssh_write_file(runner_server, run_sh_path, script)
        await ssh_run(
            runner_server,
            build_launch_command(session_key, turn_no, run_sh_path),
            ASSISTANT_SSH_PROBE_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 - any SSH-layer exception means unreachable
        return AssistantTurnResult(status="unreachable")

    exit_code_cmd = build_check_exit_code_command(workspace_rel, session_key, turn_no)
    deadline = turn_timeout_sec + ASSISTANT_TURN_TIMEOUT_GRACE_SEC
    elapsed = 0.0

    while True:
        try:
            exit_res = await ssh_run(
                runner_server, exit_code_cmd, ASSISTANT_SSH_PROBE_TIMEOUT
            )
        except Exception:  # noqa: BLE001
            return AssistantTurnResult(status="unreachable")

        settled = await _settle_from_exit_code_output(
            runner_server=runner_server,
            paths=paths,
            ssh_run=ssh_run,
            raw_exit_code=(exit_res.stdout or ""),
        )
        if settled is not None:
            return settled

        try:
            tmux_res = await ssh_run(
                runner_server,
                build_tmux_check_command(session_key, turn_no),
                ASSISTANT_SSH_PROBE_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            return AssistantTurnResult(status="unreachable")

        if "EXISTS" not in (tmux_res.stdout or ""):
            # tmux is gone — re-check exit_code once to close the race window
            # (same rationale as app.agent_session_turns.converge_...()).
            try:
                exit_res2 = await ssh_run(
                    runner_server, exit_code_cmd, ASSISTANT_SSH_PROBE_TIMEOUT
                )
            except Exception:  # noqa: BLE001
                return AssistantTurnResult(status="unreachable")
            settled = await _settle_from_exit_code_output(
                runner_server=runner_server,
                paths=paths,
                ssh_run=ssh_run,
                raw_exit_code=(exit_res2.stdout or ""),
            )
            if settled is not None:
                return settled
            return AssistantTurnResult(
                status="failed",
                reason="tmux session gone without an exit_code (INV-SSH-6)",
            )

        if elapsed >= deadline:
            return AssistantTurnResult(status="timeout")
        await sleep(poll_interval_sec)
        elapsed += poll_interval_sec


async def _settle_from_exit_code_output(
    *,
    runner_server: str,
    paths: AssistantTurnPaths,
    ssh_run: SshRunCallable,
    raw_exit_code: str,
) -> Optional[AssistantTurnResult]:
    output = raw_exit_code.strip()
    if not output:
        return None
    try:
        code = int(output.splitlines()[0].strip())
    except ValueError:
        return AssistantTurnResult(
            status="failed", reason=f"exit_code file has unparseable content: {output!r}"
        )

    if code == 0:
        try:
            reply_res = await ssh_run(
                runner_server,
                build_read_file_command(paths.reply_file),
                ASSISTANT_SSH_PROBE_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            return AssistantTurnResult(status="unreachable")
        text, error = _parse_reply_json(reply_res.stdout or "")
        if error is not None:
            return AssistantTurnResult(status="failed", reason=error)
        return AssistantTurnResult(status="ok", text=text or "")

    if code == 124:
        return AssistantTurnResult(status="timeout")

    try:
        reason_res = await ssh_run(
            runner_server,
            build_read_file_command(paths.reason_file),
            ASSISTANT_SSH_PROBE_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return AssistantTurnResult(status="unreachable")
    status, reason = _classify_reason_file(reason_res.stdout or "")
    return AssistantTurnResult(status=status, reason=reason)


__all__ = [
    "ASSISTANT_CHAT_SUBDIR",
    "ASSISTANT_SSH_PROBE_TIMEOUT",
    "ASSISTANT_SYSTEM_PREAMBLE",
    "ASSISTANT_TURN_POLL_INTERVAL_SEC",
    "ASSISTANT_TURN_TIMEOUT_GRACE_SEC",
    "ASSISTANT_TURN_TIMEOUT_SEC",
    "AssistantTurnPaths",
    "AssistantTurnResult",
    "InvalidAssistantTurnInputError",
    "build_assistant_prompt",
    "build_assistant_turn_script",
    "build_check_exit_code_command",
    "build_dispatch_paths",
    "build_launch_command",
    "build_mkdir_command",
    "build_read_file_command",
    "build_tmux_check_command",
    "run_assistant_turn",
    "tmux_session_name",
]
