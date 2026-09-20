---
name: work-packet-delivery
description: Use for V0.1 work-packet Git branch, checkpoint commit, rebase, push, PR, merge sequencing, or starting the next WP. Not for feature implementation semantics or deployment.
---

# Work Packet Delivery

Owns delivery sequencing only. The relevant domain skill still owns **what** the
work packet changes.

## Invariants

- Default to one work packet per branch and PR unless the plan or user
  explicitly groups packets.
- Start a new packet from the latest `main` only after prerequisite packet PRs
  are merged.
- Do not stack a new packet on an unmerged sibling branch.
- Keep `main` as the reviewed/merged checkpoint, not an active work branch.
- Preserve uncommitted work before branch/rebase surgery.
- Git ancestry and Implementation Plan status must agree.
- Push, opening a PR, merging, and deployment are separate actions; do not infer
  permission for one from another.

## Standard flow

1. Inspect `git status`, current branch, and the active/READY packet.
2. Before a new packet:
   - ensure the prerequisite packet is merged;
   - `git switch main`;
   - `git pull --ff-only origin main`;
   - require a clean worktree;
   - create a packet branch, normally `feat/wp<N>-<slug>`.
3. Execute only that packet with the relevant domain/boundary skills.
4. Validate the packet, update its evidence/status, and promote only the next
   appropriate packet in the same change.
5. Create a local checkpoint commit after validation.
6. Before publishing, fetch current `main`. If the base moved, rebase onto the
   latest `main` and rerun affected validation.
7. Push/open a PR only with explicit user intent. Do not merge automatically.
8. After merge, sync `main` again before creating the next packet branch.

## Divergence recovery

If packet N+1 was created before packet N reached `main`:

1. checkpoint N+1 so no work is lost;
2. publish/merge packet N first;
3. update local `main`;
4. rebase N+1 onto that `main`;
5. resolve conflicts semantically:
   - preserve completed prerequisite behavior;
   - replay the current packet's intended behavior;
   - make Plan statuses match the resulting ancestry;
6. rerun validation after the rebase;
7. only then publish N+1.

Do not use `rebase --skip`, blindly choose `--ours`/`--theirs`, or
force-push merely to make the graph look clean.

## Validation before PR

At minimum verify:

- no unmerged paths;
- clean `git diff --check`;
- relevant packet tests/build/checks pass after the final rebase;
- the Implementation Plan reflects the actual completed/READY/BLOCKED state;
- the branch diff against `main` contains only the intended packet plus its
  required plan/evidence changes.

## Boundaries

- This skill does not grant permission to change product semantics.
- `release-gate` owns explicit exact-commit pre-deployment verification.
- `updating-pilot-site` owns explicit deployment/rollback/restart.
- Never touch production credentials, runtime data, or production services as
  part of branch/PR hygiene.
