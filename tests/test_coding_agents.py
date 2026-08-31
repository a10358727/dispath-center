"""Phase 1b registry pins: the reviewed exec adapters are retired — the
registry keeps their descriptors for historical rows, refuses unknown ids,
and never again exposes a runtime adapter or an experimental registry."""

import dataclasses

import pytest

from app.coding_agents import (
    CLAUDE_CODE_AGENT_PROVIDER_ID,
    CODEX_AGENT_PROVIDER_ID,
    UnknownCodingAgentProviderError,
    get_coding_agent,
    list_coding_agent_capability_snapshots,
    list_coding_agent_runtime_capability_snapshots,
    list_coding_agents,
    require_coding_agent,
)


def test_registry_holds_exactly_the_two_historical_descriptors():
    descriptors = list_coding_agents()
    assert [d.provider_id for d in descriptors] == [CLAUDE_CODE_AGENT_PROVIDER_ID, CODEX_AGENT_PROVIDER_ID]
    assert {d.adapter for d in descriptors} == {"claude-code-v1", "codex-exec-v1"}
    assert get_coding_agent("codex").display_name == "Codex"
    assert get_coding_agent("nope") is None
    with pytest.raises(UnknownCodingAgentProviderError):
        require_coding_agent("codex-app-server")


def test_descriptors_are_frozen_and_snapshots_are_defensive_copies():
    descriptor = require_coding_agent(CODEX_AGENT_PROVIDER_ID)
    with pytest.raises(dataclasses.FrozenInstanceError):
        descriptor.display_name = "hacked"  # type: ignore[misc]
    first = list_coding_agent_capability_snapshots()
    first[0]["adapter"] = "tampered"
    assert list_coding_agent_capability_snapshots()[0]["adapter"] != "tampered"


def test_runtime_snapshots_are_honestly_retired():
    for snapshot in list_coding_agent_runtime_capability_snapshots():
        assert snapshot["retired"] is True
        assert snapshot["execution_mode"] == "retired"
        assert set(snapshot["operations"].values()) == {False}
