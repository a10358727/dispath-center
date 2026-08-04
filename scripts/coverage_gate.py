"""Run the deterministic coverage baseline used by CI.

Coverage tracing currently deadlocks the legacy FastAPI composition root's
threaded ``TestClient`` lifespan and the mailer's ``asyncio.to_thread`` fake.
Those integration tests still run in the complete offline suite; this gate uses
the broad pure-core subset that has stable tracing semantics. Router extraction
and worker separation should make the exclusion unnecessary over time.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COVERAGE_THRESHOLD = 35

COVERAGE_TESTS = (
    "tests/test_authorization.py",
    "tests/test_authorization_shadow.py",
    "tests/test_migrations.py",
    "tests/test_durable_audit.py",
    "tests/test_execution_launch_arbitration.py",
    "tests/test_execution_plan.py",
    "tests/test_node_protocol.py",
    "tests/test_dataset_snapshot.py",
    "tests/test_server_publication.py",
    "tests/test_engineering_path_policy.py",
    "tests/test_security.py",
    "tests/test_scheduler.py",
    "tests/test_db_migration.py",
    "tests/test_execution_attempt_dispatch.py",
    "tests/test_node_agent_daemon.py",
    "tests/test_node_invariants_static.py",
    "tests/test_coding_agents.py",
    "tests/test_codex_app_server.py",
    "tests/test_execution_backend.py",
    "tests/test_config.py",
    "tests/test_typed_settings.py",
    "tests/test_identity.py",
    "tests/test_oidc_provider.py",
    "tests/test_autoapprove.py",
    "tests/test_jobqueue.py",
    "tests/test_server_config.py",
    "tests/test_monitor.py",
    "tests/test_results.py",
    "tests/test_stall.py",
    "tests/test_control_plane_cli.py",
    "tests/test_unit_of_work.py",
)


def build_pytest_args() -> list[str]:
    return [
        "-q",
        *COVERAGE_TESTS,
        "--cov=app",
        "--cov=agent",
        "--cov=dispatch_center",
        "--cov-report=term-missing",
        "--cov-report=xml",
        f"--cov-fail-under={COVERAGE_THRESHOLD}",
    ]


def main() -> int:
    os.chdir(REPOSITORY_ROOT)
    return int(pytest.main(build_pytest_args()))


if __name__ == "__main__":
    raise SystemExit(main())
