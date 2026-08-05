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
| `HttpSettings` | private bind address, port, process role |
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

## Verification

Run the settings compatibility tests with:

```bash
python -m pytest -q tests/test_config.py tests/test_typed_settings.py
```

The packaging and static invariant gates additionally verify that Pydantic is
a declared Control Plane dependency and that `app.settings` ships in the wheel.
