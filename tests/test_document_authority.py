from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_product_v2_execution_plan_mirror_is_byte_identical():
    assert (REPOSITORY_ROOT / "PLAN.md").read_bytes() == (
        REPOSITORY_ROOT / "docs" / "DISPATCH_CENTER_PRODUCT_V2_EXECUTION_PLAN.md"
    ).read_bytes()


def test_product_brief_defers_to_the_product_v2_execution_authority():
    brief = _read("docs/DISPATCH_CENTER_CURRENT_SYSTEM_AND_IMPROVEMENT_PLAN.md")
    for marker in (
        "Product Brief",
        "[`PLAN.md`](../PLAN.md)",
        "DISPATCH_CENTER_PRODUCT_V2_EXECUTION_PLAN.md",
        "[`CAPABILITY_LEDGER.md`](CAPABILITY_LEDGER.md)",
        "不是目前能力、工作包順序或部署狀態的權威來源",
        "Product v2 execution plan §12",
    ):
        assert marker in brief


def test_product_v2_baseline_decision_is_recorded_without_rewriting_history():
    decisions = _read("docs/DECISIONS.md")
    assert "DG-PRODUCT-V2-BASELINE-v1" in decisions
    assert "保留 2026-07-16 D7 的原文與當時範圍" in decisions
    for marker in (
        "Authorization enforcement",
        "Multi-role RBAC v2",
        "API v2 cutover",
        "Project bootstrap",
        "Dataset sharing",
    ):
        assert marker in decisions


def test_api_v2_foundation_decision_preserves_default_off_non_product_scope():
    decisions = _read("docs/DECISIONS.md")
    section = decisions.split("DG-API-V2-FOUNDATION-v1", 1)[1]
    for marker in (
        "`API_V2_ENABLED` 預設 `false`",
        "不含 probe 或產品 endpoint",
        "BEGIN IMMEDIATE",
        "Migration v5",
        "不新增任何 Product v2",
    ):
        assert marker in section


def test_product_rbac_decision_records_legacy_evidence_and_opaque_lookup():
    decisions = _read("docs/DECISIONS.md")
    section = decisions.split("DG-PRODUCT-RBAC-V2-v1", 1)[1]
    for marker in (
        "`project_memberships.updated_at`",
        "不代表原始授權或人工核准時間",
        "direct DB helpers 不推導 provenance",
        "generic `404 not_found`",
        "`PRODUCT_RBAC_V2_ENABLED`",
    ):
        assert marker in section

    migrations = _read("docs/MIGRATIONS.md")
    for marker in (
        "schema version 9",
        "`grant_approval_id=NULL`",
        "last observed update time",
        "does **not** prove the role's original grant",
        "low-level legacy/test compatibility primitives",
    ):
        assert marker in migrations


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
