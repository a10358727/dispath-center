# DG-CODE-PROMOTE — Promoting Codex output into an immutable ProjectVersion

> Date: 2026-07-28
>
> Contract revision: `DG-CODE-PROMOTE-v1`
>
> Status: **approved on 2026-07-28 for WP-3C implementation. Promotion is not
> enabled in any running deployment, no GitHub adapter is authorized, and no
> worktree deletion is permitted.**
>
> Authoritative decision record: `docs/DECISIONS.md`
>
> Approved reviewed-draft SHA-256:
> `510d4075d6011dc84b3fc4c913665dc8a3853bfc72f721033a6c65d14fb25baf`
> (verifiable in commit `4abd84e`; this status block was appended afterwards)
>
> Required approval phrase:
> `DG-CODE-PROMOTE v1：核准本文件的 recommended contract`

Required before WP-3C in `docs/archive/NEXT_IMPLEMENTATION_PLAN.md` §8.5. It is the
last gate on the Phase 3 loop: WP-3A pins data, WP-3B pins the plan, and this
pins **code** — without it, a run can only reference a ProjectVersion that
arrived some other way, and "Codex changed the code and we ran it" has no
auditable link between the two halves.

---

## 1. Recommended decision

Approve a promotion path with these fixed properties:

1. **Promotion is a distinct, human-approved act.** Producing a bundle is not
   promoting it. Codex may generate a diff and a commit all day; nothing
   becomes runnable until a person approves that specific artifact.
2. **Codex can never approve its own output.** This is `INV-LLM-*` restated at
   the one place it would be most tempting to bend: the agent that wrote the
   code cannot be the actor that promotes it, even via an auto-approve rule.
3. **The bundle's digest is the identity.** A promotion approval pins the exact
   `git bundle` SHA-256 and the resulting commit. Re-verified at approve time,
   because a bundle regenerated between request and approval is a different
   artifact regardless of whether the diff "looks the same".
4. **Hub write is atomic and reversible.** The bundle is verified into a
   staging location, the ProjectVersion row is created, and only then is the
   hub reference published. A failure at any point leaves no half-imported
   version.
5. **A ProjectVersion is immutable once created.** It records the commit, the
   bundle digest and the promoting approval. Re-promoting the same commit is a
   no-op, not a second version.
6. **Only promoted versions are runnable.** A plan may bind a ProjectVersion
   only if it carries a promotion approval; a version that arrived by any other
   route is `legacy_observed` and cannot back a reproducible run.

---

## 2. Existing behavior and the gap

`app/engineering_tasks.py` already builds bundles
(`build_engineering_bundle_create_command`, `_verify`, `_push`) and
`app/hub.py` has `build_deploy_push_command`. What does not exist is any
approval kind that turns a bundle into a ProjectVersion — so today the loop
stops at "here is a bundle" and a human moves it by hand, with nothing
recording which bundle became which version.

`project_versions` currently has no promotion provenance: `id`, `project_name`,
`git_commit`, `git_ref`, `source_instance_id`, `created_at`, `metadata`. A row
can be inserted by anything.

Behavior that must not change:

- `maybe_auto_approve()` stays exactly `enqueue|stop`.
- LLM/MCP may create a pending request and nothing else.
- Existing bundle creation/verification commands keep their exact strings.
- No GitHub adapter is activated; `DG-GITHUB-PUBLISH` remains separate.

---

## 3. The promotion contract

New approval kind `engineering_task_promote`, contract
`code-promotion-v1`, pinned payload:

```json
{
  "engineering_task_id": "...",
  "project_name": "...",
  "base_project_version_id": "...",
  "git_commit": "<40 hex>",
  "bundle_sha256": "<64 hex>"
}
```

Exactly these keys. The payload carries identifiers and digests only — never a
command, a path, or a branch name that could be interpreted at approve time.

Approve-time re-verification:

