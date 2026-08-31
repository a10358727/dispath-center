"""Phase 1b: the lifted checkpoint/bundle module — legacy layout unchanged,
V3 override paths validated and used end to end."""

import pytest

from app.agent_session_bundle import (
    InvalidAgentSessionTurnInputError,
    build_checkpoint_script,
    checkpoint_bundle_remote_path,
    session_repo_dir,
)
import app.agent_session_turns as legacy

SESSION_ID = "11111111-1111-4111-8111-111111111111"
BRANCH = f"ai-session-{SESSION_ID}"
COMMIT = "a" * 40


def test_legacy_shape_is_byte_identical_to_the_original_module():
    ours = build_checkpoint_script(session_id=SESSION_ID, workspace_rel="codex_workspaces", workspace_branch=BRANCH, base_commit=COMMIT)
    theirs = legacy.build_checkpoint_script(session_id=SESSION_ID, workspace_rel="codex_workspaces", workspace_branch=BRANCH, base_commit=COMMIT)
    assert ours == theirs
    assert session_repo_dir("codex_workspaces", SESSION_ID) == legacy.session_repo_dir("codex_workspaces", SESSION_ID)
    assert checkpoint_bundle_remote_path("codex_workspaces", SESSION_ID) == legacy.checkpoint_bundle_remote_path("codex_workspaces", SESSION_ID)


def test_v3_overrides_place_the_reported_worktree_and_sibling_bundle():
    script = build_checkpoint_script(
        session_id=SESSION_ID, workspace_rel="ignored", workspace_branch=BRANCH, base_commit=COMMIT,
        repo_dir="/home/runner/dispatch_workspaces/sessions/x/repo",
        bundle_path="/home/runner/dispatch_workspaces/sessions/x/checkpoint.bundle",
    )
    assert "/home/runner/dispatch_workspaces/sessions/x/repo" in script
    assert "/home/runner/dispatch_workspaces/sessions/x/checkpoint.bundle" in script
    assert "ignored/agent_sessions" not in script


@pytest.mark.parametrize("bad", ["relative/path", "/has/../dotdot", "/bad\nnewline", "-/leading-dash", ""])
def test_v3_override_paths_fail_closed(bad):
    with pytest.raises(InvalidAgentSessionTurnInputError):
        build_checkpoint_script(
            session_id=SESSION_ID, workspace_rel="w", workspace_branch=BRANCH, base_commit=COMMIT,
            repo_dir=bad, bundle_path="/ok/checkpoint.bundle",
        )
