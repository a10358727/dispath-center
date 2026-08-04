"""The canary evaluator must fail closed on bad evidence.

A reporting script that says PASS when the window was not actually clean is
worse than no script at all, so these tests drive it from constructed database
states rather than trusting its output shape.
"""

from __future__ import annotations

import pathlib
import json
import sqlite3
import subprocess
import sys


from app.db import Database
from scripts.canary_report import CONTRACT_VERSION, REQUIRED_DRILLS, collect, evaluate

# The suite runs with a temporary working directory, so the script must be
# addressed absolutely rather than relative to cwd.
_SCRIPT = str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "canary_report.py")
_SINCE = "2026-07-28T00:00:00Z"
_THROUGH = "2026-07-28T08:00:00Z"
_SERVER = "compute-a"


def _evidence() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "environment": "non-production",
        "candidate_commit": "a" * 40,
        "server_name": _SERVER,
        "since": _SINCE,
        "through": _THROUGH,
        "drills": {
            name: {
                "passed": True,
                "observed_at": "2026-07-28T04:00:00Z",
                "evidence_ref": f"evidence/{name}.json",
            }
            for name in REQUIRED_DRILLS
        },
    }


def _db(tmp_path):
    path = tmp_path / "canary.db"
    Database(str(path))
    return path


def _insert_attempt(conn, attempt_id, job_id, state, liveness="known", **extra):
    with_collect = extra.pop("with_collect", True)
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
    if state in {"done", "failed"} and with_collect:
        conn.execute(
            """
            INSERT INTO execution_operations
                (id, attempt_id, operation, idempotency_key,
                 authorization_approval_id, authorization_class,
                 authorized_contract_sha256, payload_json, payload_sha256,
                 state, attempt_count, created_at, updated_at)
            VALUES (?, ?, 'collect', ?, 1, 'execution', 'sha',
                    '{}', 'payload-sha', 'delivered', 1, ?, ?)
            """,
            (
                f"collect-{attempt_id}",
                attempt_id,
                f"collect-key-{attempt_id}",
                "2026-07-28T02:00:00Z",
                "2026-07-28T02:00:01Z",
            ),
        )
    conn.commit()


def _metrics(conn):
    return collect(conn, _SINCE, _THROUGH, _SERVER)


def test_clean_window_passes(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for i in range(20):
        _insert_attempt(conn, f"a{i}", i + 1, "done", exit_code=0)
    metrics = _metrics(conn)
    results = evaluate(metrics, _evidence(), min_jobs=20)
    assert all(ok for _, ok, _ in results), [r for r in results if not r[1]]


def test_exact_window_boundary_normalizes_equivalent_utc_encodings(tmp_path):
    """SQLite text ordering treats ``+00:00`` as less than ``Z``.

    Runtime timestamps use the former while the evidence contract requires
    the latter, so the evaluator must compare instants rather than raw text or
    it silently drops an attempt created exactly at the window start.
    """

    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _insert_attempt(
        conn,
        "boundary",
        1,
        "done",
        exit_code=0,
        created_at="2026-07-28T00:00:00+00:00",
    )

    metrics = _metrics(conn)

    assert metrics["attempts_total"] == 1
    assert metrics["terminal_attempts"] == 1
    assert metrics["collect_delivered"] == 1


def test_too_few_jobs_fails(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for i in range(3):
        _insert_attempt(conn, f"a{i}", i + 1, "done", exit_code=0)
    results = evaluate(_metrics(conn), _evidence(), min_jobs=20)
    assert not all(ok for _, ok, _ in results)


def test_unresolved_unknown_attempt_fails(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for i in range(20):
        _insert_attempt(conn, f"a{i}", i + 1, "done", exit_code=0)
    _insert_attempt(conn, "stuck", 999, "dispatching", liveness="unknown")
    results = dict((name, ok) for name, ok, _ in evaluate(
        _metrics(conn), _evidence(), min_jobs=20
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
        _metrics(conn), _evidence(), min_jobs=1
    ))
    assert results["zero duplicate launches"] is False


def test_missing_collection_cannot_count_as_one_hundred_percent(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for i in range(20):
        _insert_attempt(
            conn,
            f"a{i}",
            i + 1,
            "done",
            exit_code=0,
            with_collect=False,
        )

    metrics = _metrics(conn)
    results = {
        name: ok
        for name, ok, _ in evaluate(metrics, _evidence(), min_jobs=20)
    }

    assert metrics["collect_operations"] == 0
    assert results["result collection 100%"] is False


def test_short_window_and_missing_drill_fail(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _insert_attempt(conn, "a1", 1, "done", exit_code=0)
    metrics = collect(
        conn,
        _SINCE,
        "2026-07-28T07:59:59Z",
        _SERVER,
    )
    evidence = _evidence()
    evidence["through"] = "2026-07-28T07:59:59Z"
    del evidence["drills"]["forced_response_loss"]
    results = {
        name: ok
        for name, ok, _ in evaluate(metrics, evidence, min_jobs=1)
    }

    assert results["window is at least 8 hours"] is False
    assert results["drill passed: forced_response_loss"] is False


def test_v1_manifest_is_not_silently_reinterpreted(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for i in range(20):
        _insert_attempt(conn, f"a{i}", i + 1, "done", exit_code=0)
    evidence = _evidence()
    evidence["contract_version"] = "ssh-canary-evidence-v1"
    results = {
        name: ok
        for name, ok, _ in evaluate(_metrics(conn), evidence, min_jobs=20)
    }
    assert results["evidence contract is pinned"] is False


def _write_evidence(tmp_path, evidence=None):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence or _evidence()), encoding="utf-8")
    return path


def test_unreadable_database_exits_unusable(tmp_path):
    evidence_path = _write_evidence(tmp_path)
    proc = subprocess.run(
        [sys.executable, _SCRIPT, "--db",
         str(tmp_path / "missing.db"), "--since", _SINCE,
         "--through", _THROUGH, "--server", _SERVER,
         "--evidence", str(evidence_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "UNUSABLE" in proc.stderr


def test_report_never_writes_to_the_database(tmp_path):
    path = _db(tmp_path)
    evidence_path = _write_evidence(tmp_path)
    before = path.read_bytes()
    proc = subprocess.run(
        [sys.executable, _SCRIPT, "--db", str(path),
         "--since", _SINCE, "--through", _THROUGH,
         "--server", _SERVER, "--evidence", str(evidence_path),
         "--json"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["passed"] is False
    assert path.read_bytes() == before
