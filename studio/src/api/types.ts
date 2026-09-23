export interface Me {
  authenticated: boolean;
  authentication_method?: string | null;
  oidc_enabled?: boolean;
  actor: { id: string; type: string; display_name: string | null; platform_admin: boolean } | null;
  project_memberships?: { project_id: string; role: string }[];
}

export interface ProjectInstance {
  server?: string;
  path?: string;
  git_branch?: string | null;
  git_commit?: string | null;
  dirty?: boolean;
  state?: string;
}

export interface Project {
  id?: string;
  name: string;
  repo_or_path?: string;
  instances?: ProjectInstance[];
}

export interface TemplateParameter {
  name: string;
  type?: string;
  required?: boolean;
  enum_values?: string[];
  minimum?: unknown;
  maximum?: unknown;
}

export interface ProjectWorkspace {
  project?: { id: string; name: string };
  environment?: {
    id?: string;
    name?: string;
    revision_id?: string;
    revision?: number;
    status?: string;
    required_server_tags?: string[];
    preflight_kinds?: string[];
  } | null;
  run_template?: {
    id?: string;
    name?: string;
    revision?: number;
    status?: string;
    environment_revision_id?: string;
    spec_digest?: string;
    parameters?: TemplateParameter[];
  } | null;
  defaults?: { revision_id?: string } | null;
  run_creation_options?: {
    project_version_candidates?: { id: string; created_at?: string; state?: string }[];
    ssh_target_candidates?: {
      id?: string;
      server_name: string;
      revision?: number;
      preflight_state?: string;
      ready?: boolean;
      readiness_state?: "ready" | "not_ready" | "blocked" | "unknown";
      readiness_reasons?: string[];
      registered_instance_id?: string | null;
      update_available?: boolean;
      matching_promoted_version_ids?: string[];
      instance_state?: string;
    }[];
  };
}

/** One item of `GET /api/v2/projects/{id}/environments`. */
export interface EnvironmentHead {
  environment_id: string;
  name: string;
  status: string;
  head_revision?: { revision?: number; revision_id?: string; required_server_tags?: string[]; setup_command?: string };
  readiness?: { state?: string; reasons?: string[] };
  approval_id?: number | null;
}

export interface LiveServer {
  name: string;
  online?: boolean;
  enabled?: boolean;
  error?: string | null;
  updated_at?: string | null;
  disk_avail_bytes?: number | null;
  load1?: number | null;
  cpu_count?: number | null;
  mem_total_bytes?: number | null;
  mem_available_bytes?: number | null;
  gpu_count?: number;
  gpu_util_max?: number | null;
  gpus?: { util_percent?: number; mem_used_mb?: number; mem_total_mb?: number }[];
  /** DG-HARDWARE-EXECUTION v1 P1: declared devices with the latest observed
   *  presence (`present` | `absent`; missing = not observed). */
  devices?: Record<string, string> | null;
}

export interface ServerConfig {
  name: string;
  host?: string;
  user?: string;
  key?: string;
  port?: number;
  gpu?: boolean;
  tags?: string[];
  enabled?: boolean;
  note?: string | null;
  project_roots?: string[];
  dataset_roots?: string[];
  devices?: { id?: string; kind?: string; model?: string; serial?: string; tags?: string[] }[] | null;
  attempt_backend_preflight?: string | null;
  attempt_backend_preflight_observed_at?: string | null;
  host_identity?: SSHHostIdentity | null;
  [key: string]: unknown;
}

export interface SSHHostIdentity {
  id?: string;
  state: "trusted" | "tofu" | "mismatch" | "rebind_required" | "revoked" | "untrusted";
  host?: string;
  port?: number;
  algorithm?: string;
  fingerprint_sha256?: string;
  verification_method?: "oob" | "tofu";
  independently_verified?: boolean;
  trusted_at?: string;
  mismatch_fingerprint_sha256?: string | null;
}

export interface AuditRecord {
  event_id?: string;
  ts?: string;
  action?: string;
  result?: string;
  actor?: { id?: string | null; kind?: string | null; authentication?: string | null } | null;
  source?: string;
  durability?: string;
  [key: string]: unknown;
}

/** One row of `GET /api/v2/projects/{id}/hardware-images` (P2/P3b). */
export interface HardwareImage {
  id: string;
  project_id: string;
  project_version_id: string;
  build_plan_id: string;
  job_id: number;
  kind: string;
  output_name: string;
  relative_path: string;
  sha256: string;
  size_bytes: number;
  target_device_kind?: string | null;
  registered_at: string;
  known_good_marked_by_approval_id?: number | null;
  known_good_marked_at?: string | null;
  known_good_marked_by_actor_id?: string | null;
  known_good_source?: "decision" | "direct" | null;
}

/** One row of `GET /api/v2/projects/{id}/hardware-receipts` (P3b). */
export interface HardwareReceipt {
  job_id: number;
  approval_id: number;
  action_class: string;
  status: "collected" | "missing" | "invalid" | "oversize";
  reason?: string | null;
  receipt_json?: string | null;
  source_sha256?: string | null;
  collected_at: string;
  execution_plan_id?: string;
}

/** One row of `GET /api/v2/projects/{id}/run-templates`. */
export interface RunTemplateHead {
  run_profile_id: string;
  name: string;
  revision: number;
  status: string;
  classification: string;
  head_spec?: { action_class?: string; argv_template?: { kind: string; value?: string | null; name?: string | null }[] } | null;
}

