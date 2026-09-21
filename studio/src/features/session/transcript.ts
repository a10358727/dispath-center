import type { SessionEvent } from "@/api/types";

export type TranscriptItem =
  | { type: "status"; seq: number; state: string; detail: string; at: string }
  | { type: "user"; seq: number; text: string; attachments: { media_type: string; bytes: number }[]; at: string }
  | { type: "assistant"; seq: number; text: string; streaming: boolean; at: string }
  | {
      type: "tool";
      seq: number;
      toolUseId: string | null;
      name: string;
      input: unknown;
      result: { isError: boolean; content: string } | null;
      at: string;
    }
  | {
      type: "permission";
      seq: number;
      requestId: string;
      toolName: string;
      summary: string;
      reason: string;
      allowPattern: string | null;
      toolInput: unknown;
      expiresAt: string | null;
      decision: { decision: string; allowPattern: string | null } | null;
      at: string;
    }
  | { type: "result"; seq: number; isError: boolean; costUsd: number | null; text: string; at: string }
  | { type: "system"; seq: number; subtype: string; at: string }
  | { type: "error"; seq: number; message: string; at: string };

export const RUN_ANALYSIS_MARKER = "[dispatch:analyze-run:";
export const RUN_CONTINUE_MARKER = "[dispatch:continue-run:";
const RUN_EVIDENCE_TOOLS = new Set(["get_run", "get_run_metrics", "get_run_artifacts", "get_run_log_tail", "compare_runs"]);

export type RunAnalysis = {
  planId: string;
  requested: boolean;
  completed: boolean;
  evidence: { name: string; toolUseId: string | null; availability: string | null }[];
  text: string | null;
};

function record(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function markerPlanId(text: string, marker: string): string | null {
  const start = text.indexOf(marker);
  if (start < 0) return null;
  const value = text.slice(start + marker.length, start + marker.length + 36);
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value) && text[start + marker.length + 36] === "]" ? value : null;
}

function evidenceToolName(name: string): string | null {
  const normalized = name.startsWith("mcp__dispatch__") ? name.slice("mcp__dispatch__".length) : name;
  return RUN_EVIDENCE_TOOLS.has(normalized) ? normalized : null;
}

function referencesPlan(payload: Record<string, unknown>, planId: string): boolean {
  return payload.plan_id === planId || payload.left_plan_id === planId || payload.right_plan_id === planId;
}

/** Rebuilds the result-analysis workflow exclusively from persisted transcript events. */
export function runAnalysisFor(items: TranscriptItem[], planId: string): RunAnalysis {
  const request = [...items].reverse().find((item): item is Extract<TranscriptItem, { type: "user" }> => item.type === "user" && markerPlanId(item.text, RUN_ANALYSIS_MARKER) === planId);
  if (!request) return { planId, requested: false, completed: false, evidence: [], text: null };
  const turnResult = items.find((item): item is Extract<TranscriptItem, { type: "result" }> => item.type === "result" && item.seq > request.seq);
  const turnEndSeq = turnResult?.seq ?? Number.POSITIVE_INFINITY;
  const evidence = items.flatMap((item) => {
    if (item.seq <= request.seq || item.seq >= turnEndSeq || item.type !== "tool" || !item.result || item.result.isError) return [];
    const name = evidenceToolName(item.name);
    if (!name) return [];
    try {
      const payload = record(JSON.parse(item.result.content));
      if (!payload || !referencesPlan(payload, planId)) return [];
      return [{ name, toolUseId: item.toolUseId, availability: typeof payload.availability === "string" ? payload.availability : null }];
    } catch {
      return [];
    }
  });
  const lastEvidenceSeq = Math.max(request.seq, ...items.filter((item) => item.type === "tool" && evidence.some((ref) => ref.toolUseId != null && ref.toolUseId === item.toolUseId)).map((item) => item.seq));
  const assistant = items.find((item): item is Extract<TranscriptItem, { type: "assistant" }> => item.type === "assistant" && !item.streaming && item.seq > lastEvidenceSeq && item.seq < turnEndSeq);
  const completed = evidence.length > 0 && assistant != null && turnResult != null && !turnResult.isError;
  return { planId, requested: turnResult == null || completed, completed, evidence, text: completed && assistant ? assistant.text : null };
}

