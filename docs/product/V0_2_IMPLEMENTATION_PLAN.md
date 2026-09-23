# Dispatch Center V0.2 Implementation Plan

> **Purpose:** execution plan and progress ledger for V0.2 work packets. V0.1 is
> closed (`V0_1_IMPLEMENTATION_PLAN.md`, 2026-09-23); V0.2 packets are added here
> one at a time, each bounded by a named ruling in `docs/DECISIONS.md`.
> **Status:** WP1–WP3 done; no packet `READY`
> **Last updated:** 2026-09-23
>
> This plan does not authorize protected architecture changes. Charter and named
> Decisions remain authoritative; the Plan Rules of `V0_1_IMPLEMENTATION_PLAN.md`
> §1 apply unchanged (statuses, `DONE` requirements, no weakening of acceptance).

# 1. Program Board

| WP | Title | Status | Depends on | Ruling | Outcome |
|---|---|---|---|---|---|
| WP1 | AI Provider Usage on Overview | DONE | V0.1 closed | DG-AI-USAGE-OVERVIEW-v1 | read-only Claude Code / Codex usage card without touching provider selection, agent authority, execution authority, or release semantics |
| WP2 | Studio zh-TW localization | DONE | WP1 | — (UI text only) | every Studio surface Chinese-first with the English term as small subtext; no behaviour change |
| WP3 | GitHub-only project import | DONE | WP2 | DG-PROJECT-GITHUB-IMPORT-v1 | projects are created only from a GitHub repository with a non-empty README.md; the platform clones on the worker under approval |

# 2. WP1 — AI Provider Usage on Overview

**Status:** DONE

## Goal

Show Claude Code and Codex usage on the Studio Overview page from a separate,
read-only local projection. The existing `GET /api/v2/ai-providers/usage`
(Dispatch assistant accounting) keeps its semantics.

## Decision gate

Satisfied by `DG-AI-USAGE-OVERVIEW-v1` (2026-09-23, user's words recorded in
`docs/DECISIONS.md`). No approval kind, provider, execution path, Job state, or
`INV-*` changes.

## Scope

- Backend: `app/ai_usage/` (bounded reverse-reading JSONL parsers for Codex and
  Claude Code, a separate Claude quota adapter seam that returns unavailable,
  and the projection assembler with a short in-process cache);
  `GET /api/v2/ai-providers/quota` in `ai_providers_v2.py` behind
  `AI_USAGE_V1_ENABLED` (default on) with `platform.view` classification.
- Studio: `features/overview/AiUsageCard.tsx` rendered on `OverviewPage`.
- Docs: decision record, Charter §7.1 row, ledger row, `SETTINGS.md`,
  `.env.example`, this plan.

## Acceptance

- [x] `GET /api/v2/ai-providers/quota` returns `ai-provider-quota-v1` with the
      four categories (account quota, local usage, context usage, estimated
      cost) per provider, each carrying `availability` + `reason`, never
      relabelled; flag off → 404; `Cache-Control: no-store`.
- [x] Codex: newest `token_count` rate-limit snapshot (windows with
      `used_percent`, `resets_at`, `plan_type`; `current` / `stale` /
      `expired` states), today tokens from `token_usage_record` with cumulative
      fallback, context from `last_token_usage` vs `model_context_window`;
      `auth.json` never opened; no network.
- [x] Claude Code: today tokens and context from `projects/**/*.jsonl`
      assistant usage only (deduped by message id + request id); account quota
      `unavailable` (`requires_credentialed_api`) via a separate adapter seam;
      `.credentials.json` / `history.jsonl` never opened; no transcript text.
- [x] Bounded scanning (file count, bytes per file, line length, total bytes),
      realpath containment, graceful `home_missing` / `no_session_files` /
      `scan_error` results, response with numbers, model names and timestamps
      only.
- [x] Studio card shows Claude Code and Codex separately with 5h / weekly
      windows, reset countdown, today tokens, and explicit loading / error /
      unavailable / stale / expired states; no provider switching, alerts,
      actions, or account mutation.
