# AI Engineering Task remaining decision gate

> Status: awaiting explicit human decisions; not deployed.  This document is a
> review packet, not an approval record and not permission to change a protected
> invariant.  It describes the choices that remain after the bounded slices in
> `docs/archive/CURRENT_STATE.md` §§0.1–0.7.

## 1. What is already implemented

The current worktree already contains the progressive UI foundation, the
ProjectVersion-pinned Engineering Task parent contract, task visibility and
recovery records, the Codex-only provider seam, bounded ordinary-Job Worker
validation, the Project workspace and supporting UI surfaces, final-Git path
policy enforcement on both the Runner and Server A, and the sanitized collected
patch download.  These changes are feature-flagged where documented and have
not been deployed.

The following capabilities are deliberately not claimed:

- Codex app-server turns, resume, live events or command callbacks;
- turn-time filesystem confinement, dependency/network command policy or
  cgroup/quota resource enforcement;
- automatic repair loops, task-safe Continue/Retry/Cancel lifecycle actions;
- Run Profile persistence;
- finalize/discard/Hub promotion/GitHub publication; or
- authorization enforcement (the supported modes remain exactly `off|shadow`).

## 2. Decisions that block Controlled Server A execution

### D1 — Codex app-server adapter

Recommended decision: permit implementation and fake/in-process protocol tests
behind `CONTROLLED_CODING_RUNNER_V1=false`, but do not permit production
activation yet.

The bounded implementation would:

- register only a versioned allowlisted adapter such as
  `codex-app-server-v1`; users could never provide an executable;
- require an exact reviewed Codex CLI/app-server version and protocol capability
  set, and fail closed on version or message-shape drift;
- implement start/resume/cancel, events, command-approval correlation and final
  response through `CodingAgentProvider`;
- preserve `codex-exec-v1` as the legacy adapter and never silently fall back
  from an app-server turn after a side effect may have occurred;
- use fake protocol peers in repository tests and contact no real provider,
  Runner or worker; and
- require a separate canary/rollback sign-off before the feature flag is enabled
  in an operational environment.

Decision needed: **approve bounded implementation only**, **approve production
canary as well**, or **keep app-server unapproved**.

### D2 — Runner confinement and Server A resource enforcement

Recommended decision: require fail-closed preflight and actual enforcement; do
not represent soft warnings as limits.

Proposed initial ceilings are the accepted-plan defaults:

- 8 CPU cores and 16 GiB memory per task;
- 50 GiB task disk;
- 20 minutes per command;
- 45 minutes and at most three automatic repair rounds; and
- 2 hours cumulative Server A execution time.

Implementation may proceed only if the deployment target supports the reviewed
non-root cgroup v2/systemd-scope and disk-quota mechanism.  If a required
controller or quota is unavailable, controlled execution remains unavailable;
it must not downgrade to advisory enforcement.  No production host is probed by
repository tests.

The same decision must cover deterministic post-agent finalization.  The
current wrapper still performs Git index/tree/ref/diff/bundle processing after
the Codex sandbox has closed.  Disabling commit hooks, signing and external diff
drivers is useful defense in depth, but it does not prevent an agent-modified
clean/process filter, fsmonitor, smudge driver or oversized worktree from
executing or exhausting resources as the Runner OS user.  Production activation
therefore requires the complete finalization phase to run inside an equivalent
no-network sandbox with trusted/sterilized Git metadata and hard file-count,
byte, CPU, memory, time and disk ceilings.  A mismatch or unavailable sandbox
must fail closed before any post-turn worktree-aware Git command.

Approved Job command bytes remain immutable.  A queued command created before
that boundary exists must never be silently rewritten; operators must use the
existing approval-controlled cancellation path and create a new request only
after the safe contract is available.  The implementation decision must also
state whether new legacy-unpinned Coding Tasks are disabled or routed through
the same controlled finalizer once the native backend is enabled.

A 2026-07-15 read-only local capability audit found Codex CLI 0.144.4,
bubblewrap 0.6.1, unprivileged user/network namespaces and cgroup v2.  A nested
`unshare` + bubblewrap prototype demonstrated a minimal read-only root, an
explicit writable output and a network namespace with no IPv4 route.  This is
prototype evidence only, not Runner evidence: the current shell cannot delegate
a writable cgroup, reach a systemd user/system bus, or enforce an ext4 project
quota, and no usable container/FUSE quota runtime is available.  Consequently
`timeout`/`prlimit`/soft disk checks cannot be accepted as substitutes for the
aggregate CPU/RAM/process and 50 GiB task-disk guarantees.  Repository tests
must use pure builders/fakes until the actual non-root Runner service account
passes a reviewed preflight and canary.

Until this controlled validation/resource boundary is approved and implemented,
the outer Runner intentionally does not execute agent-modified pytest or other
repository code after a Codex turn.  A missing structured test result is
`not_run`, never inferred success.

Decision needed: **approve these exact limits plus the fail-closed finalization
sandbox prerequisite**, or provide replacement limits/mechanisms.

### D3 — Command and task-lifecycle approvals

