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

_APPROVED_CODING_AGENT_PROVIDERS: Mapping[str, CodingAgentProvider] = MappingProxyType(
    {_CODEX_DESCRIPTOR.provider_id: _CODEX_PROVIDER}
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
