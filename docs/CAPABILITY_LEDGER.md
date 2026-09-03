# Capability Ledger

> Updated: 2026-09-03 (DG-CONSOLIDATION-v1 C-4 format)
> Authority: this is the single current capability-status ledger. Its
> authority is `docs/PLATFORM_CHARTER.md` (§6 invariants) and named decisions
> in `docs/DECISIONS.md`; this ledger cannot authorize a feature or change an
> invariant. Truth order: charter invariants + `docs/DECISIONS.md` →
> code/tests → this ledger →
> `docs/product/ROADMAP.md` → `docs/archive/` historical text.

## Field meanings

- `Ruling`: the named decision that bounds the capability (`—` = pre-decision-gate legacy).
- `Implemented`: `yes` (operable runtime code), `test-only` (fake/local evidence only), `partial`, `no`, `retired`.
- `Default`: what a clean configuration activates — `on`, `off`, `n/a`. Since packet C6 the product/platform chain defaults `on` (C-2); safety-posture flags and the execution-attempt chain stay `off` until their own ruling (`tests/test_pilot_posture.py` is the source).
- `Pilot`: enabled and running in the personal pilot (`/home/aied/pilot-run`) — `on`, `off`, `n/a`. Pilot evidence is personal-pilot-only and never canary or production evidence (DG-PERSONAL-PILOT-v1 D1 clarification).
- `Canary`: the plan's real-environment/time-window gate passed for the *current* candidate — `yes` requires a `docs/evidence/` link; `no`, `n/a`.
- `Evidence`: one line (≤ 160 characters) pointing at a ruling, a test, or an evidence file. Paragraphs belong in `docs/DECISIONS.md` 補充紀錄, not here.

Production readiness is not a column: no capability may claim it except through a named ruling recorded in `docs/DECISIONS.md`.

Maintenance rule: one packet touches one row (or adds one). A row change that is not a one-line edit is a sign the change needs a 補充紀錄 instead.

## Active