Recommended decision: add narrowly scoped kinds and keep the auto-approval
allowlist unchanged at exactly `enqueue|stop`.

At minimum, controlled execution needs a decision for:

- `engineering_command` for a command not already covered by the immutable task
  approval, including exact command bytes/digest, working directory, policy
  family, network/dependency need, timeout/resources and parent task/turn;
- Continue/Request Changes/Retry turns (either dedicated kinds or a documented
  new version of the existing `coding_task` payload); and
- `engineering_task_discard`, `engineering_task_finalize`,
  `engineering_task_promote` and `engineering_task_pr`.

Every new request must validate before persistence, store an immutable complete
payload, revalidate material state at decision time, record requester/approver,
and append audit evidence.  The agent may create only a pending request and can
never approve/reject it.  A running cancellation continues to use the existing
`stop` approval and sentinel reconciliation; no task controller receives a
direct kill path.

Decision needed: approve the proposed kind split, or choose which lifecycle
actions should share a versioned `coding_task` contract.

## 3. Decisions that block later product slices

### D4 — Secret-reference backend

No generic secret broker exists in this repository.  Raw values remain rejected
from task and Run Profile forms.  Enabling opaque references requires a selected
provider, project/actor scope model, allowlisted names, short-lived materializing
mechanism, redaction contract and rotation/revocation behavior.  Agent
credentials, OIDC/session data, SSH keys and provider tokens must never be
materialized into an agent turn.

Decision needed: name the approved broker/provider and scope model, or keep this
capability disabled.

### D5 — Run Profile persistence

Recommended decision: permit an additive `run-profile-v1` schema with immutable
revisions and approval-gated create/update/archive operations.  Existing
`Project.default_command`, `setup_cmd` and `require_tag` remain compatible
legacy fields; they are not silently converted into a fabricated approved
profile.  A run stores the selected profile revision plus parameter/resource/
dataset override snapshot, then creates the ordinary `enqueue` approval and Job
through the existing scheduler.

Decision needed: approve the additive schema, profile approval kinds, restricted
typed-parameter schema and the no-automatic-migration policy.

### D6 — GitHub publication

Recommended decision: permit only a provider interface and fake-provider tests
first.  Operational activation later requires an approved GitHub App
registration, repository allowlist, installation scope, egress policy and
short-lived installation-token broker.  The first publication action creates a
draft PR only; it never merges, deploys or sends credentials to the coding
agent.

Decision needed: approve interface/fake tests only, approve a named operational
GitHub App configuration as well, or keep publication disabled.

### D7 — Authorization enforcement

No decision is requested in this plan.  Role-aware presentation remains
informational and `AUTHORIZATION_MODE` remains exactly `off|shadow`.  Any future
403/404 filtering, action hiding or server-side enforcement is a separate
protected-invariant decision and must not be bundled with the Engineering Task
work.

## 4. Release-evidence decisions and external prerequisites

As of 2026-07-15, the latest targeted checkpoints pass 83 UI, 124 backend-
safety, 36 Worker-validation pure (2 TestClient deselected), 30 visibility pure
(7 TestClient deselected), 97 pure Coding Task, 34 migration, 47 authorization,
and 13 frontend-auth tests. The static invariant gate is green, and collection
finds 2241 tests without errors. These are bounded checkpoints, not a substitute
for the post-download dependency-complete full pytest run or the deselected API/
TestClient paths. The latest full-run attempt was rejected by the execution
environment's TestClient usage limit; this is missing evidence, not a test
failure. The full suite must be rerun when that environment permits it and must
not be replaced by a narrower suite.

The accepted visual criteria also require real 1440, 1024, 768 and 375 pixel
browser evidence plus keyboard/focus/overflow checks.  No usable
automated/headless browser stack is present in this workspace.  A WebKitGTK
MiniBrowser binary exists, but this shell has no display, WebKitWebDriver, or
screenshot automation with which to drive it.  Playwright/Selenium and a cached
compatible browser runtime are also absent.  Do not use generated or mock
screenshots as evidence. Completing this gate requires either an approved
disposable browser automation installation or screenshots and manual results
from an operator-controlled test environment.

No restart is useful merely to resolve either evidence gap.  A Dispatch Center
process restart is needed only after operators deliberately change a process-
loaded feature flag such as `ENGINEERING_TASK_BACKEND_V1`; workers and the
Coding Runner do not need to be restarted for the bounded frontend/backend
slices.

## 5. Explicit response template

An approver can respond with one choice for each item, for example:

```text
D1 app-server: bounded implementation only / production canary / keep disabled
D2 resources: approve proposed limits / replacement limits: ... / keep disabled
D3 approval kinds: approve proposed split / use versioned coding_task for: ...
D4 secret broker: keep disabled / approved provider and scope: ...
D5 Run Profiles: approve proposed v1 / revise: ... / keep disabled
D6 GitHub: interface+fake only / operational config: ... / keep disabled
Browser evidence: authorize disposable install / operator will provide / defer
```

Silence or a partial answer is not approval for the omitted items.
