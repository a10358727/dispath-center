# DG-DATASET-SNAPSHOT — Immutable dataset snapshots and the legacy registry boundary

> Date: 2026-07-27
>
> Contract revision: `DG-DATASET-SNAPSHOT-v1`
>
> Status: **draft for review — not approved. No implementation, migration,
> flag change or API change is authorized by this file.**
>
> Authoritative decision record after ruling: `docs/DECISIONS.md`
>
> Reviewed-draft SHA-256: `<computed at ruling time>`
>
> Required approval phrase:
> `DG-DATASET-SNAPSHOT v1：核准本文件的 recommended contract`

This is the gate required before WP-3A in `docs/NEXT_IMPLEMENTATION_PLAN.md`
§8.3, and it resolves release blocker `RB-DATASET-001`. Phase 3 — the core
product milestone — cannot start without it: until a dataset can be pinned
immutably, a reproducible run can only use `dataset=none`.

---

## 1. Recommended decision

Approve a default-off, content-addressed snapshot pipeline with these fixed
properties:

1. **A digest over a mutable directory is not a snapshot.** Publishing copies
   bytes into a content-addressed store. A snapshot that still points at a
   source path someone can edit in place is not immutable and must never be
   marked `verified`.
2. **Content digests, not size and count.** The existing path+size manifest
   (`app/datasets.py:build_manifest`) can never earn `verified`; two different
   files of equal size are indistinguishable to it.
3. **The legacy registry stays, explicitly labelled.** `POST /datasets` keeps
   working as a *non-reproducible* registry input and is marked
   `reproducible=false` everywhere it surfaces. It is not converted into a
   publishing path, and it is not silently upgraded.
4. **Approve-time re-verification.** The candidate digest computed at request
   time is recomputed at approval. Source drift in between aborts; it never
   publishes what was not reviewed.
5. **Unknown stays unknown.** An unreadable source, a scan that observes
   concurrent modification, or an interrupted build produces
   `verification_unknown`, never a fabricated hash and never a partial
   `published` snapshot.
6. **Atomic publish.** Blobs land content-addressed, then the snapshot
   descriptor is published by atomic rename, then the DB moves
   `building → published` by CAS. After `published`, descriptor, manifest and
   shard mapping are immutable.
7. **One local ArtifactStore.** Server A local filesystem only, with a pinned
   store revision. The layout keeps a future S3-compatible adapter possible;
   this gate does not authorize one.

---

## 2. Existing behavior and the exact defect

### 2.1 Evidence

`app/main.py:7447` (`create_dataset`) scans a path and calls
`app_state.db.insert_dataset(...)` directly. No approval is created, and no
`VALID_APPROVAL_KINDS` entry authorizes it — `tests/test_api.py::
test_create_and_list_dataset` proves the row appears with zero approvals. That
is `RB-DATASET-001`.

`app/datasets.py:build_manifest` records `{path, size}` per file plus
`file_count` and `total_size`. There is no content digest anywhere in it.
`verify_manifest_match()` therefore compares counts and sizes, which cannot
distinguish a corrupted or substituted file of the same length.

Consequence: nothing in the system today can state what bytes a completed run
actually consumed.

### 2.2 Behavior that must not change

- `jobs.status`, `INV-STATE-*`, `INV-SSH-*`, `INV-APPROVAL-*` and
  `INV-NODE-*` remain canonical.
- `maybe_auto_approve()` stays exactly `enqueue|stop`. A snapshot build is a
  material mutation and is never auto-approved.
- Existing dataset rows, the existing sync path and existing jobs referencing
  them keep working unchanged.
- LLM/MCP may only create a pending request.
- No production object store, no external network egress.

---

## 3. The legacy registry boundary (`RB-DATASET-001`)

This is the decision with the widest blast radius, so it is stated first.

**Recommendation: keep `POST /datasets` as an approval-free *registry* entry,
and make its non-reproducibility explicit and machine-checkable.**

