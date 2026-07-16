"""Pure tests for the reviewed coding-agent descriptor registry."""

from dataclasses import FrozenInstanceError

import pytest

from app.coding_agents import (
    CODEX_AGENT_PROVIDER_ID,
    CodingAgentCapabilityUnavailableError,
    CodingAgentCommandApprovalDecision,
    CodingAgentCommandApprovalHandle,
    CodingAgentTurnRequest,
    UnknownCodingAgentProviderError,
    get_coding_agent,
    get_coding_agent_provider,
    list_coding_agent_capability_snapshots,
    list_coding_agent_runtime_capability_snapshots,
    list_coding_agents,
    require_coding_agent,
    require_coding_agent_provider,
)
from app.engineering_tasks import (
    ENGINEERING_TASK_PROVIDER_ID,
    codex_provider_capability_snapshot,
)


EXPECTED_CODEX_SNAPSHOT = {
    "provider_id": "codex",
    "adapter": "codex-exec-v1",
    "worktree_isolation": True,
    "immutable_base": True,
    "event_stream": False,
    "resume_turn": False,
    "command_approval_callback": False,
    "network_policy": "disabled",
    "dependency_policy": "not_authorized",
}

EXPECTED_CODEX_RUNTIME_SNAPSHOT = {
    "provider_id": "codex",
    "display_name": "Codex",
    "adapter": "codex-exec-v1",
    "operations": {
        "start_turn": True,
        "resume_turn": False,
        "cancel_turn": False,
        "event_stream": False,
        "command_approval_callback": False,
    },
    "execution_mode": "single_turn_process",
    "protocol_stability": "reviewed_legacy_adapter",
    "outputs": {
        "final_response": True,
        "checkpoint": False,
        "event_stream": False,
        "machine_event_log": True,
    },
    "policy_scope": {
        "engineering_task_network": "disabled",
        "legacy_network_override": "platform_config_only",
        "dependency_installation": "not_authorized",
        "inner_command_approval": "unavailable",
        "inner_command_enforcement": "sandbox_only",
        "final_git_path_policy": "runner_pre_bundle_and_server_a_pre_accept",
        "turn_time_path_confinement": "unavailable",
    },
}


def test_registry_only_allows_honest_codex_exec_descriptor():
    descriptors = list_coding_agents()

    assert [descriptor.provider_id for descriptor in descriptors] == ["codex"]
    descriptor = require_coding_agent("codex")
    assert descriptor is descriptors[0]
    assert descriptor.display_name == "Codex"
    assert descriptor.adapter == "codex-exec-v1"
    assert descriptor.capabilities.worktree_isolation is True
    assert descriptor.capabilities.immutable_base is True
    assert descriptor.capabilities.start_turn is True
    assert descriptor.capabilities.resume_turn is False
    assert descriptor.capabilities.cancel_turn is False
    assert descriptor.capabilities.event_stream is False
    assert descriptor.capabilities.command_approval_callback is False


def test_registry_rejects_unknown_provider_without_aliases():
    assert get_coding_agent("other-agent") is None
    assert get_coding_agent_provider("other-agent") is None
    assert get_coding_agent("Codex") is None

    with pytest.raises(UnknownCodingAgentProviderError, match="unapproved"):
        require_coding_agent("other-agent")
    with pytest.raises(UnknownCodingAgentProviderError, match="unapproved"):
        require_coding_agent_provider("other-agent")


def test_descriptor_and_capabilities_are_frozen():
    descriptor = require_coding_agent("codex")

    with pytest.raises(FrozenInstanceError):
        descriptor.adapter = "tampered"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        descriptor.capabilities.start_turn = True  # type: ignore[misc]


def test_capability_snapshots_are_deterministic_defensive_copies():
    descriptor = require_coding_agent("codex")
    first = descriptor.capability_snapshot()
    second = descriptor.capability_snapshot()

    assert first == EXPECTED_CODEX_SNAPSHOT
    assert second == EXPECTED_CODEX_SNAPSHOT
    assert first is not second

    first["adapter"] = "tampered"
    first["event_stream"] = True
    listed = list_coding_agent_capability_snapshots()
    assert listed == [EXPECTED_CODEX_SNAPSHOT]
    listed[0]["adapter"] = "tampered-again"
    assert list_coding_agent_capability_snapshots() == [EXPECTED_CODEX_SNAPSHOT]


