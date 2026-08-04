from scripts.coverage_gate import (
    COVERAGE_TESTS,
    COVERAGE_THRESHOLD,
    REPOSITORY_ROOT,
    build_pytest_args,
)


def test_coverage_gate_has_a_real_nonzero_regression_threshold():
    assert COVERAGE_THRESHOLD == 35
    assert f"--cov-fail-under={COVERAGE_THRESHOLD}" in build_pytest_args()


def test_coverage_gate_targets_are_unique_existing_test_modules():
    assert len(COVERAGE_TESTS) == len(set(COVERAGE_TESTS))
    assert len(COVERAGE_TESTS) >= 20
    for relative_path in COVERAGE_TESTS:
        target = REPOSITORY_ROOT / relative_path
        assert target.is_file(), relative_path
        assert target.name.startswith("test_")


def test_coverage_gate_keeps_tracing_in_the_stable_pure_core_subset():
    for relative_path in COVERAGE_TESTS:
        source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        assert "TestClient" not in source, relative_path
        assert "api_client" not in source, relative_path
        assert "asyncio.to_thread" not in source, relative_path


def test_coverage_gate_measures_all_runtime_packages_and_writes_xml():
    args = build_pytest_args()
    assert "--cov=app" in args
    assert "--cov=agent" in args
    assert "--cov=dispatch_center" in args
    assert "--cov-report=xml" in args
