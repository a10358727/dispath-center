"""The canary evaluator must fail closed on bad evidence.

A reporting script that says PASS when the window was not actually clean is
worse than no script at all, so these tests drive it from constructed database
states rather than trusting its output shape.
"""

from __future__ import annotations

import pathlib
import sqlite3
import subprocess
import sys

import pytest

from app.db import Database
from scripts.canary_report import collect, evaluate

# The suite runs with a temporary working directory, so the script must be
# addressed absolutely rather than relative to cwd.
_SCRIPT = str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "canary_report.py")


def _db(tmp_path):
    path = tmp_path / "canary.db"
    Database(str(path))
    return path


def _insert_attempt(conn, attempt_id, job_id, state, liveness="known", **extra):
    columns = {
        "id": attempt_id,
        "job_id": job_id,
        "attempt_number": extra.get("attempt_number", 1),
        "backend": "ssh",
        "server_name": "compute-a",
        "server_config_revision_id": "rev-1",
        "target_identity_sha256": "sha",
        "execution_approval_id": 1,
        "approved_payload_sha256": "sha",
        "execution_contract_version": "v1",
        "state": state,
        "liveness": liveness,
        "fencing_token": f"tok-{attempt_id}",
        "scheduler_fencing_epoch": 1,
        "created_at": "2026-07-28T01:00:00Z",
    }
    columns.update({k: v for k, v in extra.items() if k != "attempt_number"})
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        f"INSERT INTO execution_attempts ({','.join(columns)})"
        f" VALUES ({','.join('?' * len(columns))})",
        tuple(columns.values()),
    )
    conn.commit()


def test_clean_window_passes(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for i in range(20):
        _insert_attempt(conn, f"a{i}", i + 1, "done", exit_code=0)
    metrics = collect(conn, "2026-07-28T00:00:00Z")
    results = evaluate(metrics, min_jobs=20)
    assert all(ok for _, ok, _ in results), [r for r in results if not r[1]]


def test_too_few_jobs_fails(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for i in range(3):
        _insert_attempt(conn, f"a{i}", i + 1, "done", exit_code=0)
    results = evaluate(collect(conn, "2026-07-28T00:00:00Z"), min_jobs=20)
    assert not all(ok for _, ok, _ in results)


def test_unresolved_unknown_attempt_fails(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for i in range(20):
        _insert_attempt(conn, f"a{i}", i + 1, "done", exit_code=0)
    _insert_attempt(conn, "stuck", 999, "dispatching", liveness="unknown")
    results = dict((name, ok) for name, ok, _ in evaluate(
        collect(conn, "2026-07-28T00:00:00Z"), min_jobs=20
    ))
    assert results["zero unresolved unknown attempts"] is False
    assert results["all attempts converged"] is False


def test_duplicate_launcher_claim_fails(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _insert_attempt(
        conn, "d1", 1, "done", exit_code=0, remote_claim_state="launcher_claimed"
    )
    _insert_attempt(
        conn,
        "d2",
        1,
        "done",
        exit_code=0,
        attempt_number=2,
        remote_claim_state="launcher_claimed",
    )
    results = dict((name, ok) for name, ok, _ in evaluate(
        collect(conn, "2026-07-28T00:00:00Z"), min_jobs=1
    ))
    assert results["zero duplicate launches"] is False


def test_unreadable_database_exits_unusable(tmp_path):
    proc = subprocess.run(
        [sys.executable, _SCRIPT, "--db",
         str(tmp_path / "missing.db"), "--since", "2026-07-28T00:00:00Z"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "UNUSABLE" in proc.stderr


def test_report_never_writes_to_the_database(tmp_path):
    path = _db(tmp_path)
    before = path.read_bytes()
    proc = subprocess.run(
        [sys.executable, _SCRIPT, "--db", str(path),
         "--since", "2026-07-28T00:00:00Z"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1)
    assert path.read_bytes() == before