| Capability | Ruling | Implemented | Default | Pilot | Canary | Evidence |
|---|---|---|---|---|---|---|
| `phase0_release_gate` | — | yes | on | on | n/a | `make gate`: ruff, mypy, static_checks, coverage 62%, full suite 4331 (xdist); CI 5 jobs (C4) |
| `core_control_plane` | DG-EXEC-ATTEMPT-v1 | yes | on | on | no | FastAPI/SQLite monitor, scheduler, approvals; RB-LAUNCH-001 open |
| `approval_and_audit` | DG-CONSOLIDATION-v1 U2 | yes | on | on | no | hash-chained `audit_events` + JSONL outbox; adoption catalog 84 entries; card title/summary (`app/approval_presentation.py`) |
| `ssh_execution_v1` | DG-C | yes | on | on | no | agentless SSH/SFTP/tmux backend, INV-SSH-*; `tests/test_scheduler.py` |
| `attempt_driven_ssh` | DG-WP2D-CANARY-v2 | yes | off | on | no | historical candidate passed `docs/evidence/WP2D_V2_20260802_D73A38E.md`; current candidate must repeat the window (RB-LAUNCH-001) |
| `execution_attempt_outbox` | DG-EXEC-ATTEMPT-v1 | yes | off | on | no | outbox worker + new-claims + SSH launch flags on the pilot; `tests/test_execution_attempt_dispatch.py` |
| `immutable_execution_plan` | DG-EXEC-ATTEMPT-v1 | yes | on | on | no | plan→approval→Job→attempt lineage; `GET /runs/{plan_id}` |
| `worker_process_split` | — | yes | off | off | no | `PROCESS_ROLE=worker` entry point exists; pilot runs role `all` |
| `oidc_identity` | Slice 7 (2026-07-13) | yes | off | on | n/a | pilot logs in through OIDC; `tests/test_oidc.py` |
| `authorization_shadow` | Goal 1 | yes | off | off | n/a | observational resolver seam used by enforce; `tests/test_authorization_shadow.py` |
| `authorization_enforcement` | DG-PRODUCT-RBAC-V2-v1 | yes | off | on | no | pilot runs `AUTHORIZATION_MODE=enforce` + self-approval (personal-pilot only; DG-AUTHZ-ENFORCE for any other environment) |
| `api_v2_foundation` | DG-API-V2-FOUNDATION-v1 | yes | on | on | no | `/api/v2` root, APIError/request-id, cursors, migration 5 |
| `product_rbac_v2` | DG-PRODUCT-RBAC-V2-v1 | yes | on | on | no | migration 6 role bindings; `ALLOW_HIGH_RISK_SELF_APPROVAL=true` on the pilot (DG-SELF-APPROVAL-OPTION-v1) |
| `project_bootstrap_v2` | DG-PROJECT-BOOTSTRAP-V2-v1 | yes | on | on | no | migration 7; workspace projection feeds the Studio 執行設定 panel (U4) |
| `project_environments_v1` | DG-PROJECT-ENVIRONMENTS-V1 | yes | on | on | no | `environment_change_v2` from the Studio (U4a); `tests/test_project_environments_v1.py` |
| `run_template_v2` | DG-RUN-TEMPLATE-V2 | yes | on | on | no | `run_template_change_v2`/defaults from the Studio (U4b); `tests/test_run_templates_v2.py` |
| `dataset_assets_v2` | DG-DATASET-ASSETS-V2-v1 | yes | on | on | no | migration 8 governance schema; `tests/test_dataset_assets_v2.py` |
| `dataset_sharing_v2` | DG-DATASET-SHARING-V2-v1 | yes | on | on | no | offer/accept/grant/revoke; `tests/test_dataset_sharing_v2.py` |
| `dataset_publish_v2` | DG-DATASET-PUBLISH-V2-v1 | yes | on | on | no | local-path and Run-output publish; `tests/test_dataset_publish_v2.py` |
| `immutable_dataset_snapshot` | DG-DATASET-SNAPSHOT-v1 | yes | on | on | no | request→approve→build→publish; `tests/test_dataset_snapshot.py` |
| `mutable_dataset_registry` | DG-DATASET-SNAPSHOT-v1 D-1 | yes | on | on | n/a | `POST /datasets` declaration, `reproducible=0` |
| `execution_plan_v2` | DG-EXECUTION-PLAN-V2-v1 | yes | on | on | no | migration 9/17; preview→submit→approve→one Job; `tests/test_execution_plan_v2_api.py` |
| `product_run_experience_v2` | DG-PRODUCT-RUN-EXPERIENCE-V2-v1 | yes | on | on | no | detail/timeline/clone/compare/stop; `tests/test_product_runs_v2.py` |
| `experiment_v2` | DG-EXPERIMENT-V1 | yes | on | on | no | one matrix = one approval; pilot ran a 4-run matrix; Studio RunComposer (U6) |
| `metrics_v1` | DG-METRICS-CONTRACT v1 | yes | on | on | no | pilot job 94 end-to-end (2026-08-25); `tests/test_metrics_v1.py` |
| `run_profile_v1` | D5 | yes | on | on | no | immutable revisions pinned by ExecutionPlan |
| `dispatch_policy_v1` | DG-1/DG-2 | yes | on | on | no | policy revisions for auto placement |
| `auto_placement` | DG-2 (INV-APPROVAL-4b) | yes | on | on | no | proposals on, `AUTO_PLACEMENT_KILL_SWITCH` armed on the pilot |
| `dataset_prewarm` | DG-B4 | yes | on | on | no | new-machine prewarm; kill switch armed |
| `server_config_management` | DG-INFRA-DIRECT-ACTIONS v1 | yes | on | on | no | add/update/disable direct + audited; delete by approval; revision/journal protocol |
| `server_bootstrap_v1` | DG-B | yes | on | on | no | approval-gated bootstrap; `tests/test_server_bootstrap.py` |
| `hardware_execution_v1` | DG-HARDWARE-EXECUTION v1 | yes | on | on | no | P1–P4 landed: devices, image registry, `hardware_action_v2`, SFTP push, receipts, known-good, physical_tools, Studio 硬體 panel (migrations 21–24) |
| `code_promotion_v1` | DG-CODE-PROMOTE-v1 | yes | on | on | no | checkpoint→promote from the Studio (U3); `tests/test_code_promotion.py` |
| `agent_session_checkpoint` | DG-AGENT-SESSION-CHECKPOINT | yes | on | on | no | `agent_session_checkpoint` kind + bridge task; `tests/test_agent_session_checkpoint.py` |
| `agent_runtime_v3` | DG-AGENT-RUNTIME-V3 v1 | yes | on | on | no | runner 106 enrolled; Phases 1a–2 complete; `tests/test_agent_gateway.py` |
| `studio_ui_v1` | DG-STUDIO-UI v1 | yes | n/a | on | no | sole UI since 2026-08-31; 整頓 U1–U8; Vitest 31; `scripts/frontend_smoke.py` |
| `single_operator_confirm` | DG-SINGLE-OPERATOR-CONFIRM v1 | yes | n/a | on | n/a | 確認並執行 for the closed kind list (`studio/src/features/approvals/singleOperator.ts`) |
| `assistant_tools_v1` | DG-ASSISTANT-TOOLS v1 | yes | on | on | no | per-turn `dat_` tokens + stdio bridge; runner python packages required |
| `backup_restore` | — | yes | off | off | no | online backup + `scripts/restore_drill.py`; `docs/evidence/LOCAL_RESTORE_DRILL_20260806_AB0376F.json`; DG-OPS-SLO pending |

