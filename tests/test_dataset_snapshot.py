"""WP-3A: immutable dataset snapshots.

The property under test throughout is that a snapshot proves what bytes it
contains. Every case either demonstrates that, or demonstrates the system
refusing to claim it.
"""

from __future__ import annotations

import io
import os
import pathlib
import tarfile
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.approvals import (
    DatasetSnapshotDisabledError,
    approve,
    request_dataset_snapshot_build_approval,
    resume_dataset_snapshot_build,
)
from app.config import AppConfig
from app.dataset_snapshot import (
    DEFAULT_SHARD_POLICY,
    LocalArtifactStore,
    SnapshotRefused,
    SnapshotVerificationUnknown,
    build_and_publish,
    build_candidate_manifest,
    build_shard_bytes,
    plan_shards,
)


def _source(tmp_path, files=None):
    root = tmp_path / "src"
    root.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {"a.txt": "alpha", "b/c.txt": "charlie"}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return str(root)


def test_manifest_records_content_digests_not_just_sizes(tmp_path):
    """The defect this replaces: the legacy manifest cannot tell two equal-size
    files apart."""
    first = build_candidate_manifest(_source(tmp_path / "one", {"f": "AAAAA"}))
    second = build_candidate_manifest(_source(tmp_path / "two", {"f": "BBBBB"}))

    assert first.files[0].size == second.files[0].size
    assert first.files[0].sha256 != second.files[0].sha256
    assert first.manifest_digest != second.manifest_digest


def test_identical_sources_produce_identical_digests(tmp_path):
    files = {"a.txt": "alpha", "nested/b.txt": "bravo"}
    one = build_candidate_manifest(_source(tmp_path / "one", files))
    two = build_candidate_manifest(_source(tmp_path / "two", files))
    assert one.manifest_digest == two.manifest_digest
    assert one.source_candidate_digest == two.source_candidate_digest


def test_shards_are_byte_identical_across_builds(tmp_path):
    """Determinism is the whole basis of content addressing. Tar normally
    embeds mtime/uid/gid, which would break this."""
    files = {"a.txt": "alpha", "b.txt": "bravo"}
    src_one = _source(tmp_path / "one", files)
    src_two = _source(tmp_path / "two", files)
    # Make the two trees differ in every non-content way tar would record.
    os.utime(os.path.join(src_two, "a.txt"), (1, 1))

    manifest_one = build_candidate_manifest(src_one)
    manifest_two = build_candidate_manifest(src_two)
    _, digest_one = build_shard_bytes(plan_shards(manifest_one)[0], src_one)
    _, digest_two = build_shard_bytes(plan_shards(manifest_two)[0], src_two)
    assert digest_one == digest_two


def test_shard_tar_carries_no_machine_specific_metadata(tmp_path):
    source = _source(tmp_path)
    manifest = build_candidate_manifest(source)
    payload, _ = build_shard_bytes(plan_shards(manifest)[0], source)
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        for member in tar.getmembers():
            assert member.mtime == 0
            assert member.uid == 0 and member.gid == 0
            assert member.uname == "" and member.gname == ""


def test_unreadable_source_is_unknown_not_a_hash(tmp_path):
    with pytest.raises(SnapshotVerificationUnknown):
        build_candidate_manifest(str(tmp_path / "does-not-exist"))


def test_device_and_fifo_entries_refuse_the_build(tmp_path):
    source = _source(tmp_path)
    os.mkfifo(os.path.join(source, "pipe"))
    with pytest.raises(SnapshotRefused, match="unreproducible entry type"):
        build_candidate_manifest(source)


def test_symlinks_are_recorded_never_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret data outside the source tree")
    source = _source(tmp_path)
    os.symlink(str(outside), os.path.join(source, "link"))

    manifest = build_candidate_manifest(source)
    link = next(entry for entry in manifest.files if entry.path == "link")
    assert link.is_symlink is True
    assert link.size == 0
    assert "secret data" not in repr(manifest.files)


def test_size_ceiling_refuses_rather_than_sampling(tmp_path):
    source = _source(tmp_path, {"big": "x" * 4096})
    with pytest.raises(SnapshotRefused, match="MAX_BYTES"):
        build_candidate_manifest(source, max_bytes=1024)


