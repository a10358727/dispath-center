"""WP-3A: immutable dataset snapshots (DG-DATASET-SNAPSHOT-v1).

Approved 2026-07-27. This module is the part of Phase 3 that makes a run
reproducible: it copies bytes into a content-addressed store so a completed run
can state exactly what data it consumed.

The claim the gate is built on, restated because everything here follows from
it: **a digest over a mutable directory is not a snapshot.** The existing
registry manifest (`app.datasets.build_manifest`) records only `{path, size}`,
so two different files of equal length are indistinguishable, and the source
directory can be edited in place afterwards. Neither can ever be `verified`.

Pure and local by construction:

- no network, no SSH, no worker. v1 reads only Server A local paths (gate D-3).
- the snapshot's identity is `manifest_digest` — the canonical digest of the
  per-file content hashes — not the tar bytes (gate D-4). That lets the shard
  policy or tar implementation change later without silently redefining every
  snapshot that already exists.
- unknown stays unknown. An unreadable file or a source that moves during the
  scan yields `verification_unknown`, never a fabricated hash.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.execution_contract import canonical_json, utf8_sha256

SNAPSHOT_CONTRACT_VERSION = "dataset-snapshot-build-v1"
STORE_REVISION = "local-artifact-store-v1"

#: Gate D-2: every byte is hashed, but a build above this ceiling is refused
#: rather than sampled. Sampling produces a digest that does not prove content,
#: which is the exact failure this gate exists to remove.
DEFAULT_MAX_BYTES = 200 * 1024**3

DEFAULT_SHARD_POLICY = {"max_shard_bytes": 1024**3, "max_shard_files": 4096}

_READ_CHUNK = 1024 * 1024


class SnapshotVerificationUnknown(Exception):
    """The source could not be read consistently. Never a hash, never a guess."""


class SnapshotRefused(Exception):
    """The build is refused outright (too large, or an unreproducible entry)."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    sha256: str
    is_symlink: bool = False
    link_target: Optional[str] = None


@dataclass(frozen=True)
class CandidateManifest:
    files: tuple[FileEntry, ...]
    total_bytes: int
    manifest_digest: str
    source_candidate_digest: str

    @property
    def file_count(self) -> int:
        return len(self.files)


@dataclass(frozen=True)
class ShardPlan:
    index: int
    entries: tuple[FileEntry, ...]
    total_bytes: int


@dataclass
class BuiltShard:
    index: int
    sha256: str
    size: int
    file_count: int
    staging_path: str


def _hash_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def build_candidate_manifest(
    source_path: str, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> CandidateManifest:
    """Hash every byte under `source_path` and detect drift while doing it.

    Drift detection is the reason this re-`stat`s each file *after* hashing:
    a file whose size or mtime changed while we were reading it means the
    source moved under us, and the digest we just computed describes bytes
    that no longer exist anywhere. That is `verification_unknown`, not a
    snapshot.
    """

    if not os.path.isdir(source_path):
        raise SnapshotVerificationUnknown(f"source is not a directory: {source_path}")

    entries: list[FileEntry] = []
    total = 0
    for root, dirs, filenames in os.walk(source_path):
        dirs.sort()
        for name in sorted(filenames):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, source_path).replace(os.sep, "/")
            try:
                before = os.lstat(full)
            except OSError as exc:
                raise SnapshotVerificationUnknown(f"cannot stat {rel}") from exc

            if stat.S_ISLNK(before.st_mode):
                # Symlinks are recorded as links, never followed: following one
                # would silently pull in bytes from outside the source tree.
                target = os.readlink(full)
                entries.append(
                    FileEntry(
                        path=rel,
                        size=0,
                        sha256=utf8_sha256(f"symlink:{target}"),
                        is_symlink=True,
                        link_target=target,
                    )
                )
                continue
            if not stat.S_ISREG(before.st_mode):
                # Device nodes, FIFOs and sockets are not data and cannot be
                # reproduced from a manifest.
                raise SnapshotRefused(f"unreproducible entry type: {rel}")

            total += before.st_size
            if total > max_bytes:
                raise SnapshotRefused(
                    f"source exceeds DATASET_SNAPSHOT_MAX_BYTES ({max_bytes})"
                )
            try:
                digest, size = _hash_file(full)
                after = os.lstat(full)
            except OSError as exc:
                raise SnapshotVerificationUnknown(f"cannot read {rel}") from exc

            if (
                after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or size != before.st_size
            ):
                raise SnapshotVerificationUnknown(f"source changed while reading {rel}")

            entries.append(FileEntry(path=rel, size=size, sha256=digest))

    entries.sort(key=lambda entry: entry.path)
    manifest_rows = [
        {
            "path": entry.path,
            "size": entry.size,
            "sha256": entry.sha256,
            **({"symlink": entry.link_target} if entry.is_symlink else {}),
        }
        for entry in entries
    ]
    manifest_digest = utf8_sha256(canonical_json(manifest_rows))
    source_candidate_digest = utf8_sha256(
        canonical_json(
            {
                "contract": SNAPSHOT_CONTRACT_VERSION,
                "manifest_digest": manifest_digest,
                "file_count": len(entries),
                "total_bytes": total,
            }
        )
    )
    return CandidateManifest(
        files=tuple(entries),
        total_bytes=total,
        manifest_digest=manifest_digest,
        source_candidate_digest=source_candidate_digest,
    )