| Concept | `dataset` (legacy registry) | `dataset_snapshot` (new) |
|---|---|---|
| What it records | a path someone declared | bytes in a content-addressed store |
| Manifest | path + size | path + size + SHA-256 per file |
| Mutable after creation | yes, source can change | no |
| Approval | none | `dataset_snapshot_build`, human-approved |
| Usable by a reproducible run | **no** | yes |
| `reproducible` field | always `false` | `true` when `published` |

Rationale for not converting `POST /datasets` into an approval flow now:

- It is a *declaration*, not a material remote mutation: it writes one local
  row and touches no worker. Its danger is that its output looks
  authoritative, not that it acts.
- Converting it would break existing callers and the existing sync path for no
  reproducibility gain — a registry row still cannot pin bytes.
- The honest fix is labelling, plus a separate path that actually pins bytes.

What this gate **does** require:

1. `datasets` gains `reproducible INTEGER NOT NULL DEFAULT 0` (always 0 for
   registry rows). No migration invents a `1`.
2. Every API and UI surface exposing a dataset reports `reproducible` and, for
   registry rows, `verification: "legacy_unverified"`.
3. A run requesting reproducible acceptance with a registry dataset is
   **rejected at request time** with reason `dataset_not_reproducible`, not
   silently downgraded.
4. `RB-DATASET-001` moves from "unapproved material mutation" to "explicitly
   bounded non-reproducible input". It closes only when both the label and the
   rejection are enforced by tests.

If you would rather convert `POST /datasets` into a request/approve flow, that
is sub-decision **D-1** below; it is a larger, breaking change.

---

## 4. ArtifactStore

Server A local filesystem, one pinned revision:

```text
dataset_store/blobs/sha256/<aa>/<shard-digest>.tar
dataset_store/manifests/<snapshot-id>.jsonl
dataset_store/snapshots/<snapshot-id>.json
dataset_store/staging/<build-id>/...
```

- `store_revision` is persisted on every snapshot. A snapshot published under
  one store revision is never silently reinterpreted under another.
- Blobs are sharded by the first two hex characters to keep directory sizes
  workable.
- The store root must be on a **local filesystem**: publication relies on
  atomic `rename(2)` within one filesystem, exactly as the launch claim does.
  A non-local root fails the preflight and disables snapshot publishing rather
  than silently losing atomicity.
- No garbage collection in v1. Unreferenced blobs accumulate and are removed
  only by a separately approved retention operation, because deleting bytes a
  published snapshot might reference is not a cleanup, it is data loss.

---

## 5. Determinism contract

Content addressing is only meaningful if the same input bytes always produce
the same digest. Tar is notoriously non-deterministic, so the normalization is
pinned here rather than left to an implementation detail:

- entries sorted by relative path, byte-wise, `LC_ALL=C` ordering;
- `mtime = 0`, `uid = gid = 0`, `uname = gname = ""`;
- mode normalized to `0644` for files and `0755` for directories;
- POSIX ustar format, no extended attributes, no ACLs;
- symlinks recorded as symlinks, never followed;
- hardlinks stored as regular files (dedup happens at the blob layer);
- no sparse-file encoding;
- device/FIFO/socket entries **reject the build** — they are not data and
  cannot be reproduced.

`shard_policy` is pinned per snapshot: `{"max_shard_bytes": N,
"max_shard_files": M}`. Files are assigned to shards in sorted order, so shard
boundaries are a pure function of the manifest. A file larger than
`max_shard_bytes` becomes its own shard.

**The file manifest, not the tar bytes, is the identity.**
`manifest_digest` is the canonical-JSON SHA-256 of the sorted per-file
`{path, size, sha256}` list. Shard digests are recorded as storage evidence.
This matters because it lets the shard policy or tar implementation change in
future without changing what the snapshot *is*.

---

## 6. Publish protocol

```text
candidate  → (approval)  → building → published
                              ↓
                    aborted | verification_unknown
```

1. **Candidate inventory** (read-only). Walk the source, recording relative
   path, size and SHA-256 per file. Compute `source_candidate_digest`.