def test_runtime_capabilities_are_truthful_and_separate_from_task_contract():
    first = list_coding_agent_runtime_capability_snapshots()
    second = list_coding_agent_runtime_capability_snapshots()

    assert first == [EXPECTED_CODEX_RUNTIME_SNAPSHOT]
    assert second == [EXPECTED_CODEX_RUNTIME_SNAPSHOT]
    assert first is not second
    assert first[0] is not second[0]
    first[0]["operations"]["resume_turn"] = True
    assert list_coding_agent_runtime_capability_snapshots() == [
        EXPECTED_CODEX_RUNTIME_SNAPSHOT
    ]
    assert "start_turn" not in EXPECTED_CODEX_SNAPSHOT
    assert "cancel_turn" not in EXPECTED_CODEX_SNAPSHOT


def test_codex_exec_provider_builds_only_the_reviewed_one_shot_launch():
    provider = require_coding_agent_provider("codex")
    offline = provider.start_turn(CodingAgentTurnRequest(network_access=False))
    online = provider.start_turn(CodingAgentTurnRequest(network_access=True))

    assert offline.safe_metadata() == {
        "provider_id": "codex",
        "adapter": "codex-exec-v1",
        "execution_mode": "single_turn_process",
        "outputs": {
            "final_response": True,
            "checkpoint": False,
            "event_stream": False,
            "machine_event_log": True,
        },
    }
    assert "shell_command" not in offline.safe_metadata()
    assert offline.shell_command == (
        '  codex exec --cd "$REPO_DIR" --sandbox workspace-write '
        "-c approval_policy=never --json \\\n"
        '    -o "$TASK_DIR/final_message.txt" '
        '- < "$TASK_DIR/instruction.txt" > "$TASK_DIR/codex.jsonl"'
    )
    assert online.shell_command == (
        '  codex exec --cd "$REPO_DIR" --sandbox workspace-write '
        "-c approval_policy=never --json \\\n"
        '    -o "$TASK_DIR/final_message.txt" '
        "-c sandbox_workspace_write.network_access=true "
        '- < "$TASK_DIR/instruction.txt" > "$TASK_DIR/codex.jsonl"'
    )
    assert offline.outputs.final_response_file == "final_message.txt"
    assert offline.outputs.checkpoint_file is None
    assert offline.outputs.event_stream is False
    assert offline.outputs.machine_event_log_file == "codex.jsonl"


def test_codex_exec_provider_fails_closed_for_unimplemented_lifecycle_methods():
    provider = require_coding_agent_provider("codex")
    handle = CodingAgentCommandApprovalHandle(
        request_id=42,
        engineering_task_id="opaque-task",
        attempt_number=1,
        parent_approval_id=7,
        thread_id="opaque-thread",
        turn_id="opaque-turn",
        item_id="opaque-item",
        provider_approval_id=None,
        command_digest="a" * 64,
        working_directory="/approved/worktree",
    )

    with pytest.raises(CodingAgentCapabilityUnavailableError, match="resume_turn"):
        provider.resume_turn(thread_id="opaque-thread", instruction="continue")
    with pytest.raises(CodingAgentCapabilityUnavailableError, match="cancel_turn"):
        provider.cancel_turn(thread_id="opaque-thread", turn_id="opaque-turn")
    with pytest.raises(CodingAgentCapabilityUnavailableError, match="event_stream"):
        provider.stream_events(thread_id="opaque-thread", turn_id="opaque-turn")
    with pytest.raises(
        CodingAgentCapabilityUnavailableError, match="command_approval_callback"
    ):
        provider.respond_to_command_approval(
            handle=handle,
            decision=CodingAgentCommandApprovalDecision.DECLINE,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("request_id", True, "request id"),
        ("command_digest", "short", "SHA-256"),
        ("working_directory", "relative/repo", "absolute"),
        ("attempt_number", 0, "positive"),
    ],
)
def test_command_approval_handle_rejects_ambiguous_correlation(
    field, value, message
):
    values = {
        "request_id": "rpc-request",
        "engineering_task_id": "opaque-task",
        "attempt_number": 1,
        "parent_approval_id": 7,
        "thread_id": "opaque-thread",
        "turn_id": "opaque-turn",
        "item_id": "opaque-item",
        "provider_approval_id": None,
        "command_digest": "b" * 64,
        "working_directory": "/approved/worktree",
    }
    values[field] = value
    with pytest.raises(ValueError, match=message):
        CodingAgentCommandApprovalHandle(**values)


def test_legacy_engineering_provider_exports_delegate_to_registry():
    assert ENGINEERING_TASK_PROVIDER_ID == CODEX_AGENT_PROVIDER_ID
    first = codex_provider_capability_snapshot()
    second = codex_provider_capability_snapshot()
    assert first == EXPECTED_CODEX_SNAPSHOT
    assert second == EXPECTED_CODEX_SNAPSHOT
    assert first is not second