1. The engineering task still exists and is in a terminal, non-discarded state.
2. The bundle file's digest still equals `bundle_sha256`. A regenerated bundle
   is a different artifact even if its diff is identical, because
   reproducibility claims are about bytes.
3. `git bundle verify` passes against the recorded commit.
4. The base ProjectVersion still exists.
5. No ProjectVersion already exists for `(project_name, git_commit)`.

Any failure rejects the approval with a reason code and writes an audit
record. Nothing is imported.

---

## 4. Publish protocol

```text
approved -> verify bundle in staging -> create ProjectVersion -> publish hub ref
```

- The bundle is verified in a staging path first. Importing into the hub and
  then discovering the bundle is corrupt would leave a repository someone has
  to clean up by hand.
- The ProjectVersion row is created before the hub reference is published, so a
  crash leaves an unreferenced version (harmless, visible) rather than a hub
  reference to a version that does not exist (a dangling pointer).
- Publishing the hub reference uses the existing local-run rsync/git path. No
  new remote command family.
- Rollback retires the ProjectVersion; it never deletes the commit or the
  bundle, because those are the evidence of what happened.

Additive schema on `project_versions`:

```sql
promotion_approval_id INTEGER REFERENCES approvals(id) ON DELETE RESTRICT
bundle_sha256 TEXT
promoted_at TEXT
promotion_state TEXT
  CHECK (promotion_state IS NULL
         OR promotion_state IN ('promoted', 'retired'))
```

Existing rows keep `NULL` — they are honestly `legacy_observed`, not
back-dated into promoted versions.

---

## 5. Sub-decisions requiring an explicit ruling

| # | Question | Recommended | Consequence if rejected |
|---|---|---|---|
| P-1 | May a promotion approval be auto-approved under any policy? | **No, never.** Not in `maybe_auto_approve()`, not through the `INV-APPROVAL-4b` policy mechanism. | Any automatic path lets the system run code no human ever looked at, which is the single failure this whole gate exists to prevent. |
| P-2 | Can the same commit be promoted twice? | **No — it is a no-op returning the existing version.** | Two versions for one commit makes "which version did this run use" unanswerable. |
| P-3 | Can a legacy `project_versions` row back a reproducible run? | **No.** Rows without a promotion approval are `legacy_observed`. | Back-dating them would claim provenance that was never reviewed. |
| P-4 | Does promotion push to GitHub? | **No.** Hub write only; `DG-GITHUB-PUBLISH` stays a separate, unapproved gate. | Bundling the two would activate an external egress path under a gate that never reviewed it. |
| P-5 | What happens to the worktree after promotion? | **Left in place.** Cleanup is a separately approved retention operation. | Deleting the source of an imported commit removes the evidence for it. |

---

## 6. WP-3C acceptance after approval

- A promotion request pins the bundle digest; a bundle regenerated afterwards
  is rejected at approve time.
- A tampered bundle fails `git bundle verify` and imports nothing.
- Crash injected before/after the ProjectVersion insert and before/after the
  hub publish converges to: not promoted, or promoted-and-referenced. Never a
  hub reference to a missing version.
- Promoting the same commit twice returns the first version and creates no
  second row.
- A plan binding a `legacy_observed` version is rejected with
  `project_version_not_promoted`.
- Codex/LLM/MCP cannot create an approved promotion by any route; only a
  pending request.
- `maybe_auto_approve()` is unchanged and a promotion never auto-approves.
- Existing bundle command strings are byte-identical.

---

## 7. Decision

- [x] **Approve recommended contract** — adopt §3 (promotion contract), §4
  (publish protocol and schema) and §5's recommendations (P-1…P-5), and
  authorize WP-3C implementation with promotion disabled by default.
- [ ] Approve with changes: ______________________________________________
- [ ] Reject — Codex output stays at "bundle produced", promotion stays manual
  and unrecorded, and the Phase 3 loop cannot close.

Approving unlocks implementation only. It does not authorize enabling
promotion in a running deployment, any GitHub adapter, or worktree deletion.
