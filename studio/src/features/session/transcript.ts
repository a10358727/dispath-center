import type { SessionEvent } from "@/api/types";

export type TranscriptItem =
  | { type: "status"; seq: number; state: string; detail: string; at: string }
  | { type: "user"; seq: number; text: string; at: string }
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
        items.push({ type: "user", seq: event.seq, text: str(p.text), at });
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