def test_a_file_larger_than_the_shard_limit_becomes_its_own_shard(tmp_path):
    source = _source(tmp_path, {"a": "x" * 100, "big": "y" * 5000, "c": "z" * 100})
    manifest = build_candidate_manifest(source)
    shards = plan_shards(manifest, policy={"max_shard_bytes": 1000})
    big_shard = next(
        shard for shard in shards if any(e.path == "big" for e in shard.entries)
    )
    assert len(big_shard.entries) == 1


def test_publish_produces_a_readable_content_addressed_snapshot(tmp_path):
    source = _source(tmp_path)
    store = LocalArtifactStore(str(tmp_path / "store"))
    manifest = build_candidate_manifest(source)

    result = build_and_publish(
        store,
        snapshot_id="snap-1",
        source_path=source,
        approved_candidate_digest=manifest.source_candidate_digest,
    )

    assert result.state == "published"
    assert result.manifest_digest == manifest.manifest_digest
    for shard in result.shards:
        assert os.path.exists(store.blob_path(shard.sha256))
    assert os.path.exists(os.path.join(store.snapshots, "snap-1.json"))
    assert os.path.exists(os.path.join(store.manifests, "snap-1.jsonl"))


def test_source_drift_between_request_and_approval_aborts(tmp_path):
    source = _source(tmp_path)
    store = LocalArtifactStore(str(tmp_path / "store"))
    manifest = build_candidate_manifest(source)

    # Someone edits the source after the human reviewed the digest.
    (pathlib.Path(source) / "a.txt").write_text("tampered")

    result = build_and_publish(
        store,
        snapshot_id="snap-2",
        source_path=source,
        approved_candidate_digest=manifest.source_candidate_digest,
    )

    assert result.state == "aborted"
    assert result.reason == "source_drifted_since_request"
    assert not os.path.exists(os.path.join(store.snapshots, "snap-2.json"))


def test_publishing_the_same_source_twice_dedups_blobs(tmp_path):
    source = _source(tmp_path)
    store = LocalArtifactStore(str(tmp_path / "store"))
    digest = build_candidate_manifest(source).source_candidate_digest

    first = build_and_publish(
        store, snapshot_id="s1", source_path=source, approved_candidate_digest=digest
    )
    second = build_and_publish(
        store, snapshot_id="s2", source_path=source, approved_candidate_digest=digest
    )

    assert first.manifest_digest == second.manifest_digest
    assert [s.sha256 for s in first.shards] == [s.sha256 for s in second.shards]
    blob_count = sum(len(files) for _, _, files in os.walk(store.blobs))
    assert blob_count == len(first.shards)


def test_a_corrupted_staged_shard_blocks_publish(tmp_path, monkeypatch):
    source = _source(tmp_path)
    store = LocalArtifactStore(str(tmp_path / "store"))
    digest = build_candidate_manifest(source).source_candidate_digest

    original = store.write_staging_shard

    def _corrupt(build_id, index, payload):
        path = original(build_id, index, payload)
        with open(path, "ab") as handle:
            handle.write(b"corruption")
        return path

    monkeypatch.setattr(store, "write_staging_shard", _corrupt)
    result = build_and_publish(
        store, snapshot_id="s3", source_path=source, approved_candidate_digest=digest
    )

    assert result.state == "aborted"
    assert "verification_failed" in result.reason
    assert not os.path.exists(os.path.join(store.snapshots, "s3.json"))


def test_descriptor_is_never_visible_half_written(tmp_path):
    """Published by rename, so a reader sees the whole file or no file."""
    store = LocalArtifactStore(str(tmp_path / "store"))
    path = store.publish_descriptor("s4", {"snapshot_id": "s4"})
    assert os.path.exists(path)
    assert not os.path.exists(f"{path}.tmp")


def test_preflight_reports_local_filesystem(tmp_path):
    store = LocalArtifactStore(str(tmp_path / "store"))
    assert store.preflight() in {"eligible", "unknown"}


def test_manifest_digest_is_independent_of_shard_policy(tmp_path):
    """Gate D-4: identity is the per-file manifest, so the shard policy can
    change later without redefining existing snapshots."""
    source = _source(tmp_path, {f"f{i}": "x" * 100 for i in range(10)})
    manifest = build_candidate_manifest(source)
    small = plan_shards(manifest, policy={"max_shard_bytes": 150})
    large = plan_shards(manifest, policy=DEFAULT_SHARD_POLICY)
    assert len(small) != len(large)
    # The manifest digest belongs to the manifest, not to any shard layout.
    assert build_candidate_manifest(source).manifest_digest == manifest.manifest_digest