def plan_shards(
    manifest: CandidateManifest, *, policy: Optional[dict[str, int]] = None
) -> tuple[ShardPlan, ...]:
    """Assign files to shards in sorted order.

    Shard boundaries are a pure function of the manifest and the policy, so two
    builds of the same source always produce the same shards. A file larger
    than `max_shard_bytes` becomes its own shard rather than being split.
    """
    policy = {**DEFAULT_SHARD_POLICY, **(policy or {})}
    max_bytes = int(policy["max_shard_bytes"])
    max_files = int(policy["max_shard_files"])

    shards: list[ShardPlan] = []
    current: list[FileEntry] = []
    current_bytes = 0
    for entry in manifest.files:
        would_exceed = current and (
            current_bytes + entry.size > max_bytes or len(current) >= max_files
        )
        if would_exceed:
            shards.append(
                ShardPlan(len(shards), tuple(current), current_bytes)
            )
            current, current_bytes = [], 0
        current.append(entry)
        current_bytes += entry.size
    if current:
        shards.append(ShardPlan(len(shards), tuple(current), current_bytes))
    return tuple(shards)


def _add_normalized(tar: tarfile.TarFile, entry: FileEntry, source_path: str) -> None:
    """Append one entry with every non-deterministic field normalized.

    Tar is notoriously non-reproducible: mtimes, uid/gid, uname/gname and mode
    bits all vary by machine and by run. The gate pins them (§5) so identical
    inputs always produce identical bytes.
    """
    full = os.path.join(source_path, entry.path)
    info = tarfile.TarInfo(name=entry.path)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    if entry.is_symlink:
        info.type = tarfile.SYMTYPE
        info.linkname = entry.link_target or ""
        info.mode = 0o777
        info.size = 0
        tar.addfile(info)
        return
    info.type = tarfile.REGTYPE
    info.mode = 0o644
    info.size = entry.size
    with open(full, "rb") as handle:
        tar.addfile(info, handle)


def build_shard_bytes(
    shard: ShardPlan, source_path: str
) -> tuple[bytes, str]:
    """Produce one deterministic tar shard and its digest."""
    buffer = io.BytesIO()
    # `format=USTAR_FORMAT` and no compression keep the bytes reproducible;
    # gzip would embed a timestamp.
    with tarfile.open(
        fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT
    ) as tar:
        for entry in shard.entries:
            _add_normalized(tar, entry, source_path)
    payload = buffer.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Local ArtifactStore
# ---------------------------------------------------------------------------


