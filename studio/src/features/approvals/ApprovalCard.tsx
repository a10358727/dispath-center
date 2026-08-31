import { useState } from "react";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useDecideApproval, useDecideApprovalV2 } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { formatTime } from "@/lib";

const KIND_LABELS: Record<string, string> = {
  agent_session_open: "開啟 Agent session",
  agent_runner_enroll: "登錄 runner agent",
  agent_runner_revoke: "撤銷 runner agent",
  enqueue: "排入任務",
  experiment_v2: "建立實驗",
  code_promotion: "晉升程式版本",
};

function findKey(value: unknown, key: string): unknown {
  if (!value || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  if (key in record) return record[key];
  for (const nested of Object.values(record)) {
    const found = findKey(nested, key);
    if (found !== undefined) return found;
  }
  return undefined;
}

/** One approval card, decidable where it appears. The decision is made by the
 *  signed-in person through the reviewed approval routes -- never by an agent. */
export function ApprovalCard({
  approval,
  onDecided,
  decideVia = "legacy",
}: {
  approval: Approval;
  onDecided?: (result: Record<string, unknown>) => void;
  /** `experiment_create_v2` (and other v2-only kinds) must go through the
   *  generic `/api/v2/approvals/{id}/decisions` route. */
  decideVia?: "legacy" | "v2";
}) {
  const legacyDecide = useDecideApproval();
  const v2Decide = useDecideApprovalV2();
  const decide = decideVia === "v2" ? v2Decide : legacyDecide;
  const [note, setNote] = useState("");
  const [oneTimeSecret, setOneTimeSecret] = useState<string | null>(null);
  const [showPayload, setShowPayload] = useState(false);
  const pending = approval.status === "pending";
  const run = async (decision: "approve" | "reject") => {
    const result = await decide.mutateAsync({ id: approval.id, decision, note });
    const secret = findKey(result, "agent_runner_token");
    if (typeof secret === "string") setOneTimeSecret(secret);
    onDecided?.(result);
  };
  return (
    <Card className="space-y-2" data-testid={`approval-${approval.id}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-medium">{KIND_LABELS[approval.kind] ?? approval.kind}</span>
          <Badge tone={pending ? "warn" : stateTone(approval.status === "approved" ? "ok" : "failed")}>{approval.status}</Badge>
        </div>
        <span className="text-xs text-slate-500">
          #{approval.id} · {formatTime(approval.created_at)}
        </span>
      </div>
      <div className="text-xs text-slate-600">
        {Object.entries(approval.payload ?? {})
          .flatMap(([key, value]) =>
            key === "options" && value && typeof value === "object"
              ? Object.entries(value as Record<string, unknown>).map(([k, v]) => [`options.${k}`, typeof v === "object" ? JSON.stringify(v) : v] as [string, unknown])
              : [[key, value] as [string, unknown]],
          )
          .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
          .slice(0, 8)
          .map(([key, value]) => (
            <span key={key} className="mr-3">
              <span className="text-slate-400">{key}=</span>
              {String(value)}
            </span>
          ))}
        <button type="button" className="text-sky-700 underline" onClick={() => setShowPayload((v) => !v)}>
          {showPayload ? "收起" : "完整內容"}
        </button>
      </div>
      {showPayload ? <pre className="max-h-64 overflow-auto rounded bg-slate-50 p-2 text-xs">{JSON.stringify(approval.payload, null, 2)}</pre> : null}
      {pending ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" disabled={decide.isPending} onClick={() => void run("approve")}>
            核准
          </Button>
          <input
            className="w-48 rounded border border-slate-300 px-2 py-1 text-sm"
            placeholder="退回原因（選填）"
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          <Button variant="danger" disabled={decide.isPending} onClick={() => void run("reject")}>
            退回
          </Button>
          {decide.error ? <span className="text-xs text-rose-700">{(decide.error as Error).message}</span> : null}
        </div>
      ) : null}
      {oneTimeSecret ? (
        <div className="rounded border border-amber-300 bg-amber-50 p-2 text-xs">
          <div className="font-semibold text-amber-900">runner 登錄憑證（只顯示這一次，存到 runner 的 credential 檔）</div>
          <code className="break-all">{oneTimeSecret}</code>
        </div>
      ) : null}
    </Card>
  );
}
