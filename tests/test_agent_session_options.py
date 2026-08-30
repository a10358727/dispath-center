"""DG-STUDIO-UI v1 Phase 2: per-session SDK options are a closed vocabulary and
can never switch the workspace prompts off (INV-AGENT-2)."""

import pytest

from app.agent_session_options import InvalidSessionOptionsError, merge_session_options, normalize_session_options


def test_normalize_accepts_the_closed_vocabulary():
    assert normalize_session_options(None) == {}
    assert normalize_session_options({}) == {}
    assert normalize_session_options({"model": " opus ", "effort": "high", "thinking": "adaptive", "permission_mode": "plan"}) == {
        "model": "opus", "effort": "high", "thinking": "adaptive", "permission_mode": "plan",
    }
    assert normalize_session_options({"thinking": {"budget_tokens": 16000}}) == {"thinking": {"budget_tokens": 16000}}
    assert normalize_session_options({"model": "claude-opus-4-1-20250805", "permission_mode": None}) == {"model": "claude-opus-4-1-20250805"}


@pytest.mark.parametrize(
    "raw",
    [
        {"permission_mode": "bypassPermissions"},
        {"permission_mode": "dontAsk"},
        {"permission_mode": "auto"},
        {"permission_mode": "yolo"},
        {"model": "opus; rm -rf /"},
        {"model": ""},
        {"effort": "extreme"},
        {"thinking": "lots"},
        {"thinking": {"budget_tokens": 10}},
        {"thinking": {"budget_tokens": True}},
        {"thinking": {"budget_tokens": 2048, "display": "raw"}},
        {"bogus": 1},
        "opus",
    ],
)
def test_normalize_rejects_everything_else(raw):
    with pytest.raises(InvalidSessionOptionsError):
        normalize_session_options(raw)


def test_merge_keeps_unrelated_options_and_revalidates():
    merged = merge_session_options({"model": "sonnet", "effort": "low"}, {"permission_mode": "acceptEdits"})
    assert merged == {"model": "sonnet", "effort": "low", "permission_mode": "acceptEdits"}
    with pytest.raises(InvalidSessionOptionsError):
        merge_session_options(merged, {"permission_mode": "bypassPermissions"})