- [x] Deterministic offline tests: Codex and Claude fixtures, malformed /
      partial / stale events, missing home, no-leakage spies, API contract,
      Studio card states; full repository gate green.

## Evidence

Reference parsers inspected for ideas only (no code copied):
bhutano/codex-usage-bar (MIT; `event_msg/token_count`, `rate_limits.primary/
secondary.used_percent/resets_at`, context = `last_token_usage.total_tokens /
model_context_window`), ccusage/ccusage (MIT; Claude `message.usage` fields,
`message.id` + `requestId` deduplication, daily bucketing), wakamex/ccusage
(Claude quota via OAuth credential + undocumented `oauth/usage` endpoint —
deliberately not adopted; quota stays unavailable).

Local formats verified on the development host with structure-only inspection
(no content read into the conversation): Codex `token_count` events carry
`rate_limits.primary = {used_percent, window_minutes: 300, resets_at}` and
`secondary = {…, window_minutes: 10080, …}` plus `plan_type`; newer Codex
also writes `token_usage_record` lines with per-response `usage`; Claude Code
assistant lines carry `message.usage.{input_tokens, cache_creation_input_tokens,
cache_read_input_tokens, output_tokens}` and `requestId`.

Implementation: `app/ai_usage/{common,codex_local,claude_local,claude_quota,projection}.py`,
`GET /api/v2/ai-providers/quota` (`ai_providers_v2.py`, `platform.view`,
`AI_USAGE_V1_ENABLED` + `AI_USAGE_HOME_DIR`), `studio/src/features/overview/AiUsageCard.tsx`
on `OverviewPage`. Tests: `tests/test_ai_usage_projection.py` (14),
`tests/test_ai_providers_v2_api.py` (+3), `studio/src/features/overview/AiUsageCard.test.tsx`
(7), `OverviewPage.test.tsx` (updated); fixtures `tests/fixtures/ai_usage/`. See the
change log for validation counts.

# 3. WP2 — Studio zh-TW localization

**Status:** DONE

## Goal

Make the whole Studio Traditional-Chinese first (user decision 2026-09-23:
「中文為主，英文為下面的小字」): headings, navigation, card titles and product
nouns show Chinese with the original English term as small muted subtext;
buttons, badges, messages, placeholders and tooltips are Chinese only;
technical identifiers (SHAs, branches, paths, model names, units, SSH/GPU/
FPGA/TOFU) stay as-is.

## Scope

`studio/src/components/Shell.tsx`, `App.tsx`, every page under
`studio/src/pages/`, `features/session/*`, `features/compute/*`,
`features/project/*`, `features/runs/*`, `features/hardware/*`,
`features/approvals/*` and their tests. No API, routing, state or behaviour
change; no backend change.

## Acceptance

- [x] No English sentence remains as a primary label; English appears only as
      subtext next to a Chinese heading/noun or as a technical identifier.
- [x] Accessible names remain queryable (tests query by role + Chinese name).
- [x] Full Studio suite, `tsc --noEmit`, `npm run build` and
      `scripts/frontend_smoke.py` pass.

# 4. WP3 — GitHub-only project import

**Status:** DONE

## Goal

