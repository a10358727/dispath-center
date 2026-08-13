# Settings Architecture

Dispatch Center has one configuration source and two views during the
monolith-to-modules transition:

1. `load_app_config()` reads the existing environment variables and
   `servers.yaml` into the mutable, flat `AppConfig` compatibility object.
2. `AppConfig.settings` builds an immutable `Settings` snapshot composed from
   bounded-context setting groups.

The typed view never reads the environment itself and is not cached. Code that
intentionally applies an existing compatibility normalization, such as
`apply_codex_config_rules()`, gets a fresh snapshot afterward. This prevents a
second source of truth while routers and workers are migrated incrementally.

All existing environment variable names and defaults remain supported. This
change does not enable OIDC, durable execution, Node assignment, dataset
publication, or another default-off capability.

## Typed groups

`app.settings.Settings` composes the following immutable groups:

| Group | Responsibility |
| --- | --- |
| `HttpSettings` | private bind address, port, process role, additive Product v2 gates |
| `DatabaseSettings` | SQLite path and local state root |
| `AuthSettings` | authentication transports, authorization mode, cookies |
| `OIDCSettings` | provider, callback, scopes, subjects, bounded timeouts |
| `SSHSettings` | existing SSH backend limits and key policy |
| `ExecutionSettings` | durable-attempt rollout dependencies |
| `SchedulerSettings` | scheduler and placement controls |
| `NodeSettings` | protocol/drain, assignment, lease, heartbeat, rotation |
| `DatasetSettings` | snapshots, publication, prewarm, reconciliation |
| `EngineeringSettings` | engineering backend and Codex runner controls |
| `LLMSettings` | optional Anthropic and vLLM configuration |
| `ObservabilitySettings` | audit/export worker, backup, monitoring, SMTP, result handling |
| `MachineSettings` | server inventory and bootstrap control |

Validation lives with the group that owns the rule. `Settings.validate()` calls
the groups in the legacy `AppConfig` validation order so an invalid deployment
continues to fail with the same messages and precedence. Cross-group checks are
limited to explicit dependencies, such as Node assignment requiring durable
execution reconciliation and outbox ownership.

`AppConfig` remains available while existing modules are extracted. New code
should accept the narrow group it needs rather than the whole compatibility
object. Do not add another environment parser or store a separately mutable
`Settings` instance.

`AUDIT_EXPORT_WORKER_ENABLED` is an explicit, default-off compatibility delivery
worker. When enabled it is valid only for `PROCESS_ROLE=all` or `PROCESS_ROLE=worker`;
the supervised loop claims `audit_export_operations` with the existing durable
lease and appends only to `AUDIT_PATH`. The manual `dispatch db audit-export`
command remains available. Enabling this worker does not constitute external
audit anchoring, retention approval, or a production notification policy.
`/readyz` additionally reports the worker's last successful delivery age and
fails closed after three missed success cadences or when a dead-letter row is
present; a pending backlog remains an operator `attention` signal rather than
an invented production SLO.

`EXECUTION_OUTBOX_WORKER_ENABLED` is also default-off and requires the durable
scheduler ownership path. The current lease owner must report a completed
outbox delivery/completion-recovery iteration within three scheduler cadences
for `/readyz` to remain ready. A non-leader process remains serveable for
read/approval traffic; pending or uncertain execution operations are exposed as
`attention` telemetry and are never converted into failed Jobs by readiness.

## Secrets and startup reporting

The typed view wraps these values in Pydantic `SecretStr`:

- shared authentication token;
- OIDC client secret;
- SMTP password;
- Anthropic API key;
- vLLM API key.

The corresponding legacy dataclass fields are excluded from `AppConfig`'s
representation. Startup emits a single `startup settings:` JSON report after
compatibility normalization. The report contains non-secret operational
values, feature states, and secret-presence booleans only. It never includes a
credential value, OIDC issuer/client details, SMTP username, server names, SSH
key paths, or Codex runner names.

When adding a credential, it must be masked in both representations and absent
from `Settings.safe_summary()` and `Settings.feature_report()`. Add a sentinel
test before using it at startup.

## Feature-flag lifecycle

`app/settings/features.py` is the reviewed registry for runtime feature
switches. Every entry records:

- a stable key and existing environment name;
- the owning settings group and attribute;
- an accountable owner;
- the compatibility default;
- dependency conditions;
- a lifecycle review date and retirement condition;
- an approved sunset date when one exists;
- deprecation and replacement metadata.

The next lifecycle review is 2027-02-03. That is a deadline to decide whether a
flag can retire, not permission to remove it automatically. `sunset_after`
remains empty until an approved decision supplies a removal date, migration,
and rollback path; the repository rule against fabricating missing historical
or rollout facts takes precedence. Permanent safety controls may remain after
review, but still require an explicit retirement condition in the registry.

