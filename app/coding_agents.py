"""Reviewed coding-agent providers and their execution capabilities.

Only adapters that have been explicitly reviewed belong in this registry.  A
request can select a provider id, never an executable or arbitrary command.
The first adapter preserves the existing one-shot ``codex exec`` behavior; it
does not pretend that the experimental app-server lifecycle is available.

The provider interface is intentionally transport-neutral.  ``start_turn``
returns a deterministic launch plan consumed by the existing approved Job
executor.  Unsupported lifecycle operations fail closed instead of silently
falling back to an unreviewed shell path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from string import hexdigits
from types import MappingProxyType
from typing import Any, Mapping, NoReturn


CODEX_AGENT_PROVIDER_ID = "codex"
CLAUDE_CODE_AGENT_PROVIDER_ID = "claude-code"

#: Shared, deterministic PATH-extension shell fragment reused verbatim by
#: every remote probe/preflight that invokes ``claude`` or ``codex`` (status
#: probes in ``app/main.py``, turn preflights in ``app/assistant_turns.py``/
#: ``app/agent_session_turns.py``, and both ``start_turn`` preflights below).
#: Both CLIs are commonly installed under a user-local, npm-global, or
#: nvm-managed directory that a non-interactive SSH shell's default PATH
#: omits — real-runner diagnosis: ``claude`` under ``~/.local/bin`` (job 96,
#: docs/DECISIONS.md 2026-08-24); ``codex`` additionally observed under
#: ``~/.npm-global/bin`` and an nvm-managed ``node/*/bin`` directory on
#: worker_5090_106. Fixed literal, defined once and reused verbatim — never
#: interpolates user input. The nvm glob expands in bash's default
#: sorted-by-name order, so the *last* matching node version directory is
#: prepended last and therefore wins highest PATH precedence.
PATH_EXTENSION_FRAGMENT = (
    '  export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.npm-global/bin:$PATH"\n'
    '  if [ -d "$HOME/.nvm/versions/node" ]; then for __nvb in "$HOME"/.nvm/'
    'versions/node/*/bin; do [ -d "$__nvb" ] && PATH="$__nvb:$PATH"; done; fi\n'
)

#: DG-CLAUDE-ADAPTER v1 (docs/DECISIONS.md 2026-08-24, C-5): reviewed
#: compatible Claude Code CLI version range, ``[MIN, MAX)``.  A version probe
#: outside this range fails closed instead of guessing compatibility.  Widened
#: from the original ``[1.0.0, 2.0.0)`` after the real-runner diagnosis (job
#: 96 on worker_5090_106) observed CLI ``2.1.246`` already installed and
#: logged in — ``2.x`` is the first real-runner observed major version; the
#: fail-closed awk range check itself is unchanged, only these bounds moved.
CLAUDE_CODE_CLI_MIN_VERSION = (1, 0, 0)
CLAUDE_CODE_CLI_MAX_VERSION_EXCLUSIVE = (3, 0, 0)

#: Fixed dev-local `--allowedTools` set shared by every reviewed one-shot or
#: turn-based Claude Code launch in this codebase (this module's
#: ``ClaudeCodeExecProvider.start_turn`` and
#: ``app.agent_session_turns._confinement_allowed_tools``'s always-on file
#: tools). File tools only — no ``Bash`` here: validation commands run
#: through the platform's own controlled validation path, never as a tool
#: grant to the CLI itself (DG-CLAUDE-ADAPTER v1 boundary). Single source so
#: the two call sites can never silently drift apart.
CLAUDE_CODE_DEV_LOCAL_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
)


class UnknownCodingAgentProviderError(ValueError):
    """Raised when a provider is not present in the reviewed allowlist."""


class CodingAgentCapabilityUnavailableError(RuntimeError):
    """Raised when a reviewed adapter does not implement an operation."""


class CodingAgentCommandApprovalDecision(str, Enum):
    """Only non-persistent callback decisions a future controller may send."""

    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


@dataclass(frozen=True)
class CodingAgentCapabilities:
    """Honest capability metadata for one approved provider adapter."""

    worktree_isolation: bool
    immutable_base: bool
    start_turn: bool
    resume_turn: bool
    cancel_turn: bool
    event_stream: bool
    command_approval_callback: bool
    network_policy: str
    dependency_policy: str


@dataclass(frozen=True)
class CodingAgentDescriptor:
    """Immutable identity and capability facts for one reviewed adapter."""

    provider_id: str
    display_name: str
    adapter: str
    capabilities: CodingAgentCapabilities

    def capability_snapshot(self) -> dict[str, Any]:
        """Return a fresh, persistence-compatible capability snapshot.

        The serialized keys intentionally match the existing
        ``engineering-task-v1`` approval payload.  Runtime-only operation
        fields such as ``start_turn`` and ``cancel_turn`` are not added to that
        immutable public contract retroactively.
        """

        return {
            "provider_id": self.provider_id,
            "adapter": self.adapter,
            "worktree_isolation": self.capabilities.worktree_isolation,
            "immutable_base": self.capabilities.immutable_base,
            "event_stream": self.capabilities.event_stream,
            "resume_turn": self.capabilities.resume_turn,
            "command_approval_callback": (
                self.capabilities.command_approval_callback
            ),
            "network_policy": self.capabilities.network_policy,
            "dependency_policy": self.capabilities.dependency_policy,
        }



@dataclass(frozen=True)
class CodingAgentOutputContract:
    """Provider-owned outputs available after one turn.

    The outer Coding Runner still owns ``result.json``, Git commits, diffs and
    bundles.  This contract describes only the agent adapter's own outputs.
    """

    final_response_file: str | None
    checkpoint_file: str | None
    event_stream: bool
    machine_event_log_file: str | None

    def safe_snapshot(self) -> dict[str, bool]:
        return {
            "final_response": self.final_response_file is not None,
            "checkpoint": self.checkpoint_file is not None,
            "event_stream": self.event_stream,
            "machine_event_log": self.machine_event_log_file is not None,
        }


@dataclass(frozen=True)
class CodingAgentTurnLaunch:
    """Deterministic executor plan for one coding-agent turn."""

    provider_id: str
    adapter: str
    shell_command: str
    execution_mode: str
    outputs: CodingAgentOutputContract
    #: Provider-owned preflight lines (version probe + login-status check)
    #: inserted before the runner Job takes any action on the untrusted
    #: instruction.  Pure text produced from fixed identifiers only — never
    #: contains the instruction (INV-SSH-2/3).
    preflight_script: str = ""

    def safe_metadata(self) -> dict[str, Any]:
        """Return correlation metadata without the executor command."""

        return {
            "provider_id": self.provider_id,
            "adapter": self.adapter,
            "execution_mode": self.execution_mode,
            "outputs": self.outputs.safe_snapshot(),
        }


@dataclass(frozen=True)
class CodingAgentCommandApprovalHandle:
    """Opaque callback correlation bound to one immutable task command.

    App-server JSON-RPC request ids can be strings or integers, while its
    provider-level approval id may be absent.  The callback must also remain
    tied to the task, attempt, parent approval, turn, item, exact command digest
    and approved absolute worktree.
    """

    request_id: str | int
    engineering_task_id: str
    attempt_number: int
    parent_approval_id: int
    thread_id: str
    turn_id: str
    item_id: str
    provider_approval_id: str | None
    command_digest: str
    working_directory: str

    def __post_init__(self) -> None:
        if isinstance(self.request_id, bool) or not isinstance(
            self.request_id, (str, int)
        ):
            raise ValueError("provider request id must be an opaque string or integer")
        if isinstance(self.request_id, str) and not self.request_id:
            raise ValueError("provider request id must not be empty")
        for value, label in (
            (self.engineering_task_id, "engineering_task_id"),
            (self.thread_id, "thread_id"),
            (self.turn_id, "turn_id"),
            (self.item_id, "item_id"),
        ):
            if not value:
                raise ValueError(f"{label} must not be empty")
        if self.attempt_number < 1 or self.parent_approval_id < 1:
            raise ValueError("task attempt and parent approval must be positive")
        if len(self.command_digest) != 64 or any(
            char not in hexdigits for char in self.command_digest
        ):
            raise ValueError("command digest must be a full SHA-256 hex value")
        worktree = PurePosixPath(self.working_directory)
        if not worktree.is_absolute() or ".." in worktree.parts:
            raise ValueError("working directory must be an absolute normalized path")




def _unsupported(provider_id: str, operation: str) -> NoReturn:
    raise CodingAgentCapabilityUnavailableError(
        f"coding agent provider {provider_id!r} does not support {operation}"
    )




_CODEX_DESCRIPTOR = CodingAgentDescriptor(
    provider_id=CODEX_AGENT_PROVIDER_ID,
    display_name="Codex",
    adapter="codex-exec-v1",
    capabilities=CodingAgentCapabilities(
        worktree_isolation=True,
        immutable_base=True,
        start_turn=True,
        resume_turn=False,
        cancel_turn=False,
        event_stream=False,
        command_approval_callback=False,
        network_policy="disabled",
        dependency_policy="not_authorized",
    ),
)





_CLAUDE_CODE_DESCRIPTOR = CodingAgentDescriptor(
    provider_id=CLAUDE_CODE_AGENT_PROVIDER_ID,
    display_name="Claude Code",
    adapter="claude-code-v1",
    capabilities=CodingAgentCapabilities(
        worktree_isolation=True,
        immutable_base=True,
        start_turn=True,
        resume_turn=False,
        cancel_turn=False,
        event_stream=False,
        command_approval_callback=False,
        network_policy="disabled",
        dependency_policy="not_authorized",
    ),
)


#: Both providers are reviewed and structurally identical one-shot adapters.
#: ``claude-code``'s selectability for a *new* Engineering Task request is
#: additionally gated by ``CLAUDE_CODE_AGENT_V1`` (default off) at the
#: request-validation call sites, not here — this registry only records that
#: the adapter itself has been reviewed (DG-CLAUDE-ADAPTER v1, C-1).


CODEX_APP_SERVER_PROVIDER_ID = "codex-app-server"




_CODEX_APP_SERVER_DESCRIPTOR = CodingAgentDescriptor(
    provider_id=CODEX_APP_SERVER_PROVIDER_ID,
    display_name="Codex (app-server, unwired)",
    adapter="codex-app-server-v1",
    capabilities=CodingAgentCapabilities(
        worktree_isolation=False,
        immutable_base=False,
        start_turn=False,
        resume_turn=False,
        cancel_turn=False,
        event_stream=False,
        command_approval_callback=False,
        network_policy="not_authorized",
        dependency_policy="not_authorized",
    ),
)


#: Deliberately separate from ``_APPROVED_CODING_AGENT_PROVIDERS``: nothing
#: here is part of the Engineering Task provider-selection or approval-payload
#: contract (``list_coding_agents``/``list_coding_agent_capability_snapshots``
#: must stay exactly ``["codex"]`` in this slice).


#: DG-AGENT-RUNTIME-V3 Phase 1b (R6) — registry note, kept for the record:
#: the reviewed one-shot exec adapters (`codex` / `codex-exec-v1` and
#: `claude-code` / `claude-code-v1`) and the never-wired app-server adapter
#: are **retired**. Their runtime classes, deterministic script builders and
#: the `engineering_command` callback channel were removed with the
#: job-backed execution path; engineering work runs as Studio SDK sessions
#: on runner agents (INV-AGENT-1/2) and promotes through the unchanged
#: bundle chain. The descriptors below stay so historical task rows,
#: capability snapshots and `GET /coding-agents` keep resolving; every
#: runtime operation reports unavailable. Re-wiring any exec adapter is a
#: new named decision, not a revert.
_APPROVED_CODING_AGENT_DESCRIPTORS: Mapping[str, CodingAgentDescriptor] = MappingProxyType(
    {
        _CODEX_DESCRIPTOR.provider_id: _CODEX_DESCRIPTOR,
        _CLAUDE_CODE_DESCRIPTOR.provider_id: _CLAUDE_CODE_DESCRIPTOR,
    }
)


def get_coding_agent(provider_id: str) -> CodingAgentDescriptor | None:
    """Look up a reviewed provider without accepting aliases or executables."""

    return _APPROVED_CODING_AGENT_DESCRIPTORS.get(provider_id)




def require_coding_agent(provider_id: str) -> CodingAgentDescriptor:
    """Return a reviewed provider or reject the unapproved identifier."""

    descriptor = get_coding_agent(provider_id)
    if descriptor is None:
        raise UnknownCodingAgentProviderError(
            f"unapproved coding agent provider: {provider_id!r}"
        )
    return descriptor




def list_coding_agents() -> tuple[CodingAgentDescriptor, ...]:
    """Return reviewed descriptors in deterministic provider-id order."""

    return tuple(
        _APPROVED_CODING_AGENT_DESCRIPTORS[provider_id]
        for provider_id in sorted(_APPROVED_CODING_AGENT_DESCRIPTORS)
    )


def list_coding_agent_capability_snapshots() -> list[dict[str, Any]]:
    """Return fresh serialized snapshots in deterministic registry order."""

    return [descriptor.capability_snapshot() for descriptor in list_coding_agents()]


def list_coding_agent_runtime_capability_snapshots() -> list[dict[str, Any]]:
    """Phase 1b: every reviewed exec adapter is retired — the discovery payload
    says so truthfully instead of advertising operations that no longer exist."""

    return [
        {
            "provider_id": descriptor.provider_id,
            "display_name": descriptor.display_name,
            "adapter": descriptor.adapter,
            "operations": {
                "start_turn": False,
                "resume_turn": False,
                "cancel_turn": False,
                "event_stream": False,
                "command_approval_callback": False,
            },
            "execution_mode": "retired",
            "protocol_stability": "retired_legacy_adapter",
            "retired": True,
            "note": "已退役（DG-AGENT-RUNTIME-V3 Phase 1b）：工程工作改用 Studio SDK session",
        }
        for descriptor in list_coding_agents()
    ]




