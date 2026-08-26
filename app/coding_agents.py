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

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from string import hexdigits
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping, NoReturn


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
class CodingAgentTurnRequest:
    """Inputs that a reviewed adapter may use to construct one launch.

    Instruction and filesystem locations are fixed by the outer Coding Runner
    script and therefore are deliberately not accepted as free-form fields.
    """

    network_access: bool = False


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


class CodingAgentProvider(ABC):
    """Execution interface implemented only by reviewed provider adapters."""

    @property
    @abstractmethod
    def descriptor(self) -> CodingAgentDescriptor:
        """Return immutable provider identity and capability metadata."""

    @abstractmethod
    def runtime_capability_snapshot(self) -> dict[str, Any]:
        """Return truthful safe runtime metadata for discovery clients."""

    @abstractmethod
    def start_turn(self, request: CodingAgentTurnRequest) -> CodingAgentTurnLaunch:
        """Create a deterministic launch plan for a new turn."""

    @abstractmethod
    def resume_turn(self, *, thread_id: str, instruction: str) -> CodingAgentTurnLaunch:
        """Resume an existing provider thread, or fail closed."""

    @abstractmethod
    def cancel_turn(self, *, thread_id: str, turn_id: str) -> None:
        """Cancel an active provider turn, or fail closed."""

    @abstractmethod
    def stream_events(
        self, *, thread_id: str, turn_id: str
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Return provider events, or fail closed."""

    @abstractmethod
    def respond_to_command_approval(
        self,
        *,
        handle: CodingAgentCommandApprovalHandle,
        decision: CodingAgentCommandApprovalDecision,
    ) -> None:
        """Respond to a provider command callback, or fail closed."""


def _unsupported(provider_id: str, operation: str) -> NoReturn:
    raise CodingAgentCapabilityUnavailableError(
        f"coding agent provider {provider_id!r} does not support {operation}"
    )


@dataclass(frozen=True)
class CodexExecProvider(CodingAgentProvider):
    """Reviewed adapter for the existing one-shot ``codex exec`` runner."""

    _descriptor: CodingAgentDescriptor

    @property
    def descriptor(self) -> CodingAgentDescriptor:
        return self._descriptor

    def runtime_capability_snapshot(self) -> dict[str, Any]:
        capabilities = self.descriptor.capabilities
        return {
            "provider_id": self.descriptor.provider_id,
            "display_name": self.descriptor.display_name,
            "adapter": self.descriptor.adapter,
            "operations": {
                "start_turn": capabilities.start_turn,
                "resume_turn": capabilities.resume_turn,
                "cancel_turn": capabilities.cancel_turn,
                "event_stream": capabilities.event_stream,
                "command_approval_callback": (
                    capabilities.command_approval_callback
                ),
            },
            "execution_mode": "single_turn_process",
            "protocol_stability": "reviewed_legacy_adapter",
            "outputs": CodingAgentOutputContract(
                final_response_file="final_message.txt",
                checkpoint_file=None,
                event_stream=False,
                machine_event_log_file="codex.jsonl",
            ).safe_snapshot(),
            "policy_scope": {
                "engineering_task_network": "disabled",
                "legacy_network_override": "platform_config_only",
                "dependency_installation": "not_authorized",
                "inner_command_approval": "unavailable",
                "inner_command_enforcement": "sandbox_only",
                "final_git_path_policy": (
                    "runner_pre_bundle_and_server_a_pre_accept"
                ),
                "turn_time_path_confinement": "unavailable",
            },
        }

    def start_turn(self, request: CodingAgentTurnRequest) -> CodingAgentTurnLaunch:
        network_args = (
            "-c sandbox_workspace_write.network_access=true "
            if request.network_access
            else ""
        )
        command = (
            '  codex exec --cd "$REPO_DIR" --sandbox workspace-write '
            "-c approval_policy=never --json \\\n"
            f'    -o "$TASK_DIR/final_message.txt" {network_args}'
            '- < "$TASK_DIR/instruction.txt" > "$TASK_DIR/codex.jsonl"'
        )
        preflight = (
            PATH_EXTENSION_FRAGMENT
            + "  command -v codex >/dev/null 2>&1 || fail 'codex CLI 未安裝："
            "請照 README §13 在 Codex Runner 安裝並登入'\n"
            '  export R_CODEX_VERSION="$(codex --version 2>/dev/null | head -1)"\n'
            "  codex login status >/dev/null 2>&1 || fail 'codex 未登入："
            "請在 Runner 執行 codex login（或 codex login --with-api-key）'\n"
        )
        return CodingAgentTurnLaunch(
            provider_id=self.descriptor.provider_id,
            adapter=self.descriptor.adapter,
            shell_command=command,
            execution_mode="single_turn_process",
            outputs=CodingAgentOutputContract(
                final_response_file="final_message.txt",
                checkpoint_file=None,
                event_stream=False,
                machine_event_log_file="codex.jsonl",
            ),
            preflight_script=preflight,
        )

    def resume_turn(self, *, thread_id: str, instruction: str) -> CodingAgentTurnLaunch:
        _unsupported(self.descriptor.provider_id, "resume_turn")

    def cancel_turn(self, *, thread_id: str, turn_id: str) -> None:
        _unsupported(self.descriptor.provider_id, "cancel_turn")

    def stream_events(
        self, *, thread_id: str, turn_id: str
    ) -> AsyncIterator[Mapping[str, Any]]:
        _unsupported(self.descriptor.provider_id, "event_stream")

    def respond_to_command_approval(
        self,
        *,
        handle: CodingAgentCommandApprovalHandle,
        decision: CodingAgentCommandApprovalDecision,
    ) -> None:
        _unsupported(self.descriptor.provider_id, "command_approval_callback")


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

_CODEX_PROVIDER = CodexExecProvider(_CODEX_DESCRIPTOR)


@dataclass(frozen=True)
class ClaudeCodeExecProvider(CodingAgentProvider):
    """DG-CLAUDE-ADAPTER v1 (docs/DECISIONS.md 2026-08-24): reviewed adapter
    for a headless one-shot ``claude -p`` (non-interactive print mode) turn.

    Structurally identical to :class:`CodexExecProvider`: the same one-shot
    launch shape, the same fail-closed lifecycle (no resume/cancel/stream/
    command-approval), and the same instruction-never-in-shell-string rule
    (INV-SSH-2/3) — the instruction only ever reaches the CLI via
    ``< "$TASK_DIR/instruction.txt"`` stdin redirect.  Registry membership
    here does not by itself make this provider selectable: request-time
    selection is additionally gated by ``CLAUDE_CODE_AGENT_V1`` (default
    off) in ``app/engineering_tasks.py``/``app/approvals.py``, and its
    discovery listing is gated the same way in ``GET /coding-agents``.
    """

    _descriptor: CodingAgentDescriptor

    @property
    def descriptor(self) -> CodingAgentDescriptor:
        return self._descriptor

    def runtime_capability_snapshot(self) -> dict[str, Any]:
        capabilities = self.descriptor.capabilities
        return {
            "provider_id": self.descriptor.provider_id,
            "display_name": self.descriptor.display_name,
            "adapter": self.descriptor.adapter,
            "operations": {
                "start_turn": capabilities.start_turn,
                "resume_turn": capabilities.resume_turn,
                "cancel_turn": capabilities.cancel_turn,
                "event_stream": capabilities.event_stream,
                "command_approval_callback": (
                    capabilities.command_approval_callback
                ),
            },
            "execution_mode": "single_turn_process",
            "protocol_stability": "reviewed_legacy_adapter",
            "outputs": CodingAgentOutputContract(
                final_response_file="final_message.txt",
                checkpoint_file=None,
                event_stream=False,
                machine_event_log_file="claude.jsonl",
            ).safe_snapshot(),
            "policy_scope": {
                "engineering_task_network": "disabled",
                "legacy_network_override": "platform_config_only",
                "dependency_installation": "not_authorized",
                "inner_command_approval": "unavailable",
                "inner_command_enforcement": "sandbox_only",
                "final_git_path_policy": (
                    "runner_pre_bundle_and_server_a_pre_accept"
                ),
                "turn_time_path_confinement": "unavailable",
            },
        }

    def start_turn(self, request: CodingAgentTurnRequest) -> CodingAgentTurnLaunch:
        network_flag = "--allow-network " if request.network_access else ""
        command = (
            f'  claude -p --output-format json {network_flag}\\\n'
            '    < "$TASK_DIR/instruction.txt" > "$TASK_DIR/claude.jsonl"\n'
            "  CLAUDE_EXIT=$?\n"
            "  python3 -c '\n"
            "import json, sys\n"
            "source, dest = sys.argv[1], sys.argv[2]\n"
            "try:\n"
            "    with open(source) as fh:\n"
            "        payload = json.load(fh)\n"
            '    text = payload.get("result") or ""\n'
            "except Exception:\n"
            '    text = ""\n'
            "with open(dest, \"w\") as fh:\n"
            "    fh.write(text)\n"
            "' \"$TASK_DIR/claude.jsonl\" \"$TASK_DIR/final_message.txt\"\n"
            '  ( exit "$CLAUDE_EXIT" )'
        )
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
        preflight = (
            PATH_EXTENSION_FRAGMENT
            + "  command -v claude >/dev/null 2>&1 || fail 'claude CLI 未安裝："
            "請照 README 在 Claude Code Runner 安裝並登入'\n"
            '  export R_CODEX_VERSION="$(claude --version 2>/dev/null | head -1)"\n'
            '  CLAUDE_VERSION_NUM="$(printf \'%s\\n\' "$R_CODEX_VERSION" | '
            "grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1)\"\n"
            '  [ -n "$CLAUDE_VERSION_NUM" ] || fail \'claude CLI 版本無法解析\'\n'
            f"  awk -v v=\"$CLAUDE_VERSION_NUM\" 'BEGIN{{split(v,a,\".\");"
            f"n=a[1]*1000000+a[2]*1000+a[3]; if (n>={min_bound} && n<{max_bound}) "
            "exit 0; exit 1}' || fail 'claude CLI 版本不在已審閱相容範圍內'\n"
            "  claude auth status >/dev/null 2>&1 || fail 'claude 未登入："
            "請在 Runner 完成 Claude Code 登入'\n"
        )
        return CodingAgentTurnLaunch(
            provider_id=self.descriptor.provider_id,
            adapter=self.descriptor.adapter,
            shell_command=command,
            execution_mode="single_turn_process",
            outputs=CodingAgentOutputContract(
                final_response_file="final_message.txt",
                checkpoint_file=None,
                event_stream=False,
                machine_event_log_file="claude.jsonl",
            ),
            preflight_script=preflight,
        )

    def resume_turn(self, *, thread_id: str, instruction: str) -> CodingAgentTurnLaunch:
        _unsupported(self.descriptor.provider_id, "resume_turn")

    def cancel_turn(self, *, thread_id: str, turn_id: str) -> None:
        _unsupported(self.descriptor.provider_id, "cancel_turn")

    def stream_events(
        self, *, thread_id: str, turn_id: str
    ) -> AsyncIterator[Mapping[str, Any]]:
        _unsupported(self.descriptor.provider_id, "event_stream")

    def respond_to_command_approval(
        self,
        *,
        handle: CodingAgentCommandApprovalHandle,
        decision: CodingAgentCommandApprovalDecision,
    ) -> None:
        _unsupported(self.descriptor.provider_id, "command_approval_callback")


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

_CLAUDE_CODE_PROVIDER = ClaudeCodeExecProvider(_CLAUDE_CODE_DESCRIPTOR)

#: Both providers are reviewed and structurally identical one-shot adapters.
#: ``claude-code``'s selectability for a *new* Engineering Task request is
#: additionally gated by ``CLAUDE_CODE_AGENT_V1`` (default off) at the
#: request-validation call sites, not here — this registry only records that
#: the adapter itself has been reviewed (DG-CLAUDE-ADAPTER v1, C-1).
_APPROVED_CODING_AGENT_PROVIDERS: Mapping[str, CodingAgentProvider] = MappingProxyType(
    {
        _CODEX_DESCRIPTOR.provider_id: _CODEX_PROVIDER,
        _CLAUDE_CODE_DESCRIPTOR.provider_id: _CLAUDE_CODE_PROVIDER,
    }
)


CODEX_APP_SERVER_PROVIDER_ID = "codex-app-server"


@dataclass(frozen=True)
class CodexAppServerProvider(CodingAgentProvider):
    """D1 bounded first slice (docs/DECISIONS.md): registered but unwired.

    Every ``CodingAgentProvider`` operation fails closed here regardless of
    ``CONTROLLED_CODING_RUNNER_V1`` — that flag only controls whether this
    adapter's identity appears in the ``GET /coding-agents`` discovery list
    (see ``list_experimental_coding_agent_runtime_capability_snapshots``).
    This provider is intentionally never added to
    ``_APPROVED_CODING_AGENT_PROVIDERS``: an Engineering Task request can
    only ever select the ``codex`` provider id, so this adapter can never be
    used to start a real turn in this slice. The JSON-RPC session shape it
    will eventually front lives in ``app/codex_app_server.py``.
    """

    _descriptor: CodingAgentDescriptor

    @property
    def descriptor(self) -> CodingAgentDescriptor:
        return self._descriptor

    def runtime_capability_snapshot(self) -> dict[str, Any]:
        from app.codex_app_server import (
            REVIEWED_CAPABILITIES,
            REVIEWED_PROTOCOL_VERSION,
        )

        capabilities = self.descriptor.capabilities
        return {
            "provider_id": self.descriptor.provider_id,
            "display_name": self.descriptor.display_name,
            "adapter": self.descriptor.adapter,
            "operations": {
                "start_turn": capabilities.start_turn,
                "resume_turn": capabilities.resume_turn,
                "cancel_turn": capabilities.cancel_turn,
                "event_stream": capabilities.event_stream,
                "command_approval_callback": (
                    capabilities.command_approval_callback
                ),
            },
            "execution_mode": "not_wired",
            "protocol_stability": "bounded_first_slice_unwired",
            "session_protocol_version": REVIEWED_PROTOCOL_VERSION,
            "session_protocol_capabilities": sorted(REVIEWED_CAPABILITIES),
            "outputs": CodingAgentOutputContract(
                final_response_file=None,
                checkpoint_file=None,
                event_stream=False,
                machine_event_log_file=None,
            ).safe_snapshot(),
            "policy_scope": {
                "engineering_task_network": "not_applicable",
                "legacy_network_override": "not_applicable",
                "dependency_installation": "not_authorized",
                "inner_command_approval": "unavailable",
                "inner_command_enforcement": "unavailable",
                "final_git_path_policy": "not_applicable",
                "turn_time_path_confinement": "unavailable",
            },
        }

    def start_turn(self, request: CodingAgentTurnRequest) -> CodingAgentTurnLaunch:
        _unsupported(self.descriptor.provider_id, "start_turn")

    def resume_turn(self, *, thread_id: str, instruction: str) -> CodingAgentTurnLaunch:
        _unsupported(self.descriptor.provider_id, "resume_turn")

    def cancel_turn(self, *, thread_id: str, turn_id: str) -> None:
        _unsupported(self.descriptor.provider_id, "cancel_turn")

    def stream_events(
        self, *, thread_id: str, turn_id: str
    ) -> AsyncIterator[Mapping[str, Any]]:
        _unsupported(self.descriptor.provider_id, "event_stream")

    def respond_to_command_approval(
        self,
        *,
        handle: CodingAgentCommandApprovalHandle,
        decision: CodingAgentCommandApprovalDecision,
    ) -> None:
        _unsupported(self.descriptor.provider_id, "command_approval_callback")


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

_CODEX_APP_SERVER_PROVIDER = CodexAppServerProvider(_CODEX_APP_SERVER_DESCRIPTOR)

#: Deliberately separate from ``_APPROVED_CODING_AGENT_PROVIDERS``: nothing
#: here is part of the Engineering Task provider-selection or approval-payload
#: contract (``list_coding_agents``/``list_coding_agent_capability_snapshots``
#: must stay exactly ``["codex"]`` in this slice).
_EXPERIMENTAL_UNWIRED_CODING_AGENT_PROVIDERS: Mapping[str, CodingAgentProvider] = (
    MappingProxyType(
        {_CODEX_APP_SERVER_DESCRIPTOR.provider_id: _CODEX_APP_SERVER_PROVIDER}
    )
)


def get_coding_agent(provider_id: str) -> CodingAgentDescriptor | None:
    """Look up a reviewed provider without accepting aliases or executables."""

    provider = _APPROVED_CODING_AGENT_PROVIDERS.get(provider_id)
    return provider.descriptor if provider is not None else None


def get_coding_agent_provider(provider_id: str) -> CodingAgentProvider | None:
    """Look up the reviewed runtime adapter for an exact provider id."""

    return _APPROVED_CODING_AGENT_PROVIDERS.get(provider_id)


def require_coding_agent(provider_id: str) -> CodingAgentDescriptor:
    """Return a reviewed provider or reject the unapproved identifier."""

    descriptor = get_coding_agent(provider_id)
    if descriptor is None:
        raise UnknownCodingAgentProviderError(
            f"unapproved coding agent provider: {provider_id!r}"
        )
    return descriptor


def require_coding_agent_provider(provider_id: str) -> CodingAgentProvider:
    """Return a reviewed runtime adapter or reject the identifier."""

    provider = get_coding_agent_provider(provider_id)
    if provider is None:
        raise UnknownCodingAgentProviderError(
            f"unapproved coding agent provider: {provider_id!r}"
        )
    return provider


def list_coding_agents() -> tuple[CodingAgentDescriptor, ...]:
    """Return reviewed descriptors in deterministic provider-id order."""

    return tuple(
        _APPROVED_CODING_AGENT_PROVIDERS[provider_id].descriptor
        for provider_id in sorted(_APPROVED_CODING_AGENT_PROVIDERS)
    )


def list_coding_agent_capability_snapshots() -> list[dict[str, Any]]:
    """Return fresh serialized snapshots in deterministic registry order."""

    return [descriptor.capability_snapshot() for descriptor in list_coding_agents()]


def list_coding_agent_runtime_capability_snapshots() -> list[dict[str, Any]]:
    """Return fresh safe runtime metadata for every reviewed adapter."""

    return [
        _APPROVED_CODING_AGENT_PROVIDERS[provider_id].runtime_capability_snapshot()
        for provider_id in sorted(_APPROVED_CODING_AGENT_PROVIDERS)
    ]


def list_experimental_coding_agent_runtime_capability_snapshots() -> list[dict[str, Any]]:
    """Return fresh runtime metadata for bounded, not-yet-wired adapters.

    These providers are never part of the reviewed task-contract registry
    (``list_coding_agents``/``list_coding_agent_capability_snapshots``); an
    Engineering Task request can never select one (see
    ``CodexAppServerProvider``). The caller decides whether to surface this
    list at all — ``GET /coding-agents`` only appends it when
    ``CONTROLLED_CODING_RUNNER_V1`` is enabled (docs/DECISIONS.md D1).
    """

    return [
        _EXPERIMENTAL_UNWIRED_CODING_AGENT_PROVIDERS[provider_id]
        .runtime_capability_snapshot()
        for provider_id in sorted(_EXPERIMENTAL_UNWIRED_CODING_AGENT_PROVIDERS)
    ]