`NODE_AGENT_V1_ENABLED` is the first deprecated compatibility flag. When it is
present in the environment or `.env`, configuration loading emits both a Python
`DeprecationWarning` and an operator-visible warning log. Its behavior is
unchanged: true still enables both split controls. New deployments should set:

```dotenv
NODE_PROTOCOL_DRAIN_ENABLED=false
NODE_NEW_ASSIGNMENT_ENABLED=false
```

Disabling new assignment while retaining protocol/drain remains the first Node
rollback action. The SSH execution backend remains available and default.

`AUTHORIZATION_MODE` accepts `off`, `shadow`, and `enforce`. `off` preserves the
legacy compatibility behavior; `shadow` appends would-deny evidence without
changing responses or side effects; `enforce` applies the closed route-action
catalog fail-closed, performs exact project row filtering on supported list
surfaces, requires service-token scopes, and does not promote the legacy shared
token to global administration. Enforcement is an explicit rollout state and
does not by itself prove hostile multi-tenant isolation.

The Project bootstrap rollout uses three default-off gates in dependency order:

```dotenv
API_V2_ENABLED=false
PRODUCT_RBAC_V2_ENABLED=false
PROJECT_BOOTSTRAP_V2_ENABLED=false
```

`PROJECT_BOOTSTRAP_V2_ENABLED=true` is invalid unless both dependencies are
true. Turning it off hides preview, request, bootstrap-decision, and Project
Workspace behavior while preserving Migration v7 rows and all approval/audit
evidence. It never runs Git, deploy, server-bootstrap, SSH, or other remote
side effects.

Host Environment revisions use a sibling default-off package gate:

```dotenv
API_V2_ENABLED=false
PRODUCT_RBAC_V2_ENABLED=false
PROJECT_ENVIRONMENTS_V1_ENABLED=false
```

`PROJECT_ENVIRONMENTS_V1_ENABLED=true` is invalid unless API v2 and Product
RBAC are both enabled. It does not depend on `PROJECT_BOOTSTRAP_V2_ENABLED`:
bootstrap and later standalone Environment mutation are independent rollback
packages over the same Migration v7 tables. Turning the Environment flag off
hides Environment list/request routes and `environment_change_v2` approval
list/detail/decision/workspace entries, while preserving immutable revisions,
approvals, idempotency rows, and audit evidence. Readiness remains a read-only
projection and the flag never starts a probe or executes setup commands.

Typed Run Templates and Project Defaults use a third default-off package gate:

```dotenv
API_V2_ENABLED=false
PRODUCT_RBAC_V2_ENABLED=false
PROJECT_ENVIRONMENTS_V1_ENABLED=false
RUN_TEMPLATE_V2_ENABLED=false
```

`RUN_TEMPLATE_V2_ENABLED=true` is invalid unless all three dependencies are
enabled. Turning it off hides Run Template and Defaults read/request routes,
their two approval kinds, decision handling, and Workspace projections while
preserving Migration v7 rows, approvals, idempotency identities, and audit
evidence. Typed execution remains independently controlled by the Product Run
package below.

Dataset assets, adoption, immutable aliases, and lineage use a separate
default-off package gate:

```dotenv
API_V2_ENABLED=false
PRODUCT_RBAC_V2_ENABLED=false
DATASET_ASSETS_V2_ENABLED=false
```

`DATASET_ASSETS_V2_ENABLED=true` is invalid unless API v2 and Product RBAC are
both enabled. It does not depend on Project Bootstrap, Host Environments, or
Run Templates. Turning it off hides Dataset list/detail/lineage/usage/storage,
legacy-snapshot adoption, alias-change routes, both Dataset approval kinds,
their decision handling, and Workspace Dataset projections. Migration v8
assets, exact snapshot links, immutable alias and lineage history, approvals,
idempotency identities, and durable audit evidence remain intact. The storage
read model never returns legacy source identity, manifest/descriptor paths, or
credentials. Dataset usage reports verified ExecutionPlan v2 bindings and the
complete empty result for `project-defaults-v1`, whose closed contract has no
Dataset binding field.

Dataset sharing is an independently reversible package over the same Migration
v8 tables:

```dotenv
API_V2_ENABLED=false
PRODUCT_RBAC_V2_ENABLED=false
DATASET_ASSETS_V2_ENABLED=false
DATASET_SHARING_V2_ENABLED=false
```

`DATASET_SHARING_V2_ENABLED=true` is invalid unless all three dependencies are
enabled. Turning it off hides share-offer, accept, and grant-withdrawal routes,
their approval list/detail/decision entries, target-Project aliases and reads,
and the Workspace sharing feature/capability projection. Owner-only PR-07
asset reads and aliases remain available when their own flag is enabled;
owner usage reports `dataset_sharing_v2_disabled` instead of inventing an empty
grant count. Existing offers, grants, revocations, aliases, approvals,
idempotency identities, and durable audit evidence are retained.

