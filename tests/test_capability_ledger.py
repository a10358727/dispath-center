from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPOSITORY_ROOT / "docs" / "CAPABILITY_LEDGER.md"
STATUS_FIELDS = (
    "implemented",
    "test-only",
    "default-enabled",
    "deployed",
    "canary-proven",
    "production-ready",
)
ALLOWED_STATUS = {"yes", "no", "unknown", "n/a"}


def _ledger_rows() -> dict[str, dict[str, str]]:
    lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    header = (
        "| Capability ID | implemented | test-only | default-enabled | deployed "
        "| canary-proven | production-ready | Evidence / blocker |"
    )
    header_index = lines.index(header)
    rows: dict[str, dict[str, str]] = {}
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == 8
        capability = cells[0].strip("`")
        assert capability not in rows
        statuses = dict(zip(STATUS_FIELDS, cells[1:7], strict=True))
        assert set(statuses.values()) <= ALLOWED_STATUS
        rows[capability] = statuses
    return rows


def test_capability_ledger_uses_the_fixed_status_fields_and_values():
    rows = _ledger_rows()
    assert len(rows) >= 15


def test_node_truth_does_not_confuse_primitives_with_a_runnable_daemon():
    rows = _ledger_rows()
    assert rows["node_protocol_v1"] == {
        "implemented": "yes",
        "test-only": "yes",
        "default-enabled": "no",
        "deployed": "no",
        "canary-proven": "no",
        "production-ready": "no",
    }
    # Phase 4 supplied a runnable daemon. Everything that would let it
    # actually take work must still be no: activation is gated by DG-NODE-V2.
    assert rows["node_daemon"] == {
        "implemented": "yes",
        "test-only": "yes",
        "default-enabled": "no",
        "deployed": "no",
        "canary-proven": "no",
        "production-ready": "no",
    }


def test_execution_foundation_is_test_only_and_future_cutovers_remain_absent():
    rows = _ledger_rows()
    assert rows["execution_attempt_outbox"] == {
        "implemented": "yes",
        "test-only": "yes",
        "default-enabled": "no",
        "deployed": "no",
        "canary-proven": "no",
        "production-ready": "no",
    }
    # WP-2C landed the attempt-driven SSH cutover, so `implemented` is now yes.
    # Everything that would make it an operable production path must still be
    # no: code existing is not deployment, and local tests are never canary
    # evidence.
    assert rows["attempt_driven_ssh"] == {
        "implemented": "yes",
        "test-only": "yes",
        "default-enabled": "no",
        "deployed": "no",
        "canary-proven": "no",
        "production-ready": "no",
    }
    # WP-3A/3B consume the content-addressed snapshot in immutable plans, so
    # `implemented` is yes. Production operation must still be no: rollout
    # flags remain default off and no real dataset/canary evidence exists.
    assert rows["immutable_dataset_snapshot"] == {
        "implemented": "yes",
        "test-only": "yes",
        "default-enabled": "no",
        "deployed": "no",
        "canary-proven": "no",
        "production-ready": "no",
    }
    # WP-3B plus the current continuation expose an operable request,
    # materialization and lineage API, so it is no longer a fake/test-only
    # capability. Deployment/canary evidence is still absent.
    assert rows["immutable_execution_plan"] == {
        "implemented": "yes",
        "test-only": "no",
        "default-enabled": "no",
        "deployed": "no",
        "canary-proven": "no",
        "production-ready": "no",
    }
    assert rows["code_promotion_v1"] == {
        "implemented": "yes",
        "test-only": "no",
        "default-enabled": "no",
        "deployed": "no",
        "canary-proven": "no",
        "production-ready": "no",
    }
    # WP-4A landed the v2 protocol, so `implemented` is now yes. Everything
    # that would let a node take real work must still be no: activation needs
    # DG-NODE-CANARY, and no node is enrolled.
    assert rows["node_protocol_v2"] == {
        "implemented": "yes",
        "test-only": "yes",
        "default-enabled": "no",
        "deployed": "no",
        "canary-proven": "no",
        "production-ready": "no",
    }


def test_phase0_gate_and_legacy_ssh_are_reported_without_deployment_guessing():
    rows = _ledger_rows()
    assert rows["phase0_release_gate"]["implemented"] == "yes"
    assert rows["phase0_release_gate"]["deployed"] == "unknown"
    assert rows["ssh_execution_v1"]["default-enabled"] == "yes"
    assert rows["ssh_execution_v1"]["deployed"] == "unknown"
    assert rows["ssh_execution_v1"]["production-ready"] == "no"
