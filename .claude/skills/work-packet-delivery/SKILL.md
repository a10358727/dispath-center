---
name: work-packet-delivery
description: Use for V0.1 work-packet Git sequencing: starting the next READY WP, branch/checkpoint/rebase flow, publishing a packet PR, merge handoff, or recovering when a packet was based on stale main. Not for feature semantics or deployment.
---

# Work Packet Delivery

Owns delivery sequencing only. The relevant domain skill owns **what** the work
packet changes.

## Invariants

- Default to one work packet per branch and PR unless the plan or user explicitly groups packets.
- Start a new packet from the latest merged `main` after its prerequisites are present there.
- Do not stack a new packet on an unmerged sibling branch.
- Keep `main` as the reviewed/merged checkpoint, not an active feature branch.
- Require a clean worktree before branch/rebase surgery; preserve unrelated user changes.
- Git ancestry and `docs/product/V0_1_IMPLEMENTATION_PLAN.md` status must agree.
- Commit, push, open PR, merge, and deploy are separate actions. Never infer authority for one from another.
- Deployment is never part of this skill.

## Start a packet

1. Inspect `git status`, current branch, latest `origin/main`, and the Program Board.
2. Confirm the target packet is legitimately `READY` and prerequisites are merged.
3. `git switch main`.
4. `git pull --ff-only origin main`.
5. Require a clean worktree.
6. Create a packet branch, normally `feat/wp<N>-<slug>` (or another scoped prefix such as `test/` or `docs/` when that better matches the packet).

Use only the minimum relevant domain/boundary skills for implementation.

## Finish a packet

1. Run the packet's targeted validation while iterating.
2. Before the packet checkpoint commit, run the repository-required full gate from
   `CLAUDE.md` and any packet-specific checks.
3. Update implementation-plan evidence/status in the same change.
4. Mark the packet `DONE` only when every acceptance criterion passes.
5. Promote exactly one next safe packet to `READY` when appropriate; preserve
   named decision gates and existing `BLOCKED` packets.
6. Create one coherent local checkpoint commit.

## Rebase before publication

Before publishing:

1. fetch current `origin/main`;
2. if `main` moved, rebase the packet branch onto latest `main`;
3. resolve conflicts semantically:
   - preserve merged prerequisite behavior;
   - replay the current packet's intended behavior;
   - make plan status match resulting ancestry;
4. rerun affected validation after the rebase;
5. require `git diff --check` and no unmerged paths.

Never use `rebase --skip`, blindly choose `--ours`/`--theirs`, or force-push
merely to make history look clean.

## Publication authority

A completed packet may be pushed, opened as a PR, or merged only when the user
has explicitly authorized that action in the current task.

If merge is authorized, require:

- packet acceptance and validation pass;
- branch is based on current `main`;
- GitHub checks/review requirements are satisfied;
- no decision gate is being bypassed;
- the PR contains only the intended packet plus required plan/evidence changes.

After merge, sync local `main` again before starting another packet.

## Divergence recovery

If packet N+1 was started before packet N reached `main`:

1. checkpoint N+1 so no work is lost;
2. merge packet N first;
3. update local `main`;
4. rebase N+1 onto that `main`;
5. resolve conflicts semantically;
6. rerun post-rebase validation;
7. publish N+1 only after the graph and plan agree.

## Boundaries

- This skill grants no product, approval, SSH, lifecycle, or authorization semantics.
- `release-gate` owns exact-commit pre-deployment verification.
- `updating-pilot-site` owns explicit deployment/rollback/restart.
- Never touch production credentials, runtime data, workers, or services as part
  of branch/PR hygiene.
