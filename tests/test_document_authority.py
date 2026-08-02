from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_historical_status_documents_name_their_current_authority():
    for relative_path in (
        "docs/CURRENT_STATE.md",
        "docs/GOAL_3_COMPLETION_STATUS.md",
        "docs/GOAL_3_FUTURE_WORK_PLAN.md",
        "docs/DG_C_INVARIANT_REVISION_DRAFT.md",
    ):
        heading = "\n".join(_read(relative_path).splitlines()[:20])
        assert "superseded_by:" in heading, relative_path
        assert "CAPABILITY_LEDGER.md" in heading, relative_path


def test_readme_and_service_template_separate_a_runnable_daemon_from_an_enabled_one():
    """Phase 0 pinned "the entrypoint is missing". Phase 4 supplied it, so the
    disclosure moves to the boundary that is still real: the daemon runs, but
    nothing authorizes it to take work."""
    readme = _read("README.md")
    service = _read("agent/dispatch-node-agent.service")
    assert "python -m agent --check" in readme
    assert "DG-NODE-V2" in readme
    assert "DG-NODE-CANARY" in readme
    assert "NODE_AGENT_V1_ENABLED" in readme
    assert "TEMPLATE ONLY" in service
    assert "DG-NODE-V2" in service
    assert "DG-NODE-CANARY" in service
