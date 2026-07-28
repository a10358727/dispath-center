"""WP-3C: promoting Codex output into an immutable ProjectVersion.

This closes the Phase 3 loop. Without it a run can only reference a
ProjectVersion that arrived some other way, so "Codex changed the code and we
ran it" has no auditable link between the two halves.

Implemented after `DG-CODE-PROMOTE-v1` was ruled on 2026-07-28 (recorded in
docs/DECISIONS.md against reviewed-draft 510d4075). An earlier attempt was
reverted because the ruling did not yet exist.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3

import pytest

from app.config import AppConfig
from app.db import VALID_APPROVAL_KINDS, Database


def _payload(**overrides):
    payload = {
        "engineering_task_id": "task-1",
        "project_name": "demo",
        "base_project_version_id": None,
        "git_commit": "a" * 40,
        "bundle_sha256": "b" * 64,
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def database(tmp_path):
    return Database(str(tmp_path / "promote.db"))


class _State:
    def __init__(self, home):
        self.config = AppConfig(servers=[], local_home_dir=str(home))


def _write_bundle(home, task_id="task-1", content=b"bundle-bytes"):
    directory = home / "hub_bundles"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.bundle"
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_the_payload_carries_only_identifiers_and_digests(database):
    """A command, path or branch name would be a value reinterpretable at
    approve time."""
    assert database.insert_pinned_approval(
        kind="engineering_task_promote",
        contract_version="code-promotion-v1",
        payload=_payload(),
    ) > 0

    for extra in ({"command": "make deploy"}, {"bundle_path": "/tmp/x"}, {"branch": "main"}):
        with pytest.raises(ValueError, match="invalid code promotion contract"):
            database.insert_pinned_approval(
                kind="engineering_task_promote",
                contract_version="code-promotion-v1",
                payload={**_payload(), **extra},
            )


@pytest.mark.parametrize(
    "bad",
    [
        {"git_commit": "not-a-commit"},
        {"git_commit": "a" * 39},
        {"git_commit": "A" * 40},
        {"bundle_sha256": "short"},
    ],
)
def test_a_malformed_commit_or_digest_is_rejected(database, bad):
    with pytest.raises(ValueError, match="invalid code promotion contract"):
        database.insert_pinned_approval(
            kind="engineering_task_promote",
            contract_version="code-promotion-v1",
            payload={**_payload(), **bad},
        )


def test_promotion_is_registered_but_never_auto_approved():
    """P-1: any automatic path lets the system run code no human looked at."""
    import inspect

    from app.approvals import maybe_auto_approve

    assert "engineering_task_promote" in VALID_APPROVAL_KINDS
    source = inspect.getsource(maybe_auto_approve)
    assert "engineering_task_promote" not in source


# ---------------------------------------------------------------------------
# Approve-time re-verification
# ---------------------------------------------------------------------------


def _request(database, home, **overrides):
    _, digest = _write_bundle(home)
    approval_id = database.insert_pinned_approval(
        kind="engineering_task_promote",
        contract_version="code-promotion-v1",
        payload=_payload(bundle_sha256=digest, **overrides),
    )
    return approval_id


def test_promotion_creates_an_immutable_version_with_provenance(database, tmp_path):
    from app.approvals import approve

    approval_id = _request(database, tmp_path)
    result = asyncio.run(
        approve(database, approval_id, app_state=_State(tmp_path), audit_path="/dev/null")
    )

    version = result["project_version"]
    assert version["promotion_state"] == "promoted"
    assert version["promotion_approval_id"] == approval_id
    assert database.project_version_is_promoted(version["id"]) is True


def test_a_regenerated_bundle_is_rejected_even_if_the_diff_matches(database, tmp_path):
    """The digest is the identity: a bundle rebuilt between request and
    approval is a different artifact, because the claim is about bytes."""
    from app.approvals import approve

    approval_id = _request(database, tmp_path)
    _write_bundle(tmp_path, content=b"rebuilt-with-a-new-timestamp")

    result = asyncio.run(
        approve(database, approval_id, app_state=_State(tmp_path), audit_path="/dev/null")
    )

    assert result["approval"].status == "rejected"
    assert "project_version" not in result
    with database.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM project_versions")
        assert cursor.fetchone()["n"] == 0


def test_a_missing_bundle_imports_nothing(database, tmp_path):
    from app.approvals import approve

    approval_id = _request(database, tmp_path)
    os.remove(tmp_path / "hub_bundles" / "task-1.bundle")

    result = asyncio.run(
        approve(database, approval_id, app_state=_State(tmp_path), audit_path="/dev/null")
    )

    assert result["approval"].status == "rejected"
    with database.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM project_versions")
        assert cursor.fetchone()["n"] == 0


def test_promoting_the_same_commit_twice_is_a_no_op(database, tmp_path):
    """P-2: two versions for one commit makes 'which version did this run use'
    unanswerable."""
    from app.approvals import approve

    first_id = _request(database, tmp_path)
    first = asyncio.run(
        approve(database, first_id, app_state=_State(tmp_path), audit_path="/dev/null")
    )["project_version"]

    second_id = _request(database, tmp_path)
    second = asyncio.run(
        approve(database, second_id, app_state=_State(tmp_path), audit_path="/dev/null")
    )["project_version"]

    assert first["id"] == second["id"]
    with database.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM project_versions")
        assert cursor.fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# The plan gate (P-3)
# ---------------------------------------------------------------------------


def test_a_legacy_version_row_is_not_promoted(database):
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO project_versions (id, project_name, git_commit, created_at)"
            " VALUES ('legacy-1', 'demo', ?, 'legacy')",
            ("c" * 40,),
        )
    assert database.project_version_is_promoted("legacy-1") is False


def test_a_retired_version_is_no_longer_promoted(database, tmp_path):
    from app.approvals import approve

    approval_id = _request(database, tmp_path)
    version = asyncio.run(
        approve(database, approval_id, app_state=_State(tmp_path), audit_path="/dev/null")
    )["project_version"]
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE project_versions SET promotion_state = 'retired' WHERE id = ?",
            (version["id"],),
        )
    assert database.project_version_is_promoted(version["id"]) is False


def test_a_plan_cannot_bind_an_unpromoted_version(database):
    """The Phase 3 loop only closes if an unreviewed version cannot back a
    reproducible run."""
    from app.execution_plan import PlanInputs, derive_plan_draft

    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO project_versions (id, project_name, git_commit, created_at)"
            " VALUES ('legacy-2', 'demo', ?, 'legacy')",
            ("d" * 40,),
        )

    inputs = PlanInputs(
        project_name="demo",
        command="python train.py",
        project_version_id="legacy-2",
        run_profile_id="rp-1",
        dataset_none=True,
        server_config_revision_id="rev-1",
    )
    draft = derive_plan_draft(inputs, database.resolve_execution_plan_inputs(inputs))

    assert "project_version_missing" in draft.reason_codes
    assert draft.reproducible is False


def test_a_promoted_version_satisfies_the_plan_resolver(database, tmp_path):
    from app.approvals import approve
    from app.execution_plan import PlanInputs

    approval_id = _request(database, tmp_path)
    version = asyncio.run(
        approve(database, approval_id, app_state=_State(tmp_path), audit_path="/dev/null")
    )["project_version"]

    inputs = PlanInputs(
        project_name="demo",
        command="python train.py",
        project_version_id=version["id"],
        dataset_none=True,
    )
    assert database.resolve_execution_plan_inputs(inputs).project_version_exists is True


def test_migration_never_back_dates_a_legacy_version(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(
        """
        CREATE TABLE project_versions (
            id TEXT PRIMARY KEY, project_id TEXT, project_name TEXT NOT NULL,
            git_commit TEXT NOT NULL, git_ref TEXT, source_instance_id TEXT,
            created_at TEXT NOT NULL, metadata TEXT
        );
        INSERT INTO project_versions (id, project_name, git_commit, created_at)
        VALUES ('old', 'demo', 'deadbeef', 'legacy');
        """
    )
    raw.commit()
    raw.close()

    database = Database(str(path))

    assert database.project_version_is_promoted("old") is False
    conn = sqlite3.connect(str(path))
    assert conn.execute(
        "SELECT promotion_approval_id, promotion_state FROM project_versions"
    ).fetchone() == (None, None)
