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


def test_readme_and_service_template_disclose_missing_node_entrypoint():
    readme = _read("README.md")
    service = _read("agent/dispatch-node-agent.service")
    assert "agent/__main__.py` 尚不存在" in readme
    assert "TEMPLATE ONLY" in service
    assert "agent/__main__.py is absent" in service