export function analyzedRunIds(items: TranscriptItem[]): string[] {
  return [...new Set(items.flatMap((item) => item.type === "user" ? [markerPlanId(item.text, RUN_ANALYSIS_MARKER)].filter((value): value is string => value != null) : []))];
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : value == null ? fallback : String(value);
}

/** Pure projection of the persisted event log into renderable items.
 *  Streaming `text_delta`s are merged into one live bubble that is replaced
 *  by the final `assistant_text`; tool results and permission decisions are
 *  folded into the item they answer. */
export function buildTranscript(events: SessionEvent[]): TranscriptItem[] {
  const items: TranscriptItem[] = [];
  const toolIndex = new Map<string, number>();
  const permissionIndex = new Map<string, number>();
  let live: { seq: number; text: string; at: string } | null = null;
  const flushLive = (streaming: boolean) => {
    if (live && live.text) items.push({ type: "assistant", seq: live.seq, text: live.text, streaming, at: live.at });
    live = null;
  };
  for (const event of events) {
    const p = event.payload ?? {};
    const at = event.created_at;
    switch (event.kind) {
      case "text_delta":
        if (!live) live = { seq: event.seq, text: "", at };
        live.text += str(p.text);
        break;
      case "assistant_text":
        live = null;
        items.push({ type: "assistant", seq: event.seq, text: str(p.text), streaming: false, at });
        break;
      case "user_text":
        flushLive(false);
        items.push({
          type: "user",
          seq: event.seq,
          text: str(p.text),
          attachments: Array.isArray(p.attachments)
            ? (p.attachments as Record<string, unknown>[]).map((a) => ({ media_type: str(a.media_type), bytes: typeof a.bytes === "number" ? a.bytes : 0 }))
            : [],
          at,
        });
        break;
      case "tool_use": {
        flushLive(false);
        const toolUseId = p.tool_use_id == null ? null : str(p.tool_use_id);
        items.push({ type: "tool", seq: event.seq, toolUseId, name: str(p.name), input: p.input, result: null, at });
        if (toolUseId) toolIndex.set(toolUseId, items.length - 1);
        break;
      }
      case "tool_result": {
        const index = p.tool_use_id == null ? undefined : toolIndex.get(str(p.tool_use_id));
        const result = { isError: Boolean(p.is_error), content: str(p.content) };
        if (index !== undefined) {
          const item = items[index];
          if (item.type === "tool") item.result = result;
        } else {
          items.push({ type: "tool", seq: event.seq, toolUseId: null, name: "(result)", input: null, result, at });
        }
        break;
      }
      case "permission": {
        flushLive(false);
        const requestId = str(p.request_id);
        items.push({
          type: "permission",
          seq: event.seq,
          requestId,
          toolName: str(p.tool_name, "?"),
          summary: str(p.summary),
          reason: str(p.reason),
          allowPattern: p.allow_pattern == null ? null : str(p.allow_pattern),
          toolInput: p.tool_input,
          expiresAt: p.expires_at == null ? null : str(p.expires_at),
          decision: null,
          at,
        });
        permissionIndex.set(requestId, items.length - 1);
        break;
      }
      case "permission_decision": {
        const index = permissionIndex.get(str(p.request_id));
        if (index !== undefined) {
          const item = items[index];
          if (item.type === "permission")
            item.decision = { decision: str(p.decision), allowPattern: p.allow_pattern == null ? null : str(p.allow_pattern) };
        }
        break;
      }
      case "result":
        flushLive(false);
        items.push({
          type: "result",
          seq: event.seq,
          isError: Boolean(p.is_error),
          costUsd: typeof p.total_cost_usd === "number" ? p.total_cost_usd : null,
          text: str(p.text),
          at,
        });
        break;
      case "status":
        items.push({ type: "status", seq: event.seq, state: str(p.state, "unknown"), detail: str(p.detail), at });
        break;
      case "system":
        items.push({ type: "system", seq: event.seq, subtype: str(p.subtype), at });
        break;
      case "config": {
        const parts = [p.model ? `模型 ${str(p.model)}` : "", p.permission_mode ? `權限模式 ${str(p.permission_mode)}` : ""].filter(Boolean);
        items.push({ type: "status", seq: event.seq, state: "config", detail: `設定變更：${parts.join("，")}`, at });
        break;
      }
      case "error":
        flushLive(false);
        items.push({ type: "error", seq: event.seq, message: str(p.message ?? p.text ?? p.detail, "error"), at });
        break;
      default:
        break;
    }
  }
  flushLive(true);
  return items;
}
