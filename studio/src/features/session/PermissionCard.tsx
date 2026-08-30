import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { TranscriptItem } from "./transcript";

type Item = Extract<TranscriptItem, { type: "permission" }>;

/** INV-AGENT-2: a workspace permission prompt is decided inline by the session
 *  owner. It is not an approval card and can never decide one. */
export function PermissionCard({
  item,
  busy,
  onDecide,
}: {
  item: Item;
  busy: boolean;
  onDecide: (decision: "allow" | "deny", allowPattern?: string) => void;
}) {
  const command = item.toolInput && typeof item.toolInput === "object" ? (item.toolInput as Record<string, unknown>).command : undefined;
  const expired = item.expiresAt ? new Date(item.expiresAt).getTime() < Date.now() : false;
  return (
    <div className="my-2 rounded-lg border border-amber-300 bg-amber-50 p-3" data-testid={`permission-${item.requestId}`}>
      <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-amber-900">
        <span>權限提示 · {item.toolName}</span>
        {item.decision ? <Badge tone={item.decision.decision === "allow" ? "ok" : "bad"}>{item.decision.decision === "allow" ? "已允許" : "已拒絕"}</Badge> : null}
        {!item.decision && expired ? <Badge tone="neutral">已逾時（視為拒絕）</Badge> : null}
      </div>
      {item.summary ? <div className="text-sm text-slate-800">{item.summary}</div> : null}
      {typeof command === "string" ? <pre className="mt-1 overflow-auto rounded bg-white p-2 text-xs">{command}</pre> : null}
      {item.reason ? <div className="mt-1 text-xs text-slate-600">{item.reason}</div> : null}
      {!item.decision && !expired ? (
        <div className="mt-2 flex flex-wrap gap-2">
          <Button variant="primary" disabled={busy} onClick={() => onDecide("allow")}>
            允許這一次
          </Button>
          {item.allowPattern ? (
            <Button variant="secondary" disabled={busy} title={item.allowPattern} onClick={() => onDecide("allow", item.allowPattern ?? undefined)}>
              本 session 一律允許 <code className="ml-1 text-xs">{item.allowPattern}</code>
            </Button>
          ) : null}
          <Button variant="danger" disabled={busy} onClick={() => onDecide("deny")}>
            拒絕
          </Button>
        </div>
      ) : null}
    </div>
  );
}