class LocalArtifactStore:
    """Server A local filesystem store (gate §4).

    Publication relies on atomic `rename(2)` within one filesystem, exactly as
    the launch claim does, so the root must be local. A non-local root fails
    preflight and disables publishing rather than silently losing atomicity.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.blobs = os.path.join(self.root, "blobs", "sha256")
        self.manifests = os.path.join(self.root, "manifests")
        self.snapshots = os.path.join(self.root, "snapshots")
        self.staging = os.path.join(self.root, "staging")
        for path in (self.blobs, self.manifests, self.snapshots, self.staging):
            os.makedirs(path, exist_ok=True)

    def blob_path(self, digest: str) -> str:
        return os.path.join(self.blobs, digest[:2], f"{digest}.tar")

    def staging_dir(self, build_id: str) -> str:
        path = os.path.join(self.staging, build_id)
        os.makedirs(path, exist_ok=True)
        return path

    def write_staging_shard(self, build_id: str, index: int, payload: bytes) -> str:
        path = os.path.join(self.staging_dir(build_id), f"shard-{index:05d}.tar")
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def verify_staged_shard(self, path: str, expected_digest: str, expected_size: int) -> bool:
        """Re-read from disk. Verifying the in-memory value would prove nothing
        about what actually landed."""
        try:
            digest, size = _hash_file(path)
        except OSError:
            return False
        return digest == expected_digest and size == expected_size

    def publish_blob(self, staging_path: str, digest: str) -> bool:
        """Move a verified shard into its content-addressed path.

        An existing blob with the same digest is dedup, not an error: identical
        bytes are identical bytes. Returns True when this call created it.
        """
        final = self.blob_path(digest)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        if os.path.exists(final):
            os.remove(staging_path)
            return False
        os.replace(staging_path, final)
        return True

    def publish_manifest(self, snapshot_id: str, rows: Iterable[dict[str, Any]]) -> str:
        final = os.path.join(self.manifests, f"{snapshot_id}.jsonl")
        tmp = f"{final}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(canonical_json(row) + "\n")
        os.replace(tmp, final)
        return final

    def publish_descriptor(self, snapshot_id: str, descriptor: dict[str, Any]) -> str:
        """Publish by temp file + atomic rename, so a half-written descriptor
        is never visible."""
        final = os.path.join(self.snapshots, f"{snapshot_id}.json")
        tmp = f"{final}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(descriptor))
        os.replace(tmp, final)
        return final

    def preflight(self) -> str:
        """`eligible` only when the root is on a local filesystem."""
        try:
            import subprocess

            result = subprocess.run(
                ["stat", "-f", "-c", "%T", self.root],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            return "unknown"
        if result.returncode != 0:
            return "unknown"
        fs_type = result.stdout.strip().lower()
        if fs_type in {"nfs", "smb2", "cifs", "fuseblk", "fuse"}:
            return "ineligible_non_local_fs"
        return "eligible"


@dataclass
class PublishResult:
    snapshot_id: str
    state: str  # 'published' | 'aborted' | 'verification_unknown'
    manifest_digest: Optional[str] = None
    shards: list[BuiltShard] = field(default_factory=list)
    reason: str = ""


def build_and_publish(
    store: LocalArtifactStore,
    *,
    snapshot_id: str,
    source_path: str,
    approved_candidate_digest: str,
    shard_policy: Optional[dict[str, int]] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> PublishResult:
    """Re-verify, build into staging, verify from disk, then publish.

    `approved_candidate_digest` is what a human approved. Recomputing it here
    is the step that stops a source edited between request and approval from
    being published as something nobody reviewed.
    """

    try:
        manifest = build_candidate_manifest(source_path, max_bytes=max_bytes)
    except SnapshotVerificationUnknown as exc:
        return PublishResult(snapshot_id, "verification_unknown", reason=str(exc))
    except SnapshotRefused as exc:
        return PublishResult(snapshot_id, "aborted", reason=str(exc))

    if manifest.source_candidate_digest != approved_candidate_digest:
        return PublishResult(
            snapshot_id, "aborted", reason="source_drifted_since_request"
        )

    shards = plan_shards(manifest, policy=shard_policy)
    built: list[BuiltShard] = []
    for shard in shards:
        payload, digest = build_shard_bytes(shard, source_path)
        staging_path = store.write_staging_shard(snapshot_id, shard.index, payload)
        # Verify from disk, not from the bytes still in memory.
        if not store.verify_staged_shard(staging_path, digest, len(payload)):
            return PublishResult(
                snapshot_id, "aborted", reason=f"shard_{shard.index}_verification_failed"
            )
        built.append(
            BuiltShard(
                index=shard.index,
                sha256=digest,
                size=len(payload),
                file_count=len(shard.entries),
                staging_path=staging_path,
            )
        )

    for shard in built:
        store.publish_blob(shard.staging_path, shard.sha256)

    store.publish_manifest(
        snapshot_id,
        (
            {"path": entry.path, "size": entry.size, "sha256": entry.sha256}
            for entry in manifest.files
        ),
    )
    store.publish_descriptor(
        snapshot_id,
        {
            "snapshot_id": snapshot_id,
            "contract": SNAPSHOT_CONTRACT_VERSION,
            "store_revision": STORE_REVISION,
            "manifest_digest": manifest.manifest_digest,
            "file_count": manifest.file_count,
            "total_bytes": manifest.total_bytes,
            "shards": [
                {"index": s.index, "sha256": s.sha256, "size": s.size}
                for s in built
            ],
        },
    )
    return PublishResult(
        snapshot_id,
        "published",
        manifest_digest=manifest.manifest_digest,
        shards=built,
        reason="published",
    )