# ---------------------------------------------------------------------------
# Schema and the legacy registry boundary (gate D-1 / D-5)
# ---------------------------------------------------------------------------


def test_registry_datasets_are_never_reproducible(tmp_path):
    """Gate D-1: POST /datasets keeps working, but a registry row can never
    pin bytes, so it must never be labelled reproducible."""
    import sqlite3

    from app.db import Database

    path = tmp_path / "reg.db"
    database = Database(str(path))
    database.insert_dataset(
        name="legacy", version="v1", size_bytes=10, source_path="/srv/data",
        manifest={"file_count": 1, "total_size": 10, "files": []},
    )
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT reproducible FROM datasets").fetchall()
    assert rows and all(row[0] == 0 for row in rows)


def test_legacy_dataset_migration_never_invents_reproducible(tmp_path):
    import sqlite3

    from app.db import Database

    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(
        """
        CREATE TABLE datasets (
            name TEXT NOT NULL, version TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, source_path TEXT NOT NULL,
            manifest TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (name, version)
        );
        INSERT INTO datasets VALUES ('old','v1',5,'/srv','{}','legacy');
        """
    )
    raw.commit()
    raw.close()

    Database(str(path))
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT reproducible FROM datasets").fetchone()[0] == 0


def test_published_snapshot_row_is_immutable(tmp_path):
    import sqlite3

    from app.db import Database

    path = tmp_path / "snap.db"
    Database(str(path))
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO dataset_snapshots (id, dataset_name, state,"
        " source_candidate_digest, manifest_digest, manifest_path,"
        " descriptor_path, store_revision, shard_policy_json, file_count,"
        " total_bytes, build_approval_id, created_at)"
        " VALUES ('s1','d','published','cand','man','/m','/p','rev','{}',"
        " 10, 20, 1, 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE dataset_snapshots SET manifest_digest='x' WHERE id='s1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM dataset_snapshots WHERE id='s1'")


def test_a_snapshot_cannot_be_published_without_its_evidence(tmp_path):
    import sqlite3

    from app.db import Database

    path = tmp_path / "snap.db"
    Database(str(path))
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO dataset_snapshots (id, dataset_name, state,"
            " source_candidate_digest, store_revision, shard_policy_json,"
            " created_at) VALUES ('s2','d','published','cand','rev','{}','now')"
        )


