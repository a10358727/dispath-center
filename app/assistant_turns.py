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
from dataclasses import dataclass, field, replace
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

#: Packet D2: same shape as `app.config.ASSISTANT_MODEL_NAME_RE`, duplicated
#: here (not imported) so this module keeps its existing zero-dependency-on-
#: `app.config` shape -- `build_assistant_turn_script()` is pure/golden-string
#: tested without any config object.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")


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
    #: DG-ASSISTANT-TOOLS v1 (tools-enabled turns only; always computed).
    bridge_file: str = ""
    tools_config_file: str = ""
    token_file: str = ""
    mcp_config_file: str = ""
    calls_log_file: str = ""
    tools_reason_file: str = ""


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
        bridge_file=f"{turn_dir}/bridge.py",
        tools_config_file=f"{turn_dir}/tools.json",
        token_file=f"{turn_dir}/token",
        mcp_config_file=f"{turn_dir}/mcp.json",
        calls_log_file=f"{turn_dir}/tool_calls.jsonl",
        tools_reason_file=f"{turn_dir}/tools_reason.txt",
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
# DG-ASSISTANT-TOOLS v1: per-turn platform tools via the MCP bridge (pure)
# ---------------------------------------------------------------------------

#: MCP server name the bridge is registered under for `claude -p`; tool names
#: become `mcp__dispatch__<tool>` (T-1).
ASSISTANT_TOOLS_SERVER_NAME = "dispatch"
#: Hard cap on agentic turns inside one chat turn (belt-and-braces next to the
#: bridge's own per-turn call cap, T-6).
ASSISTANT_TOOLS_MAX_TURNS = 12
ASSISTANT_TOOLS_MAX_CALL_LOG_ENTRIES = 64
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")
_RUNNER_PYTHON_RE = re.compile(r"^[A-Za-z0-9._/-]{1,128}\Z")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{16,256}\Z")


@dataclass(frozen=True)
class AssistantToolsSpec:
    """Everything a tools-enabled turn needs; the bearer is memory-only.

    - ``dispatch_base_url``: how the bridge on the runner reaches Server A.
    - ``runner_python``: interpreter that runs the shipped bridge (PATH name
      or absolute path; validated, then shell-quoted by the script builder).
    - ``tool_names``: the closed set of bridge tools allowed this turn.
    - ``token``: the per-turn ``dat_`` bearer (T-2); written to its own file
      over SFTP, never into a shell string, never logged.
    """

    dispatch_base_url: str
    runner_python: str
    max_calls: int
    tool_names: tuple[str, ...]
    token: str = field(repr=False)
    source: str = "assistant"

    def __post_init__(self) -> None:
        url = self.dispatch_base_url
        if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
            raise InvalidAssistantTurnInputError("dispatch_base_url must be an http(s) URL")
        if any(ch in url for ch in "\"'\\\n\r ") :
            raise InvalidAssistantTurnInputError("dispatch_base_url contains forbidden characters")
        if not isinstance(self.runner_python, str) or not _RUNNER_PYTHON_RE.match(self.runner_python):
            raise InvalidAssistantTurnInputError("runner_python has an invalid shape")
        if isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int) or not 1 <= self.max_calls <= 16:
            raise InvalidAssistantTurnInputError("max_calls must be between 1 and 16")
        if not self.tool_names or any(not _TOOL_NAME_RE.match(n) for n in self.tool_names):
            raise InvalidAssistantTurnInputError("tool_names must be non-empty lowercase identifiers")
        if not isinstance(self.token, str) or not _TOKEN_RE.match(self.token):
            raise InvalidAssistantTurnInputError("token has an invalid shape")
        if not isinstance(self.source, str) or not re.match(r"^[a-z]{1,32}\Z", self.source):
            raise InvalidAssistantTurnInputError("source must be a short lowercase label")


