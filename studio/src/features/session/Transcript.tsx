import { useEffect, useRef, useState } from "react";
import { Badge, stateTone } from "@/components/ui/badge";
import { PermissionCard } from "./PermissionCard";
import type { TranscriptItem } from "./transcript";

function ToolCard({ item }: { item: Extract<TranscriptItem, { type: "tool" }> }) {
  const [open, setOpen] = useState(false);
  const input = item.input && typeof item.input === "object" ? (item.input as Record<string, unknown>) : {};
  const headline = typeof input.command === "string" ? input.command : typeof input.file_path === "string" ? input.file_path : "";
  return (
    <div className="my-1 rounded-md border border-slate-200 bg-white text-sm">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-1.5 text-left" onClick={() => setOpen((v) => !v)}>
        <Badge tone={item.result ? (item.result.isError ? "bad" : "ok") : "info"}>{item.name}</Badge>
        <span className="truncate font-mono text-xs text-slate-600">{headline}</span>
        <span className="ml-auto text-xs text-slate-400">{open ? "收起" : "展開"}</span>
      </button>
      {open ? (
        <div className="space-y-1 border-t border-slate-100 p-2">
          <pre className="max-h-48 overflow-auto rounded bg-slate-50 p-2 text-xs">{JSON.stringify(item.input, null, 2)}</pre>
          {item.result ? <pre className="max-h-64 overflow-auto rounded bg-slate-50 p-2 text-xs">{item.result.content}</pre> : <span className="text-xs text-slate-400">執行中…</span>}
        </div>
      ) : null}
    </div>
  );
}

export function Transcript({
  items,
  deciding,
  onDecide,
}: {
  items: TranscriptItem[];
  deciding: boolean;
  onDecide: (requestId: string, decision: "allow" | "deny", allowPattern?: string) => void;
}) {
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [items.length, items[items.length - 1]?.type === "assistant" ? (items[items.length - 1] as { text: string }).text.length : 0]);
  return (
    <div className="flex-1 space-y-1 overflow-y-auto px-4 py-3" data-testid="transcript">
      {items.length === 0 ? <div className="py-10 text-center text-sm text-slate-400">還沒有對話。啟動 session 後在下方輸入指示。</div> : null}
      {items.map((item) => {
        switch (item.type) {
          case "user":
            return (
              <div key={item.seq} className="ml-auto max-w-3xl whitespace-pre-wrap rounded-lg bg-sky-600 px-3 py-2 text-sm text-white">
                {item.text}
                {item.attachments.length > 0 ? (
                  <div className="mt-1 text-xs text-sky-100">
                    {item.attachments.map((a, i) => (
                      <span key={i} className="mr-2">🖼 {a.media_type}（{Math.round(a.bytes / 1024)} KB）</span>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          case "assistant":
            return (
              <div key={item.seq} className="max-w-3xl whitespace-pre-wrap rounded-lg bg-white px-3 py-2 text-sm shadow-sm">
                {item.text}
                {item.streaming ? <span className="ml-1 animate-pulse text-slate-400">▍</span> : null}
              </div>
            );
          case "tool":
            return <ToolCard key={item.seq} item={item} />;
          case "permission":
            return <PermissionCard key={item.seq} item={item} busy={deciding} onDecide={(decision, pattern) => onDecide(item.requestId, decision, pattern)} />;
          case "result":
            return (
              <div key={item.seq} className="flex items-center gap-2 py-1 text-xs text-slate-500">
                <Badge tone={item.isError ? "bad" : "ok"}>{item.isError ? "回合錯誤" : "回合完成"}</Badge>
                {item.costUsd != null ? <span>累計 ${item.costUsd.toFixed(4)}</span> : null}
              </div>
            );
          case "status":
            return (
              <div key={item.seq} className="flex items-center gap-2 py-1 text-xs text-slate-500">
                <Badge tone={stateTone(item.state)}>{item.state}</Badge>
                <span>{item.detail}</span>
              </div>
            );
          case "error":
            return (
              <div key={item.seq} className="rounded bg-rose-50 px-3 py-1 text-xs text-rose-800">
                {item.message}
              </div>
            );
          default:
            return null;
        }
      })}
      <div ref={bottom} />
    </div>
  );
}