def test_approval_builds_and_publishes_local_snapshot_atomically(tmp_path):
    from app.db import Database

    source = _source(tmp_path)
    db = Database(str(tmp_path / "workflow.db"))
    db.insert_dataset("demo", "v1", 12, source, {"file_count": 2})
    config = AppConfig(
        servers=[],
        dataset_snapshot_v1_enabled=True,
        dataset_snapshot_publish_enabled=True,
        dataset_snapshot_store_root=str(tmp_path / "store"),
        dataset_snapshot_max_bytes=1024 * 1024,
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    approval = request_dataset_snapshot_build_approval(
        db,
        dataset_name="demo",
        dataset_version="v1",
        config=config,
        audit_path=config.audit_path,
    )
    assert approval.status == "pending"

    decided = asyncio.run(
        approve(
            db,
            approval.id,
            app_state=SimpleNamespace(config=config),
            audit_path=config.audit_path,
        )
    )
    snapshot = decided["snapshot"]
    assert snapshot.state == "published"
    assert decided["approval"].status == "approved"
    assert len(db.get_dataset_snapshot_shards(snapshot.id)) == 1
    assert os.path.exists(snapshot.manifest_path)
    assert os.path.exists(snapshot.descriptor_path)
    events = db.list_durable_audit_events(limit=100)
    assert any(event["action"] == "approval_decided" for event in events)
    published = next(
        event
        for event in events
        if event["action"] == "project_snapshot_published"
    )
    assert published["result"] == "published"
    assert published["params"]["manifest_digest"] == snapshot.manifest_digest


def test_approval_records_source_drift_as_aborted_snapshot(tmp_path):
    from app.db import Database

    source = _source(tmp_path)
    db = Database(str(tmp_path / "workflow.db"))
    db.insert_dataset("demo", "v1", 12, source, {"file_count": 2})
    config = AppConfig(
        servers=[],
        dataset_snapshot_v1_enabled=True,
        dataset_snapshot_publish_enabled=True,
        dataset_snapshot_store_root=str(tmp_path / "store"),
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    approval = request_dataset_snapshot_build_approval(
        db,
        dataset_name="demo",
        dataset_version="v1",
        config=config,
        audit_path=config.audit_path,
    )
    pathlib.Path(source, "a.txt").write_text("drifted")
    decided = asyncio.run(
        approve(
            db,
            approval.id,
            app_state=SimpleNamespace(config=config),
            audit_path=config.audit_path,
        )
    )
    assert decided["snapshot"].state == "aborted"
    assert decided["snapshot"].last_error_category == "snapshot_build_refused"
    assert decided["approval"].status == "approved"
    failed = next(
        event
        for event in db.list_durable_audit_events(limit=100)
        if event["action"] == "project_snapshot_published"
    )
    assert failed["result"] == "aborted"


def test_snapshot_request_is_fail_closed_when_flag_is_off(tmp_path):
    from app.db import Database

    source = _source(tmp_path)
    db = Database(str(tmp_path / "workflow.db"))
    db.insert_dataset("demo", "v1", 12, source, {"file_count": 2})
    with pytest.raises(DatasetSnapshotDisabledError):
        request_dataset_snapshot_build_approval(
            db,
            dataset_name="demo",
            dataset_version="v1",
            config=AppConfig(servers=[]),
            audit_path="/dev/null",
        )


def test_concurrent_approvals_have_one_snapshot_winner(tmp_path):
    from app.db import Database

    source = _source(tmp_path)
    db = Database(str(tmp_path / "workflow.db"))
    db.insert_dataset("demo", "v1", 12, source, {"file_count": 2})
    config = AppConfig(
        servers=[],
        dataset_snapshot_v1_enabled=True,
        dataset_snapshot_publish_enabled=True,
        dataset_snapshot_store_root=str(tmp_path / "store"),
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    approvals = [
        request_dataset_snapshot_build_approval(
            db,
            dataset_name="demo",
            dataset_version="v1",
            config=config,
            audit_path=config.audit_path,
        )
        for _ in range(2)
    ]

    def decide(item):
        return asyncio.run(
            approve(
                db,
                item.id,
                app_state=SimpleNamespace(config=config),
                audit_path=config.audit_path,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, approvals))

    snapshots = db.list_dataset_snapshots(dataset_name="demo", dataset_version="v1")
    assert [snapshot.state for snapshot in snapshots] == ["published"]
    assert sum(result["approval"].status == "approved" for result in results) == 1
    assert sum(result["approval"].status == "rejected" for result in results) == 1


def test_interrupted_build_can_resume_from_approved_evidence(tmp_path, monkeypatch):
    from app import approvals as approvals_module
    from app.db import Database

    source = _source(tmp_path)
    db = Database(str(tmp_path / "workflow.db"))
    db.insert_dataset("demo", "v1", 12, source, {"file_count": 2})
    config = AppConfig(
        servers=[],
        dataset_snapshot_v1_enabled=True,
        dataset_snapshot_publish_enabled=True,
        dataset_snapshot_store_root=str(tmp_path / "store"),
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    approval = request_dataset_snapshot_build_approval(
        db,
        dataset_name="demo",
        dataset_version="v1",
        config=config,
        audit_path=config.audit_path,
    )

    def crash(*args, **kwargs):
        raise SystemExit("simulated control-plane crash")

    monkeypatch.setattr(approvals_module, "build_and_publish", crash)
    with pytest.raises(SystemExit):
        asyncio.run(
            approve(
                db,
                approval.id,
                app_state=SimpleNamespace(config=config),
                audit_path=config.audit_path,
            )
        )

    building = db.list_dataset_snapshots(state="building")
    assert len(building) == 1
    monkeypatch.setattr(approvals_module, "build_and_publish", build_and_publish)
    resumed = resume_dataset_snapshot_build(
        db,
        building[0].id,
        config=config,
        audit_path=config.audit_path,
    )
    assert resumed["snapshot"].state == "published"
    assert db.list_dataset_snapshots(state="building") == []