2. **Drift detection.** Re-`stat` every file after hashing. Any size or
   `mtime_ns` change means the source moved under us →
   `verification_unknown`, no build. Unreadable file → same. A fabricated
   hash is never acceptable.
3. **Request.** Create a `dataset_snapshot_build` pending approval pinning
   dataset, version, source identity, `source_candidate_digest`,
   `store_revision`, `shard_policy` and publish target. LLM/MCP may create
   this request and nothing else.
4. **Approve-time re-verification.** Recompute the candidate digest. Mismatch
   → reject with `source_drifted_since_request`; never publish what was not
   reviewed. Only on match is a `building` row created.
5. **Build into staging.** Stream deterministic shards into
   `staging/<build-id>/`, computing each shard's SHA-256. Any read, hash or
   drift failure aborts. Nothing outside staging is touched.
6. **Verify from staging.** Re-read shards from disk and re-verify digests and
   sizes against the manifest. Verifying only the in-memory values would prove
   nothing about what actually landed.
7. **Publish.** Link/rename each blob into its content-addressed path
   (existing identical blob = dedup, not an error), write the manifest, then
   publish the snapshot descriptor by temp-file + atomic rename. Finally CAS
   the DB `building → published`.
8. **Immutable after publish.** Descriptor, manifest and shard mapping are
   append-only; triggers enforce it as with attempts.

Crash at any step converges to: still `building` (staging retained, resumable),
`verification_unknown`, or `published`. A half-written descriptor is never
visible because it is renamed into place, never written in place.

---

## 7. Additive schema

```sql
CREATE TABLE dataset_snapshots (
    id TEXT PRIMARY KEY,
    dataset_name TEXT NOT NULL,
    dataset_version TEXT,
    state TEXT NOT NULL
        CHECK (state IN ('candidate','building','published',
                         'aborted','verification_unknown')),
    source_candidate_digest TEXT NOT NULL,
    manifest_digest TEXT,
    manifest_path TEXT,
    descriptor_path TEXT,
    store_revision TEXT NOT NULL,
    shard_policy_json TEXT NOT NULL,
    file_count INTEGER,
    total_bytes INTEGER,
    build_approval_id INTEGER REFERENCES approvals(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    last_error_category TEXT,
    sanitized_error_detail TEXT
);

CREATE TABLE dataset_snapshot_shards (
    snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(id) ON DELETE RESTRICT,
    shard_index INTEGER NOT NULL,
    shard_sha256 TEXT NOT NULL,
    shard_bytes INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, shard_index)
);

-- additive column
ALTER TABLE datasets ADD COLUMN reproducible INTEGER NOT NULL DEFAULT 0;
```

New approval kind: `dataset_snapshot_build`, contract
`dataset-snapshot-build-v1`, pinned payload. It is **not** added to the
auto-approve allowlist.

---

## 8. Target staging and run binding

- A Job/ExecutionPlan binds `snapshot_id` + `manifest_digest` +
  `store_revision`. Never a mutable path, never "latest".
- Staging to a worker transfers only missing shards, unpacks outside the final
  path, verifies digests there, then publishes the replica by marker + atomic
  rename.
- Approve-time re-verifies the exact binding on the target. A mismatch is
  rejected **before any workload side effect**.
- A replica that fails verification is quarantined, not deleted: the bytes are
  evidence of whatever went wrong.

---

## 9. Feature flags and rollout

```text
DATASET_SNAPSHOT_V1_ENABLED=false
DATASET_SNAPSHOT_PUBLISH_ENABLED=false
```

- Schema, request and preview may be exercised with only the first flag.
- Publishing requires both, plus a store-root preflight confirming a local
  filesystem.
- Rollback disables publishing; it never deletes published snapshots, blobs or
  manifests.

---

## 10. Invariant compatibility

No canonical invariant text changes under this gate.

- `INV-APPROVAL-1/2`: a new pinned kind with an exact contract; dangerous-input
  validation at request and re-validation at approve.