Dataset publication is another independently reversible package over the
existing snapshot builder and Migration v8 governance rows:

```dotenv
API_V2_ENABLED=false
PRODUCT_RBAC_V2_ENABLED=false
DATASET_ASSETS_V2_ENABLED=false
DATASET_SNAPSHOT_V1_ENABLED=false
DATASET_SNAPSHOT_PUBLISH_ENABLED=false
DATASET_PUBLISH_V2_ENABLED=false
# Comma-separated canonical absolute Server A roots; empty disables local mode.
DATASET_PUBLISH_LOCAL_ROOTS=
```

`DATASET_PUBLISH_V2_ENABLED=true` is invalid unless all five dependencies are
enabled. `DATASET_PUBLISH_LOCAL_ROOTS` entries must be absolute and unique at
typed-settings validation; runtime additionally rejects broad roots, missing or
symlinked root components, source/store overlap, non-canonical containment, and
special source entries. These roots are independent from every compute
`ServerConfig.dataset_roots` value. An empty root list is valid and leaves only
eligible Run-output publication available.

Turning the flag off hides both publish routes, `dataset_publish_v2` approval
projection/decision handling, Workspace capability, and the Publish Wizard. It
does not remove approved/building/published snapshots, content-addressed blobs,
assets, initial aliases, Run lineage, idempotency rows, approvals, or durable
audit evidence. Resume is available only when the package is re-enabled and the
same approved publish contract plus `building` snapshot remain verifiable.

Product ExecutionPlan v2 is a separate default-off package:

```dotenv
API_V2_ENABLED=false
PRODUCT_RBAC_V2_ENABLED=false
PROJECT_ENVIRONMENTS_V1_ENABLED=false
RUN_TEMPLATE_V2_ENABLED=false
DATASET_ASSETS_V2_ENABLED=false
RUN_EXPERIENCE_V2_ENABLED=false
```

`RUN_EXPERIENCE_V2_ENABLED=true` is invalid unless all five dependencies are
enabled. It exposes read-only Run preview, idempotent Run request, the verified
`execution_plan_v2` approval-detail/decision branches, and the Product Run
detail, Clone preview, Compare, Stop request, and Artifact metadata routes.
Preview resolves exact
ProjectVersion, typed Template or Defaults reference, Environment, Dataset
snapshots, eligible SSH target revision, checkout evidence, resources, argv,
and command digests without persisting any row. Submit requires the exact
preview digest and atomically creates one immutable plan companion plus pending
approval; a different enabled HUMAN Owner or Reviewer may materialize one
target-pinned Job after full revalidation.

The resulting `execution-plan-v2` Job is attempt-only. Actual SSH dispatch also
requires `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED=true`, a live fenced scheduler
owner, and an exact matching approved ServerConfig revision in the attempt
context. If any condition is absent, the Job remains queued; it never falls
back to the legacy SSH/sentinel/stall path. This preserves the reviewed target
when the current server revision changes after approval.

Approval detail preserves the exact four-field approval envelope and adds a
verified `review` containing the immutable review-safe ExecutionPlan spec.
Missing or inconsistent plan/companion evidence returns 409 only after caller
authorization; foreign callers remain opaque. Raw command, argv, observation,
normalized target, credential, host/user/key, checkout path, and setup command
data are never projected.

Dataset aliases are resolved once at preview/submit and historical exact alias
revision evidence is revalidated at approval; a later alias-head move does not
change the submitted snapshot. Active shared grants, current Template/
Environment/Defaults heads, Dispatch Policy head, target revision, checkout,
and fresh resource evidence fail closed when stale. `project-defaults-v1`
continues to fix only Environment, Run Profile, and parameter values. Dataset
selection is an independent per-ExecutionPlan input, so the Defaults section of
Dataset usage is an exhaustive available empty set rather than a configurable
Dataset-default feature.

Product Run is a read model over the immutable plan plus canonical Job/attempt/
operation evidence; no additional state table is created. Clone preview and
Compare are zero-write. Stop creates an approval and, after a human decision,
one durable pending operation for worker delivery; it never directly sets a
terminal Job status. Artifact responses contain only revalidated relative-path
metadata, never content, host, storage root, URL, or absolute path.

Turning the flag off hides preview/request, all five Product Run routes,
ExecutionPlan and Product Stop approval projection/decision behavior, and the
Workspace Product Run controls while retaining Migration v9, immutable plans,
approvals, idempotency rows, Jobs, attempts, pending operation outbox rows, and
durable audit evidence. It does not cancel or rewrite an already approved Job
and does not alter the v1 plan or SSH execution contracts.

## Verification

Run the settings compatibility tests with:

```bash
python -m pytest -q tests/test_config.py tests/test_typed_settings.py
```

The packaging and static invariant gates additionally verify that Pydantic is
a declared Control Plane dependency and that `app.settings` ships in the wheel.
