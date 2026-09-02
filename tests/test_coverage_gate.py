from pathlib import Path

import pytest

from scripts.coverage_gate import (
    COVERAGE_SELECTION,
    COVERAGE_THRESHOLD,
    REPOSITORY_ROOT,
    build_pytest_args,
)


def test_coverage_gate_has_a_real_nonzero_regression_threshold():
    # The threshold may rise as coverage grows; it must never be lowered.
    assert COVERAGE_THRESHOLD >= 35
    assert f"--cov-fail-under={COVERAGE_THRESHOLD}" in build_pytest_args()


def test_coverage_gate_selects_by_the_untraced_marker_not_a_module_list():
    args = build_pytest_args()
    assert "tests/" in args
    assert args[args.index("-m") + 1] == COVERAGE_SELECTION == "not untraced"
    # The marker is declared so `--strict-markers` style tooling never rejects it.
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "untraced:" in pyproject


@pytest.mark.parametrize("marker", ["TestClient(", "asyncio.to_thread"])
def test_untraced_marker_is_derived_from_the_test_itself(marker, request):
    # This module references both trigger strings in its source, so conftest
    # must have marked every item in it `untraced` (the derivation is the
    # single source; there is no allowlist to keep in sync).
    assert marker in Path(__file__).read_text(encoding="utf-8")
    assert request.node.get_closest_marker("untraced") is not None


def test_coverage_gate_measures_all_runtime_packages_and_writes_xml():
    args = build_pytest_args()
    assert "--cov=app" in args
    assert "--cov=agent" in args
    assert "--cov=dispatch_center" in args
    assert "--cov-report=xml" in args