- `INV-APPROVAL-4`: auto-approve remains `enqueue|stop`.
- `INV-STATE-1/3`: SQLite stays the truth; schema is additive and
  migration-tested.
- `INV-LLM-*`: request creation only.
- `INV-TEST-*`: all tests use temporary store roots and fixture data; no
  production dataset path is read.

Deferred and **not** unlocked: S3/object-store adapter, garbage collection or
retention deletion, dataset ACLs, cross-site replication, and any conversion of
the legacy registry beyond labelling (unless D-1 is chosen).

---

## 11. Sub-decisions requiring an explicit ruling

| # | Question | Recommended | Consequence if rejected |
|---|---|---|---|
| D-1 | Convert `POST /datasets` into a request/approve flow now? | **No — label it `reproducible=false` and add the reproducible path alongside.** It writes one local row and touches no worker; its danger is that it looks authoritative, and labelling fixes that. | Converting breaks existing callers and the sync path for no reproducibility gain, since a registry row still cannot pin bytes. |
| D-2 | Hash every byte at request time, for datasets that may be very large? | **Yes, with an explicit size ceiling** (`DATASET_SNAPSHOT_MAX_BYTES`, default 200 GiB) above which the build is refused rather than sampled. | Sampling or trusting mtime produces a digest that does not prove content, which is the exact failure mode this gate exists to remove. |
| D-3 | Where do bytes get copied from? | **Server A local path only in v1.** A dataset that lives only on a worker cannot be snapshotted yet. | Pulling from a worker adds an SSH/rsync trust and failure surface to a publishing path that has no attempt/outbox protection yet. |
| D-4 | Manifest identity vs tar identity | **`manifest_digest` (per-file content digests) is the snapshot identity**; shard digests are storage evidence. | Making tar bytes the identity freezes the shard policy and tar implementation forever — any change would silently redefine every snapshot. |
| D-5 | What happens to an existing legacy `dataset` used by a run requesting reproducibility? | **Reject at request time** with `dataset_not_reproducible`. | Silently downgrading produces runs that claim reproducibility they do not have, which is worse than refusing. |

---

## 12. WP-3A acceptance after approval

- Deterministic build: the same source produces byte-identical shards and an
  identical `manifest_digest` across two runs, two processes and two store
  roots.
- A file modified during the candidate scan yields `verification_unknown` and
  publishes nothing.
- A file modified between request and approval is rejected with
  `source_drifted_since_request`.
- A shard corrupted in staging fails verification and blocks publish.
- Crash injected before/after blob linking, before/after descriptor rename and
  before/after the DB CAS each converge to `building` or `published`, never to
  a visible partial snapshot.
- Publishing the same source twice dedups blobs and produces the same
  `manifest_digest`.
- A `published` snapshot's descriptor, manifest and shard rows reject UPDATE
  and DELETE.
- Two concurrent builds of the same dataset produce one winner, no interleaved
  staging.
- A registry dataset reports `reproducible=false` at every surface, and a run
  requesting reproducibility with it is rejected.
- Symlink, hardlink, and device-node inputs behave exactly as §5 states.
- A non-local store root fails preflight and disables publishing.
- Fresh and representative legacy migrations pass; no existing dataset row
  gains `reproducible=1`.
- Full suite, static gate and migration suite green; no production dataset
  path, worker or network is touched.

---

## 13. Decision

- [ ] **Approve recommended contract** — adopt §3 (legacy boundary), §4
  (ArtifactStore), §5 (determinism), §6 (publish protocol), §7 (schema), §8
  (run binding) and the §11 recommendations (D-1…D-5), and authorize WP-3A
  implementation with both flags default-off.
- [ ] Approve with changes: ______________________________________________
      (name the sub-decisions to overturn)
- [ ] Reject — Phase 3 then proceeds with `dataset=none` only, and
  `RB-DATASET-001` stays open as an unapproved material mutation.

Approving unlocks implementation only. It does not authorize enabling either
flag, publishing a real dataset, an object-store adapter, or any retention
deletion.
