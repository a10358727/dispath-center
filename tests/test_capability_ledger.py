"""Capability ledger format rules (DG-CONSOLIDATION-v1 C-4).

The ledger is validated by rule, not by per-row literal pins: columns, allowed
values, a ruling reference per row, one-line evidence, and the two safety
statements that must stay true (node rows off until DG-NODE-CANARY; no canary
claim without an evidence file).
"""

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPOSITORY_ROOT / "docs" / "CAPABILITY_LEDGER.md"
HEADER = "| Capability | Ruling | Implemented | Default | Pilot | Canary | Evidence |"
COLUMNS = ("ruling", "implemented", "default", "pilot", "canary", "evidence")
ALLOWED = {
    "implemented": {"yes", "test-only", "partial", "no", "retired", "retiring"},
    "default": {"on", "off", "n/a"},
    "pilot": {"on", "off", "n/a"},
    "canary": {"yes", "no", "n/a"},
}
RULING_RE = re.compile(r"(DG-[A-Z0-9-]+|\bD[1-7]\b|D-[1-9]|WP-[0-9A-Z]+|Slice \d+|Goal \d|—)")
MAX_EVIDENCE_CHARS = 160


def _ledger_rows() -> dict[str, dict[str, str]]:
    lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    rows: dict[str, dict[str, str]] = {}
    in_table = False
    for line in lines:
        if line.strip() == HEADER:
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and not line.startswith("|"):
            in_table = False
            continue
        if not in_table:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) == 7, line
        capability = cells[0].strip("`").split(" ")[0]
        assert capability not in rows, f"duplicate row {capability}"
        rows[capability] = dict(zip(COLUMNS, cells[1:], strict=True))
    return rows


def test_capability_ledger_uses_the_fixed_columns_and_values():
    rows = _ledger_rows()
    assert len(rows) >= 30
    for capability, row in rows.items():
        for column, allowed in ALLOWED.items():
            assert row[column] in allowed, (capability, column, row[column])
        assert RULING_RE.search(row["ruling"]), (capability, row["ruling"])
        assert 0 < len(row["evidence"]) <= MAX_EVIDENCE_CHARS, (capability, len(row["evidence"]))


def test_production_ready_is_not_a_column_and_the_authority_chain_is_stated():
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    assert "production-ready" not in ledger.split("## Active")[1]
    assert "`docs/PLATFORM_CHARTER.md` (§6 invariants) and named decisions" in ledger
    assert "this ledger →\n> `docs/product/ROADMAP.md` → `docs/archive/` historical" in ledger


def test_node_rows_stay_off_until_dg_node_canary():
    rows = _ledger_rows()
    for capability in ("node_protocol_v1", "node_protocol_v2", "node_daemon", "workload_isolation_v1"):
        assert rows[capability]["pilot"] == "off", capability
        assert rows[capability]["canary"] == "no", capability
        assert rows[capability]["default"] == "off", capability


def test_canary_yes_requires_an_evidence_file_link():
    rows = _ledger_rows()
    for capability, row in rows.items():
        if row["canary"] == "yes":
            match = re.search(r"docs/evidence/[A-Za-z0-9_.-]+", row["evidence"])
            assert match, capability
            assert (REPOSITORY_ROOT / match.group(0)).exists(), capability


def test_pilot_posture_rows_are_marked_personal_pilot_only():
    rows = _ledger_rows()
    assert rows["authorization_enforcement"]["pilot"] == "on"
    assert "personal-pilot only" in rows["authorization_enforcement"]["evidence"]
    assert rows["authorization_enforcement"]["canary"] == "no"
    assert rows["attempt_driven_ssh"]["canary"] == "no"
    assert "docs/evidence/WP2D_V2_20260802_D73A38E.md" in rows["attempt_driven_ssh"]["evidence"]
