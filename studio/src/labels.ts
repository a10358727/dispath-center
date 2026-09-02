/** User-facing nouns and one-line descriptions (整頓 U8): the Studio never
 *  shows a UUID slice, a digest, an env-var name or a raw identifier as the
 *  primary label. Internal names stay in tooltips/expanded JSON only. */

import type { ProjectVersion } from "@/api/types";
import { formatTime, shortCommit } from "@/lib";

export const NOUNS = {
  environment: "環境",
  template: "執行模板",
  target: "執行機器",
  version: "版本",
  defaults: "預設參數",
  checkpoint: "存檔",
  promote: "晉升",
  session: "Session",
  runner: "Agent runner",
} as const;

export const PROMOTION_STATES: Record<string, string> = {
  promoted: "已晉升",
  legacy_observed: "觀察到",
  pending: "晉升中",
  retired: "已退役",
};

export function promotionStateLabel(state: string | null | undefined): string {
  if (!state) return "觀察到";
  return PROMOTION_STATES[state] ?? state;
}

/** `#3 · abcdef12 · 已晉升 · 2026-09-02 10:00` — index counts from the newest. */
export function versionLabel(version: Pick<ProjectVersion, "git_commit" | "promotion_state" | "created_at"> & { git_ref?: string | null }, index?: number): string {
  const parts = [
    index != null ? `#${index + 1}` : null,
    version.git_ref ? `${version.git_ref}@${shortCommit(version.git_commit)}` : shortCommit(version.git_commit),
    promotionStateLabel(version.promotion_state),
    version.created_at ? formatTime(version.created_at) : null,
  ];
  return parts.filter(Boolean).join(" · ");
}

const AUDIT_ACTIONS: Record<string, string> = {
  enqueue: "排入任務",
  dispatch: "派工",
  done: "任務完成",
  failed: "任務失敗",
  requeue: "任務重排",
  cancel: "任務取消",
  stop: "停止任務",
  approve: "核准",
  reject: "退回",
  approval_requested: "建立核准卡",
  approval_decided: "核准卡已決定",
  auto_approve: "規則自動核准",
  import_project: "匯入專案",
  inventory_scan: "掃描機器",
  hub_sync: "登記版本",
  engineering_task_promote: "晉升為正式版本",
  agent_session_checkpoint: "存檔工作區變更",
  agent_session_open: "開啟 Agent session",
  server_add: "新增機器設定",
  server_update: "更新機器設定",
  server_disable: "停用機器設定",
  server_delete: "刪除機器設定",
  metrics_collected: "收到 metrics",
  results_collected: "回收結果",
};

interface AuditLike {
  action?: string | null;
  result?: string | null;
  params?: Record<string, unknown> | null;
  actor?: { kind?: string | null; display_name?: string | null; id?: string | null } | null;
}

function scalar(value: unknown, limit = 60): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    const text = String(value).replace(/(?<![\w.])\/[^\s'"]+/g, "…");
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  }
  return "";
}

/** One human line per audit record: `排入任務 · demo · 任務 #94 · server-a`. */
export function describeAudit(record: AuditLike): string {
  const params = record.params ?? {};
  const action = record.action ?? "";
  const title = AUDIT_ACTIONS[action] ?? (params.kind && typeof params.kind === "string" ? AUDIT_ACTIONS[params.kind] ?? action : action);
  const bits = [
    title,
    scalar(params.project ?? params.project_name),
    params.job_id != null ? `任務 #${scalar(params.job_id)}` : "",
    params.approval_id != null ? `卡 #${scalar(params.approval_id)}` : "",
    scalar(params.server ?? params.pin_server ?? params.server_name),
    scalar(params.command, 50),
  ].filter(Boolean);
  return bits.join(" · ");
}

export function actorLabel(actor: AuditLike["actor"]): string {
  if (!actor) return "系統";
  if (actor.display_name) return actor.display_name;
  if (actor.kind === "human") return "人";
  if (actor.kind) return actor.kind;
  return "—";
}