export interface ExperimentMember {
  execution_plan_id?: string;
  job_id?: number | null;
  plan_digest?: string;
  server_name?: string;
  parameter_values?: Record<string, unknown>;
  canonical_job_status?: string | null;
  collection_state?: string;
  metrics_status?: string;
  metrics_summary?: { status?: string; reason?: string | null; collected_at?: string | null } | null;
}

export interface ExperimentItem {
  experiment_id: number;
  project_id: string;
  approval_id: number;
  status: string;
  run_count: number;
  matrix?: { axes?: { name: string; values: unknown[] }[] };
  guard?: { total_runs?: number; target_servers?: string[] };
  members?: ExperimentMember[];
}

export interface ProjectVersion {
  id: string;
  project_id: string;
  project_name?: string;
  git_commit: string;
  git_ref?: string | null;
  created_at: string;
  promotion_state?: string | null;
}

export interface AgentRunner {
  id: string;
  server: string;
  label?: string | null;
  status: string;
  active: boolean;
  connected: boolean;
  card?: Record<string, unknown> | null;
}

export interface Approval {
  id: number;
  kind: string;
  /** Chinese card title from the backend presentation map (整頓 U2). */
  title?: string;
  /** One-line payload summary from the backend; may be empty. */
  summary?: string;
  payload?: Record<string, unknown>;
  project_id?: string | null;
  status: string;
  created_at: string;
  decided_at?: string | null;
  note?: string | null;
  requester_actor_id?: string | null;
  requester_is_self?: boolean;
  can_decide?: boolean;
  decision_reason?: string | null;
  decision_actor_id?: string | null;
  decision_mechanism?: string | null;
  payload_digest?: string | null;
  payload_verified?: boolean;
  review?: Record<string, unknown> | null;
}

export type ProductRunState =
  | "awaiting_approval"
  | "rejected"
  | "queued"
  | "preparing"
  | "running"
  | "stopping"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "blocked"
  | "needs_attention";

export interface ProductRunDetail {
  plan_id: string;
  project_id: string;
  project_name: string;
  state: ProductRunState;
  metrics_status: string;
  canonical_job_status?: string | null;
  current_attempt?: { id: string; state: string; liveness: string } | null;
  attention_reasons: string[];
  job?: {
    id: number;
    status: string;
    created_at: string;
    started_at?: string | null;
    finished_at?: string | null;
    exit_code?: number | null;
    stalled_suspect: boolean;
  } | null;
  terminal_result?: {
    canonical_job_status: string;
    exit_code?: number | null;
    finished_at?: string | null;
    collection_state: string;
  } | null;
  timeline: { items: Array<Record<string, unknown>>; truncated: boolean };
}

export interface RunMetrics {
  job_id: number;
  collection_status: string;
  reason?: string | null;
  collected_at?: string | null;
  metrics: { key: string; value_type: string; value_text: string; recorded_at?: string }[];
}

export interface ProductRunArtifacts {
  availability: string;
  metadata_only: true;
  complete: false;
  truncated?: boolean;
  scope_truncated?: boolean;
  items: { relative_path: string; kind: string; size_bytes: number; sha256: string; reported_at: string; metadata_only: true }[];
}

/** One row of `GET /api/v2/engineering-tasks` (the checkpoint→promote bridge
 *  writes a `done` task whose `detected_metadata.source` is
 *  `agent_session_checkpoint`). Only the fields the Studio reads. */
export interface EngineeringTaskRow {
  id: string;
  project?: string | null;
  status: string;
  approval_id?: number | null;
  project_version_id?: string | null;
  detected_metadata?: Record<string, unknown> | null;
  presentation?: { promote?: { enabled?: boolean; reason?: string | null } } | null;
}

export interface SessionSummary {
  id: string;
  project_id: string;
  provider_id: string;
  workspace_branch?: string | null;
  base_version_id?: string | null;
  status: string;
  created_at: string;
  last_used_at?: string | null;
  closed_at?: string | null;
}

export interface PermissionRequest {
  request_id: string;
  session_id?: string;
  tool_name: string;
  tool_input?: Record<string, unknown>;
  summary?: string;
  reason?: string;
  allow_pattern?: string | null;
  status?: string;
  created_at?: string;
  expires_at?: string;
}

export type PermissionMode = "default" | "acceptEdits" | "plan";
export type EffortLevel = "low" | "medium" | "high" | "xhigh" | "max";
export type ThinkingOption = "adaptive" | "disabled" | { budget_tokens: number };

export interface SessionOptions {
  model?: string;
  effort?: EffortLevel;
  thinking?: ThinkingOption;
  permission_mode?: PermissionMode;
}

export interface StudioSession extends SessionSummary {
  runtime: {
    runner_id: string | null;
    runner_connected: boolean;
    task_state: string | null;
    sdk_session_id: string | null;
    cost_usd: number | null;
    last_seq: number;
    options?: SessionOptions;
  };
  pending_permissions: PermissionRequest[];
}

export interface SessionEvent {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface DiffResult {
  session_id: string;
  ok?: boolean;
  unreachable?: boolean;
  patch: string;
  status: unknown[];
  truncated?: boolean;
}