def build_tools_config_json(spec: AssistantToolsSpec) -> str:
    """`tools.json` for `mcp_bridge.py --stdio`: sibling file names only."""

    return json.dumps(
        {
            "dispatch_base_url": spec.dispatch_base_url.rstrip("/"),
            "token_file": "token",
            "calls_log": "tool_calls.jsonl",
            "max_calls": int(spec.max_calls),
            "source": spec.source,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def build_mcp_config_json(spec: AssistantToolsSpec) -> str:
    """`mcp.json` for `claude -p --mcp-config`: the bridge runs as a stdio
    server; paths are relative to claude's cwd (`<turn_dir>/cwd`)."""

    return json.dumps(
        {
            "mcpServers": {
                ASSISTANT_TOOLS_SERVER_NAME: {
                    "type": "stdio",
                    "command": spec.runner_python,
                    "args": ["../bridge.py", "--stdio", "--config", "../tools.json"],
                }
            }
        },
        sort_keys=True,
    )


def build_allowed_tools_value(spec: AssistantToolsSpec) -> str:
    """Comma-separated `--allowedTools` value: only the bridge tools, by name."""

    return ",".join(f"mcp__{ASSISTANT_TOOLS_SERVER_NAME}__{name}" for name in spec.tool_names)


def _parse_calls_log(raw: str) -> tuple[dict, ...]:
    entries: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or not isinstance(item.get("tool"), str):
            continue
        entry: dict = {"tool": item["tool"], "status": str(item.get("status") or "unknown")}
        approval_id = item.get("approval_id")
        if isinstance(approval_id, int) and not isinstance(approval_id, bool):
            entry["approval_id"] = approval_id
        if item.get("auto_approved") is True:
            entry["auto_approved"] = True
        entries.append(entry)
        if len(entries) >= ASSISTANT_TOOLS_MAX_CALL_LOG_ENTRIES:
            break
    return tuple(entries)


def _parse_tools_reason(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("TOOLS_UNAVAILABLE:"):
        text = text[len("TOOLS_UNAVAILABLE:") :].strip()
    return text[:200] or None


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


#: Tools-enabled turns (DG-ASSISTANT-TOOLS v1 T-3/T-5): the model may query the
#: platform and create pending approval cards, but never decides or executes.
ASSISTANT_TOOLS_PREAMBLE = (
    "你是 Dispatch Center 的助手。你可以透過 dispatch 工具查詢平台狀態（伺服器、"
    "任務、核准、專案、資料集等），也可以用 request_* 工具替使用者建立「待核准」的請求卡；"
    "你永遠不能核准、拒絕或執行任何東西，所有動作都由人在核准頁決定。需要平台資料時"
    "先查再答，回覆中說明依據；建立請求卡後告知卡號並提醒使用者到核准頁決定。"
    "本回合工具呼叫有上限，請精簡使用。"
)


def build_assistant_prompt(
    history: Optional[Sequence[dict]], user_text: str, *, tools_enabled: bool = False
) -> str:
    """Bounded conversation history (already trimmed by the caller, see
    `app.agent_runtime.trim_history()`) + the new user message + the system
    preamble, as one plain-text document — this is the entire content of
    `prompt.txt`, never interpolated into the shell (INV-SSH-2/3)."""

    lines = [ASSISTANT_TOOLS_PREAMBLE if tools_enabled else ASSISTANT_SYSTEM_PREAMBLE]
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
    model: Optional[str] = None,
    tools: Optional[AssistantToolsSpec] = None,
) -> str:
    """Assemble the full `run.sh` for exactly one assistant chat turn.

    `tools` (DG-ASSISTANT-TOOLS v1): when given, the script preflights the
    runner python + `mcp`/`httpx` packages and the four SFTP-written tool
    files; on any miss it records `TOOLS_UNAVAILABLE: …` and runs the exact
    zero-tool invocation instead (T-4 degradation). When tools are usable it
    launches `claude -p` with `--mcp-config … --strict-mcp-config
    --allowedTools <bridge tools only> --max-turns N`. Without `tools` the
    script is byte-identical to the pre-tools version.

    Packet D2: `model` (`config.assistant_claude_model`, already validated
    against `app.config.ASSISTANT_MODEL_NAME_RE` at config load and at the
    `POST /api/v2/ai-providers/assistant-model` endpoint) adds
    `--model '<value>'` to the `claude -p` invocation when truthy; `None`/
    empty leaves the CLI's own default model in effect (byte-identical to
    the pre-D2 script). Re-validated here too (INV-SSH-3: this is the one
    place the value actually lands in a shell string), so a caller that
    somehow bypasses both upstream checks still cannot inject shell syntax
    through this flag."""

    session_key = _require_session_key(session_key)
    turn_no = _require_turn_no(turn_no)
    if not isinstance(turn_timeout_sec, int) or turn_timeout_sec <= 0:
        raise InvalidAssistantTurnInputError(
            f"invalid turn_timeout_sec: {turn_timeout_sec!r}"
        )
    if model and not _MODEL_NAME_RE.match(model):
        raise InvalidAssistantTurnInputError(f"invalid model: {model!r}")

    paths = build_dispatch_paths(workspace_rel, session_key, turn_no)
    q_prompt = shlex.quote(paths.prompt_file)
    q_reply = shlex.quote(paths.reply_file)
    q_cwd = shlex.quote(paths.cwd_dir)

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

    # Packet D2: `--model` is only ever added when `model` is truthy (already
    # validated above) -- absent, the invocation is byte-identical to the
    # pre-D2 script (existing golden pins keep passing unmodified).
    model_flag = f" --model {shlex.quote(model)}" if model else ""

    zero_tool_exec = (
        f"  ( cd {q_cwd} && exec env -i HOME=\"$HOME\" PATH=\"$EXTENDED_PATH\" "
        "USER=\"$USER\" LANG=C.UTF-8 LC_ALL=C.UTF-8 TERM=dumb \\\n"
        f"    timeout {int(turn_timeout_sec)}s claude -p --output-format json "
        f"--allowedTools \"\"{model_flag} \\\n"
        f"    < \"$HOME\"/{q_prompt} > \"$HOME\"/{q_reply} )\n"
    )
    tools_vars = ""
    fail_cleanup = ""
    tools_preflight = ""
    if tools is not None:
        q_py = shlex.quote(tools.runner_python)
        q_bridge = shlex.quote(paths.bridge_file)
        q_tools_cfg = shlex.quote(paths.tools_config_file)
        q_token = shlex.quote(paths.token_file)
        q_mcp = shlex.quote(paths.mcp_config_file)
        q_allowed = shlex.quote(build_allowed_tools_value(tools))
        tools_vars = (
            f'TOOLS_REASON_FILE="{paths.tools_reason_file}"\n'
            f'TOKEN_FILE="{paths.token_file}"\n'
        )
        fail_cleanup = 'rm -f "$TOKEN_FILE" 2>/dev/null || true; '
        tools_preflight = (
            "  # DG-ASSISTANT-TOOLS v1: tools are optional -- any miss degrades to\n"
            "  # the zero-tool turn and records why (T-4).\n"
            "  TOOLS_ENABLED=1\n"
            "  tools_off() { printf '%s' \"$1\" > \"$TOOLS_REASON_FILE\" 2>/dev/null || true; "
            "log \"TOOLS OFF: $1\"; TOOLS_ENABLED=0; }\n"
            f"  command -v {q_py} >/dev/null 2>&1 || "
            "tools_off 'TOOLS_UNAVAILABLE: runner python not found'\n"
            f"  [ \"$TOOLS_ENABLED\" = 1 ] && {{ {q_py} -c 'import mcp, httpx' >/dev/null 2>&1 || "
            "tools_off 'TOOLS_UNAVAILABLE: python packages mcp/httpx missing on runner'; }\n"
            f"  [ \"$TOOLS_ENABLED\" = 1 ] && {{ [ -s {q_bridge} ] && [ -s {q_tools_cfg} ] && "
            f"[ -s {q_token} ] && [ -s {q_mcp} ] || "
            "tools_off 'TOOLS_UNAVAILABLE: tool files missing'; }\n"
            f"  chmod 600 {q_token} {q_tools_cfg} 2>/dev/null || true\n"
        )
        tools_exec = (
            f"  ( cd {q_cwd} && exec env -i HOME=\"$HOME\" PATH=\"$EXTENDED_PATH\" "
            "USER=\"$USER\" LANG=C.UTF-8 LC_ALL=C.UTF-8 TERM=dumb \\\n"
            f"    timeout {int(turn_timeout_sec)}s claude -p --output-format json "
            f"--mcp-config \"$HOME\"/{q_mcp} --strict-mcp-config "
            f"--allowedTools {q_allowed} --max-turns {ASSISTANT_TOOLS_MAX_TURNS}{model_flag} \\\n"
            f"    < \"$HOME\"/{q_prompt} > \"$HOME\"/{q_reply} )\n"
        )
        turn_invocation = (
            "  # confinement: brand-new dedicated empty cwd (never $HOME, never a\n"
            "  # project workspace); env -i drops every inherited variable except the\n"
            "  # five explicitly re-set below; tools only through the stdio bridge.\n"
            f"  mkdir -p {q_cwd} || fail 'INTERNAL: cannot create dedicated empty cwd'\n"
            f"  [ -s {q_prompt} ] || "
            "fail 'INTERNAL: prompt.txt missing (should have been SFTP-written "
            "before launch)'\n"
            "  if [ \"$TOOLS_ENABLED\" = 1 ]; then\n"
            + tools_exec
            + "    R_CLAUDE_EXIT=$?\n"
            "  else\n"
            + zero_tool_exec
            + "    R_CLAUDE_EXIT=$?\n"
            "  fi\n"
            f"  rm -f {q_token} 2>/dev/null || true\n"
        )
    else:
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
        f"--allowedTools \"\"{model_flag} \\\n"
        f"    < \"$HOME\"/{q_prompt} > \"$HOME\"/{q_reply} )\n"
        "  R_CLAUDE_EXIT=$?\n"
    )

    script = r"""set -u
TURN_DIR="__TURN_DIR__"
REASON_FILE="__REASON_FILE__"
__TOOLS_VARS__log() { echo "[assistant_turn] $*"; }
fail() { printf '%s' "$1" > "$REASON_FILE" 2>/dev/null || true; __FAIL_CLEANUP__log "FAIL: $1"; exit 1; }

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
    script = script.replace("__TOOLS_VARS__", tools_vars)
    script = script.replace("__FAIL_CLEANUP__", fail_cleanup)
    script = script.replace(
        "__PREFLIGHT_BLOCK__", (preflight + tools_preflight).rstrip("\n")
    )
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

    `usage` (packet D3, best-effort): `{"input_tokens": int, "output_tokens":
    int}` parsed from `reply.json`'s top-level `usage` object when present
    (only ever set on `status == "ok"`) — `None` when the CLI build's JSON
    shape did not include it. Never blocks or changes any other field."""

    status: str
    text: Optional[str] = None
    reason: Optional[str] = None
    usage: Optional[dict] = None
    #: DG-ASSISTANT-TOOLS v1: bridge call log entries (`tool`/`status`/
    #: `approval_id`) and, when tools were requested but unusable on the
    #: runner, the recorded reason. Attached for every settled status except
    #: `unreachable`, so cards created before a timeout are never lost.
    tool_calls: tuple[dict, ...] = ()
    tools_reason: Optional[str] = None


def _parse_reply_json(raw: str) -> tuple[Optional[str], Optional[str], Optional[dict]]:
    """Defensively extract `(text, error, usage)` from `claude -p
    --output-format json`'s stdout. Returns `(text, None, usage)` on a
    recognizable success shape, `(None, reason, None)` otherwise — never
    raises. `usage` is `{"input_tokens": int, "output_tokens": int}` when
    the top-level `usage` object has both as ints, else `None` (packet D3 —
    purely additive, never affects the text/error outcome)."""

    stripped = (raw or "").strip()
    if not stripped:
        return None, "reply.json is empty", None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        # Some CLI builds may write plain text despite the flag; fall back to
        # the raw content rather than treating it as an unconditional failure.
        return stripped, None, None
    if not isinstance(data, dict):
        return None, "reply.json top level is not a JSON object", None
    usage = _parse_reply_usage(data)
    if data.get("is_error"):
        result = data.get("result")
        return (
            None,
            result if isinstance(result, str) and result else "claude reported is_error=true",
            None,
        )
    result = data.get("result")
    if isinstance(result, str):
        return result, None, usage
    return None, "reply.json has no string 'result' field", None


def _parse_reply_usage(data: dict) -> Optional[dict]:
    """`data["usage"]` (claude CLI JSON output convention: `{"input_tokens":
    N, ..., "output_tokens": N, ...}`, may include cache-related keys this
    ledger does not track) -> `{"input_tokens": int, "output_tokens": int}`
    or `None` if the shape does not match. Never raises."""

    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


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
    model: Optional[str] = None,
    tools: Optional[AssistantToolsSpec] = None,
    bridge_source: Optional[str] = None,
) -> AssistantTurnResult:
    """Launch one bounded turn and block until it converges (or times out).

    `tools`/`bridge_source` (DG-ASSISTANT-TOOLS v1): when both are given the
    bridge source, `tools.json`, the per-turn token and `mcp.json` are
    SFTP-written into the turn directory before `run.sh`, and after the turn
    settles the bridge's call log and the tools-availability reason are read
    back into the result. Without `tools` the behaviour is unchanged."""

    if tools is not None and not bridge_source:
        raise InvalidAssistantTurnInputError("bridge_source is required when tools are enabled")
    result = await _run_assistant_turn_core(
        runner_server=runner_server,
        workspace_rel=workspace_rel,
        session_key=session_key,
        turn_no=turn_no,
        history=history,
        user_text=user_text,
        ssh_run=ssh_run,
        ssh_write_file=ssh_write_file,
        turn_timeout_sec=turn_timeout_sec,
        poll_interval_sec=poll_interval_sec,
        sleep=sleep,
        model=model,
        tools=tools,
        bridge_source=bridge_source or "",
    )
    if tools is None or result.status == "unreachable":
        return result
    paths = build_dispatch_paths(workspace_rel, session_key, turn_no)
    tool_calls: tuple[dict, ...] = ()
    tools_reason: Optional[str] = None
    try:
        log_res = await ssh_run(
            runner_server, build_read_file_command(paths.calls_log_file), ASSISTANT_SSH_PROBE_TIMEOUT
        )
        tool_calls = _parse_calls_log(log_res.stdout or "")
        reason_res = await ssh_run(
            runner_server,
            build_read_file_command(paths.tools_reason_file),
            ASSISTANT_SSH_PROBE_TIMEOUT,
        )
        tools_reason = _parse_tools_reason(reason_res.stdout or "")
    except Exception:  # noqa: BLE001 - evidence read failure never changes the reply
        pass
    return replace(result, tool_calls=tool_calls, tools_reason=tools_reason)


