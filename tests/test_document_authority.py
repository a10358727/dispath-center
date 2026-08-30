import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_product_brief_defers_to_the_product_v2_execution_authority():
    brief = _read("docs/archive/DISPATCH_CENTER_CURRENT_SYSTEM_AND_IMPROVEMENT_PLAN.md")
    for marker in (
        "Product Brief",
        "superseded_by:",
        "docs/PLATFORM_CHARTER.md",
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

    migrations = _read("docs/reference/MIGRATIONS.md")
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
        "docs/archive/CURRENT_STATE.md",
        "docs/archive/GOAL_3_COMPLETION_STATUS.md",
        "docs/archive/GOAL_3_FUTURE_WORK_PLAN.md",
        "docs/decisions/DG_C_INVARIANT_REVISION_DRAFT.md",
        "docs/archive/AGENT_SESSION_V1_PLAN.md",
        "docs/archive/DISPATCH_CENTER_CURRENT_SYSTEM_AND_IMPROVEMENT_PLAN.md",
        "docs/archive/FULL_PLATFORM_SECOND_PASS_PLAN.md",
        "docs/archive/GOAL_2_AUTOMATED_DISPATCH_PLAN.md",
        "docs/archive/IMPLEMENTATION_PROGRESS.md",
        "docs/archive/NEXT_IMPLEMENTATION_PLAN.md",
        "docs/archive/PERSONAL_PILOT_PLAN.md",
        "docs/archive/PRODUCT_V2_EXECUTION_PLAN.md",
        "docs/archive/STAGE_ACCEPTANCE_MANUAL.md",
        "docs/archive/GOAL_1_IMPLEMENTATION_PLAN.md",
        "docs/archive/TESTING_REPORT.md",
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



_DOC_TOKEN_RE = re.compile(r"docs/[A-Za-z0-9_./-]+\.(?:md|json)")
_CLAUDE_TOKEN_RE = re.compile(r"\.claude/[A-Za-z0-9_./-]+\.md")

_DOCS_RELATIVE_TOKEN_RE = re.compile(
    r"^(?:decisions|archive|reference|runbooks|product|evidence|examples)/"
    r"[A-Za-z0-9_./-]+\.(?:md|json)$"
)


def _strip_trailing_punctuation(token: str) -> str:
    return token.rstrip("。）)，,；;：:'、")


def test_live_documents_reference_existing_paths():
    live_paths = [
        "README.md",
        "CLAUDE.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "docs/PLATFORM_CHARTER.md",
        "docs/CAPABILITY_LEDGER.md",
        "docs/product/ROADMAP.md",
        "docs/decisions/README.md",
        "docs/archive/README.md",
    ]
    live_paths.extend(
        str(path.relative_to(REPOSITORY_ROOT))
        for path in sorted((REPOSITORY_ROOT / ".claude" / "skills").rglob("*.md"))
    )
    live_paths.extend(
        str(path.relative_to(REPOSITORY_ROOT))
        for path in sorted((REPOSITORY_ROOT / ".claude" / "agents").rglob("*.md"))
    )

    missing = []
    for relative_path in live_paths:
        text = _read(relative_path)
        tokens = set()
        for match in _DOC_TOKEN_RE.finditer(text):
            tokens.add(_strip_trailing_punctuation(match.group(0)))
        for match in _CLAUDE_TOKEN_RE.finditer(text):
            tokens.add(_strip_trailing_punctuation(match.group(0)))

        is_under_docs = relative_path.startswith("docs/")
        if is_under_docs:
            for line in text.splitlines():
                for backtick_match in re.finditer(r"`([^`]+)`", line):
                    candidate = _strip_trailing_punctuation(backtick_match.group(1))
                    if _DOCS_RELATIVE_TOKEN_RE.match(candidate):
                        tokens.add("docs/" + candidate)

        for token in sorted(tokens):
            if not (REPOSITORY_ROOT / token).exists():
                missing.append(f"{relative_path} -> {token}")

    assert not missing, "missing referenced paths:\n" + "\n".join(missing)
