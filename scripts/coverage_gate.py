"""Run the deterministic coverage baseline used by CI.

Coverage tracing currently deadlocks the legacy FastAPI composition root's
threaded ``TestClient`` lifespan and the mailer's ``asyncio.to_thread`` fake.
Those integration tests still run in the complete offline suite; this gate
traces everything else. Selection is the ``untraced`` marker that
``tests/conftest.py`` derives from each test (``api_client`` fixture use or a
``TestClient(`` / ``asyncio.to_thread`` reference in the module), so no
hand-kept module list exists any more (整頓 C3, DG-CONSOLIDATION-v1 C-1).
Router extraction and worker separation should make the exclusion unnecessary
over time.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COVERAGE_THRESHOLD = 35
COVERAGE_SELECTION = "not untraced"


def build_pytest_args() -> list[str]:
    return [
        "-q",
        "tests/",
        "-m",
        COVERAGE_SELECTION,
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