Enforce the user's rule (2026-09-23): a project can only be added from a GitHub
repository — an existing one, or a new empty one the user creates on GitHub —
and its README.md must exist and be non-empty. The platform clones the
repository onto the worker after approval (`DG-PROJECT-GITHUB-IMPORT-v1`,
user's words in `docs/DECISIONS.md`).

## Scope

- `app/github_import.py`: closed URL contract, pure command builders,
  request validation (`request_github_import_approval`).
- `app/db.py`: kind `project_github_import`; durable intent / unknown /
  finalize methods mirroring `project_deploy` (project + instance + version
  committed atomically on success).
- `app/approvals.py`: approve branch (mkdir → clone to staging → README check
  → mv → HEAD/branch → finalize; README missing removes only the staging
  directory; other failures leave the site for manual inspection; response
  loss reconciles read-only); GitHub-only checks on scanned candidates at
  request and decision time.
- API: `POST /api/v2/projects/github-import-requests`; legacy direct create
  403 `github_project_required`; bootstrap sources must be GitHub; flag
  `PROJECT_GITHUB_ONLY_ENABLED` (default on) in config/registry/posture.
- Studio: `features/project/GithubImportCard.tsx` on the import page with the
  rule, the form, a README template and the "create it on GitHub first"
  guide; non-compliant candidates are labelled and cannot be imported;
  `project_github_import` joins the single-operator confirm list.

## Acceptance

- [x] Only canonical `https://github.com/<owner>/<repo>` URLs are accepted;
      no credentials, query or fragment; ref validated; dest path rules as
      deploy.
- [x] Approval clones on the worker, rejects when README.md is missing or
      empty (only the platform-created staging directory is removed), and
      registers project + instance + version atomically otherwise.
- [x] Legacy direct create, non-GitHub candidates (at request and decision
      time) and non-GitHub bootstrap sources are refused while the flag is on;
      flag off restores the previous behaviour.
- [x] Server A holds no GitHub credential; `DG-GITHUB-PUBLISH` untouched.
- [x] Studio import page leads with the GitHub import card and the rule;
      offline tests cover contract, approve flow, policy, API and card.

## Evidence

`tests/test_github_import.py` (38 tests), adjusted legacy/bootstrap/inventory
tests pin the policy switch explicitly, `studio/src/features/project/
GithubImportCard.test.tsx` (4). Validation counts in the change log.

# 5. Change Log

## 2026-09-23 — WP1 AI Provider Usage on Overview

Status: DONE

Implemented:
- `app/ai_usage/`: bounded reverse-reading JSONL parsers for Codex
  (`~/.codex/sessions/**/*.jsonl`: newest `token_count` rate-limit snapshot with
  `current`/`stale`/`expired` window states, today tokens from
  `token_usage_record` with cumulative-diff fallback, context from
  `last_token_usage` vs `model_context_window`) and Claude Code
  (`~/.claude/projects/**/*.jsonl` assistant `message.usage` only, deduped by
  message id + request id, context reported `partial` with unknown window);
  a separate Claude quota adapter seam returning `requires_credentialed_api`;
  projection assembler with four never-mixed categories, `scan_error`
  degradation, and a 30 s in-process cache. Scanning is capped (200 files
  considered, 50 scanned for today, 10 searched for the snapshot, 32 MiB per
  file, 1 MiB per line, 128 MiB total), realpath-contained, symlink-free, and
  every open goes through one spy-able entry point.
- `GET /api/v2/ai-providers/quota` (`platform.view`, `no-store`, flag
  `AI_USAGE_V1_ENABLED` default on, `AI_USAGE_HOME_DIR` override), registered
  in the feature registry, pilot posture, authorization catalog and OpenAPI
  snapshot; `/ai-providers/usage` untouched.
- Studio「AI 使用量」card on Overview: Claude Code and Codex blocks with account
  quota windows (5 小時 / 每週, progress bars, reset countdown refreshed every
  30 s), today tokens, context usage, estimated cost (always 未提供), stale /
  expired / unavailable / loading / error / 403 states; the only control is
  重試; 60 s polling.
- Docs: `DG-AI-USAGE-OVERVIEW-v1` (user's words) in `docs/DECISIONS.md`,
  Charter §7.1 rows (this ruling and the missing `DG-SSH-HOSTKEY-v1` row),
  ledger row `ai_usage_projection_v1`, `SETTINGS.md`, `.env.example`.

Validation:
- `tests/test_ai_usage_projection.py`: PASS, 14 tests (windows, fallback
  snapshot, stale/expired, usage records + cumulative fallback, malformed /
  partial / oversized lines, missing home / session dirs, no-leak + decoys never
  opened, symlink escape, Claude dedupe / context / quota seam, categories,
  cache, scan_error).
- `tests/test_ai_providers_v2_api.py`: PASS, 24 tests (3 new: contract +
  no-leak + read-only, flag-off 404, missing home).
- Route inventory / authorization coverage / OpenAPI snapshot / pilot posture /
  config / settings: PASS.
- Studio: Vitest PASS, 83 tests in 22 files; `tsc --noEmit` PASS;
  `npm run build` PASS; `scripts/frontend_smoke.py` PASS.
- `make gate`: ruff, mypy, static invariant checks, audit adoption gate, mirror
  check, coverage gate 61.86%: PASS; full suite `make test`: PASS, 4020 tests
  (xdist).

Acceptance:
- All six WP1 criteria pass offline. Local formats were verified on the
  development host by structure-only inspection; reference projects were used
  for parser ideas only (MIT; no code copied).
- Not claimed: live Claude account quota (unavailable by design), cost
  estimation, and pilot deployment.

Plan changes:
- WP1 → DONE. No packet is promoted to `READY`.

Remaining risk:
- Codex rate-limit windows are session-level snapshots; between Codex turns the
  card shows the last observation (stale/expired states make this explicit).
- The Codex `rate_limits` schema has changed across CLI versions
  (`resets_in_seconds` → `resets_at`); both are accepted, newer shapes may need
  a parser update.
- The pilot has not been redeployed; the ledger row records `Pilot: off`.

## 2026-09-23 — WP2 Studio zh-TW localization

Status: DONE

Implemented:
- 29 Studio files translated (navigation, Overview, Compute/ServersPage,
  AddComputeWizard, HostIdentityPanel, Runs, Datasets, Activity, Import,
  Project, placeholder pages, session cards: RunMonitorCard, ReadyToRunCard,
  SessionView, Transcript, options/dialog) with the Chinese-first + English
  subtext convention; context telemetry labels (Provider-reported/Estimated)
  rendered as 供應商回報／估計值 without changing the typed values.
- Tests updated to Chinese accessible names; no assertion removed.

Validation:
- Studio: Vitest PASS, 83 tests in 22 files; `tsc --noEmit` PASS; `npm run
  build` PASS; `scripts/frontend_smoke.py` PASS.
- Backend: `make test` PASS (no backend change).

Plan changes:
- WP2 → DONE. WP3 (GitHub-only project import) recorded as PLANNED; its
  design is captured for the `DG-PROJECT-GITHUB-IMPORT-v1` ruling.

Remaining risk:
- Product nouns keep their English subtext by design; future pages must
  follow the same convention (no i18n framework was introduced).

## 2026-09-23 — WP3 GitHub-only project import

Status: DONE

Implemented:
- `DG-PROJECT-GITHUB-IMPORT-v1` recorded with the user's words; Charter §7.1
  row; ledger row `project_github_import_v1`; `SETTINGS.md` / `.env.example`.
- Backend module, approval kind with durable intent/outcome, policy gates on
  legacy create / candidates / bootstrap, request route, authorization
  catalog + OpenAPI snapshot, audit action catalogue, card title.
- Studio GitHub import card + candidate compliance badge + single-operator
  confirm kind.

Validation:
- `tests/test_github_import.py`: PASS, 38 tests.
- Affected suites (deploy, inventory, bootstrap, legacy projects, audit
  adoption, presentation, authorization coverage, snapshot, posture, config):
  PASS.
- Studio: Vitest PASS, 87 tests in 23 files; `tsc --noEmit` PASS.
- `make gate`: check (ruff, mypy, static invariant checks incl. INV-APPROVAL-1
  kind pin, audit adoption, mirrors) PASS; coverage gate 62.06% PASS; full
  suite `make test` PASS, 4058 tests (xdist).

Plan changes:
- WP3 → DONE. No packet promoted to `READY`.

Remaining risk:
- Private repositories require a deploy key on the worker (by design; Server A
  holds no GitHub credential). Existing projects imported before the rule are
  untouched.
- The staging directory suffix `.dispatch-import-<id>` must stay stable for
  the bounded cleanup guard.