async def _run_assistant_turn_core(
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
    model: Optional[str] = None,
    tools: Optional[AssistantToolsSpec] = None,
    bridge_source: str = "",
) -> AssistantTurnResult:
    """Launch one bounded turn and block until it converges (or times out).

    `model` (packet D2, `config.assistant_claude_model`): forwarded verbatim
    to `build_assistant_turn_script()`, which is the one place it is
    validated/quoted into the shell string.

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
    prompt = build_assistant_prompt(history, user_text, tools_enabled=tools is not None)
    script = build_assistant_turn_script(
        session_key=session_key,
        turn_no=turn_no,
        workspace_rel=workspace_rel,
        turn_timeout_sec=turn_timeout_sec,
        model=model,
        tools=tools,
    )
    run_sh_path = f"{paths.turn_dir}/run.sh"

    try:
        await ssh_run(
            runner_server,
            build_mkdir_command(workspace_rel, session_key, turn_no),
            ASSISTANT_SSH_PROBE_TIMEOUT,
        )
        if tools is not None:
            # INV-SSH-2/3: the bridge source, config and the bearer land as
            # files over SFTP; none of them is ever part of a shell string.
            await ssh_write_file(runner_server, paths.bridge_file, bridge_source)
            await ssh_write_file(runner_server, paths.tools_config_file, build_tools_config_json(tools))
            await ssh_write_file(runner_server, paths.token_file, tools.token + "\n")
            await ssh_write_file(runner_server, paths.mcp_config_file, build_mcp_config_json(tools))
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
        text, error, usage = _parse_reply_json(reply_res.stdout or "")
        if error is not None:
            return AssistantTurnResult(status="failed", reason=error)
        return AssistantTurnResult(status="ok", text=text or "", usage=usage)

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
    "ASSISTANT_TOOLS_MAX_TURNS",
    "ASSISTANT_TOOLS_PREAMBLE",
    "ASSISTANT_TOOLS_SERVER_NAME",
    "AssistantToolsSpec",
    "AssistantTurnPaths",
    "AssistantTurnResult",
    "InvalidAssistantTurnInputError",
    "build_allowed_tools_value",
    "build_assistant_prompt",
    "build_assistant_turn_script",
    "build_check_exit_code_command",
    "build_dispatch_paths",
    "build_launch_command",
    "build_mcp_config_json",
    "build_mkdir_command",
    "build_read_file_command",
    "build_tmux_check_command",
    "build_tools_config_json",
    "run_assistant_turn",
    "tmux_session_name",
]
