import type { SessionEvent } from "@/api/types";

export type ContextUsage = {
  currentTokens: number;
  contextWindow: number;
  percent: number;
  label: "Provider-reported" | "Estimated";
  categories: { name: string; tokens: number }[];
};

function finiteNonnegative(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

/** Context occupancy is usable only when the runtime supplies both sides of
 * the ratio. Opaque categories are details, never a substitute for a total. */
export function latestContextUsage(events: SessionEvent[], invalidAfterSeq = -1): ContextUsage | null {
  let latestModelChange = invalidAfterSeq;
  let latestContext: SessionEvent | undefined;
  for (const event of events) {
    if (event.kind === "config" && typeof event.payload.model === "string") latestModelChange = event.seq;
    if (event.kind === "context") latestContext = event;
  }
  if (!latestContext || latestContext.seq <= latestModelChange) return null;
  const usage = latestContext.payload.usage;
  if (!usage || typeof usage !== "object" || Array.isArray(usage)) return null;
  const record = usage as Record<string, unknown>;
  if (!finiteNonnegative(record.total_tokens) || !finiteNonnegative(record.context_window) || record.context_window <= 0) return null;
  const categories = Array.isArray(record.categories)
    ? record.categories.flatMap((item) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) return [];
        const category = item as Record<string, unknown>;
        const name = typeof category.name === "string" ? category.name : typeof category.category === "string" ? category.category : null;
        return name && finiteNonnegative(category.tokens) ? [{ name, tokens: category.tokens }] : [];
      })
    : [];
  return {
    currentTokens: record.total_tokens,
    contextWindow: record.context_window,
    percent: (record.total_tokens / record.context_window) * 100,
    label: record.estimated === true || record.source === "estimated" ? "Estimated" : "Provider-reported",
    categories,
  };
}
