# Project onboarding contract

Canonical semantics remain in `docs/PLATFORM_CHARTER.md` (§6 invariants; §4／§8 plane model and capability verification) and named decisions.

## Discovery

- Discovery is read-only, uses a closed command set, and filters secret/forbidden paths before data can reach DB/API/audit/LLM.
- Never accept arbitrary command execution or silently broaden scanned paths.
- Unreachable instances are unknown, not missing/clean.

## Registration and mutation

- Import/link/ignore/git-init/deploy/instance-update follow reviewed request → approval → apply paths.
- Revalidate paths and candidate/instance state at approval time.
- Never mutate/reset/stash/merge/overwrite a checkout just to make onboarding succeed.
- Persistent onboarding state follows reviewed SQLite/migration semantics.

## Project/version model

- Server folders are project instances, not version truth.
- Onboarding outcomes remain provider-neutral; readiness must not be named after Codex/Claude or grant provider-specific authority.
- New contracts/statuses such as normalization/publication semantics require named decisions before implementation.

## Validation

Run focused inventory/project-instance/bootstrap/approval tests covering success, forbidden paths, secret exclusion, stale-state revalidation, and unreachable behavior. Never contact real servers/keys or runtime data.