## Gated (needs its own ruling or canary before activation)

| Capability | Ruling | Implemented | Default | Pilot | Canary | Evidence |
|---|---|---|---|---|---|---|
| `node_protocol_v1` | DG-C | test-only | off | off | no | compatibility protocol behind drain flag |
| `node_protocol_v2` | DG-NODE-V2-v1 | test-only | off | off | no | lease/ack/terminal contract; activation needs DG-NODE-CANARY (RB-NODE-001) |
| `node_daemon` | DG-NODE-V2-v1 | test-only | off | off | no | `python -m agent --check` runs; no node enrolled |
| `workload_isolation_v1` | DG-NODE-V2-v1 | test-only | off | off | no | user-systemd attempt unit contract; no unit installed |
| `github_publication` | D6 | test-only | off | off | no | interface + fake only; DG-GITHUB-PUBLISH gate |

## Retired

| Capability | Ruling | Implemented | Default | Pilot | Canary | Evidence |
|---|---|---|---|---|---|---|
| `codex_exec_runner_v1` | DG-AGENT-RUNTIME-V3 Phase 1b | retired | n/a | n/a | n/a | execution channel removed 2026-08-31; request/execution routes, registry and backend flag deleted 2026-09-03 (C-5 (c)); read-only history + promote stay |
| `codex_app_server_adapter` | DG-AGENT-RUNTIME-V3 Phase 1b | retired | n/a | n/a | n/a | fake adapter removed with the provider |
| `claude_code_agent_v1` | DG-AGENT-RUNTIME-V3 Phase 1b | retired | n/a | n/a | n/a | tmux `claude -p` turns replaced by SDK sessions |
| `assistant_claude_turn` | DG-AGENT-RUNTIME-V3 Phase 1b | retired | n/a | n/a | n/a | runner `claude -p` brain removed; API-key UI exception kept |
| `agent_session_v1` | DG-AGENT-RUNTIME-V3 Phase 1b | retired | n/a | on | n/a | D2 turn channel removed; `agent_session_open` kind and tables stay; workbench routes deleted in C8 |
| `project_conversation_v1` | DG-CONSOLIDATION-v1 C-5 | retired | n/a | n/a | n/a | routes and `app/conversations.py` deleted 2026-09-02; `ai_conversation*` tables stay |
| `frontend_workflow` (legacy Workspace) | DG-STUDIO-UI v1 P3-4 | retired | n/a | n/a | n/a | `static/workspace.*` deleted 2026-08-31 |

## Release blockers

| Blocker | Current evidence | Required destination |
|---|---|---|
| `RB-LAUNCH-001` | historical candidate `d73a38e` passed the WP-2D v2 window (`docs/evidence/WP2D_V2_20260802_D73A38E.md`); later commits changed the path | repeat the full window for the current candidate before promoting the attempt path or closing the release gate |
| `RB-NODE-001` | Node v2 daemon/protocol implemented and locally tested; no real node evidence | DG-NODE-CANARY + Phase 5 (2 nodes / 100 jobs / 7 days); production workers stay on SSH |

Resolved: `RB-DATASET-001` (D-1 labelled the registry, D-5 rejects non-reproducible runs), `RB-SERVER-001` (all server mutations publish through the revision protocol), `RB-STOP-001` (WP-1C stop semantics).

## Updating this ledger

Every packet updates its row (one line) or adds one. `Canary=yes` needs a `docs/evidence/` link in the same row; `Pilot=on` needs the flag or deployment to actually be live in `/home/aied/pilot-run`; nothing here is inferred from passing unit tests.
