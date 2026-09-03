import { useEffect, useState } from "react";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api } from "@/api/client";
import { useDecideApproval, useDecideApprovalV2 } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { formatTime } from "@/lib";
import { canConfirmImmediately } from "./singleOperator";

/** Human-readable reasons the current person cannot decide a card. */
const DECISION_REASONS: Record<string, string> = {
  denied_high_risk_self_decision: "這張卡是你自己建立的高風險請求，目前姿態不允許自核",
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
//: kinds whose approval reveals a one-time secret — the generic v2 route
//: refuses them by design; only the legacy decision path returns the secret.
const ONE_TIME_SECRET_KINDS = new Set(["service_token_issue", "node_enroll", "node_rotate", "agent_runner_enroll"]);

export function ApprovalCard({
  approval,
  onDecided,
  decideVia = "auto",
  confirmImmediately = false,
}: {
  approval: Approval;
  onDecided?: (result: Record<string, unknown>) => void;
  /** auto: one-time-secret kinds use the legacy path (the secret is in its
   *  response); everything else goes through the generic v2 decisions route
   *  with the payload digest. */
  decideVia?: "auto" | "legacy" | "v2";
  /** DG-SINGLE-OPERATOR-CONFIRM v1: the person chose 「確認並執行」 on the
   *  preview, so the freshly created card is decided right away — only for
   *  kinds in the closed list, only while pending and decidable. */
  confirmImmediately?: boolean;
}) {
  const legacyDecide = useDecideApproval();
  const v2Decide = useDecideApprovalV2();
  const resolved = decideVia === "auto" ? (ONE_TIME_SECRET_KINDS.has(approval.kind) ? "legacy" : "v2") : decideVia;
  const decide = resolved === "v2" ? v2Decide : legacyDecide;
  const [note, setNote] = useState("");
  //: DG-HARDWARE-EXECUTION v1 H-6 (a): a hil_test decider may ask that a
  //: verified receipt mark the tested image known-good. Server-enforced.
  const [markKnownGood, setMarkKnownGood] = useState(false);
  const [oneTimeSecret, setOneTimeSecret] = useState<string | null>(null);
  const [showPayload, setShowPayload] = useState(false);
  //: The v2 list is payload-free by ruling (opaque list, authorized detail);
  //: 完整內容 fetches the detail on demand when the card came from the list.
  const [fetchedPayload, setFetchedPayload] = useState<Record<string, unknown> | null>(null);
  const payload = approval.payload ?? fetchedPayload ?? undefined;
  const togglePayload = async () => {
    if (!showPayload && !approval.payload && !fetchedPayload) {
      try {
        const detail = await api<Approval>(`/api/v2/approvals/${approval.id}`);
        setFetchedPayload(detail.payload ?? {});
      } catch (error) {
        setFetchedPayload({ error: (error as Error).message });
      }
    }
    setShowPayload((v) => !v);
  };
  const pending = approval.status === "pending";
  //: The title comes from the backend presentation map (app/approval_presentation.py);
  //: the raw kind is only the last-resort fallback for stale cached rows.
  const title = approval.title ?? approval.kind;
  const undecidable = approval.can_decide === false;
  const undecidableReason = approval.decision_reason
    ? DECISION_REASONS[approval.decision_reason] ?? `無法決定（${approval.decision_reason}）`
    : "目前的身分無法決定這張卡";
  const run = async (decision: "approve" | "reject") => {
    const result = await decide.mutateAsync({ id: approval.id, decision, note, ...(resolved === "v2" && markKnownGood ? { markKnownGood: true } : {}) } as { id: number; decision: "approve" | "reject"; note?: string; markKnownGood?: boolean });
    const secret = findKey(result, "agent_runner_token");
    if (typeof secret === "string") setOneTimeSecret(secret);
    onDecided?.(result);
  };
  const [confirmed, setConfirmed] = useState(false);
  const autoConfirm = confirmImmediately && pending && !undecidable && canConfirmImmediately(approval.kind);
  useEffect(() => {
    if (autoConfirm && !confirmed && !decide.isPending) {
      setConfirmed(true);
      void run("approve");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoConfirm]);
  if (autoConfirm) {
    return (
      <Card className="space-y-1 text-sm" data-testid={`approval-${approval.id}`}>
        <div className="flex items-center gap-2">
          <span className="font-medium">{title}</span>
          <Badge tone="info">確認並執行</Badge>
          <span className="text-xs text-slate-500">卡 #{approval.id}</span>
        </div>
        {approval.summary ? <div className="text-slate-700">{approval.summary}</div> : null}
        {decide.error ? <div className="text-xs text-rose-700">{(decide.error as Error).message}</div> : <div className="text-xs text-slate-500">已由你本人立即核准，完整留稽核。</div>}
      </Card>
    );
  }
  return (
    <Card className="space-y-2" data-testid={`approval-${approval.id}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-medium">{title}</span>
          <Badge tone={pending ? "warn" : stateTone(approval.status === "approved" ? "ok" : "failed")}>{approval.status}</Badge>
        </div>
        <span className="text-xs text-slate-500">
          #{approval.id} · {formatTime(approval.created_at)}
        </span>
      </div>
      {approval.summary ? <div className="text-sm text-slate-700">{approval.summary}</div> : null}
      {approval.note ? <div className="text-xs text-slate-500">備註：{approval.note}</div> : null}
      {!pending && approval.decision_mechanism ? (
        <div className="text-xs text-slate-500">決定：{approval.decision_actor_id ?? "—"} · {approval.decision_mechanism}</div>
      ) : null}
      <div className="text-xs text-slate-600">
        {Object.entries(payload ?? {})
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
        <button type="button" className="text-sky-700 underline" onClick={() => void togglePayload()}>
          {showPayload ? "收起" : "完整內容"}
        </button>
      </div>
      {showPayload ? <pre className="max-h-64 overflow-auto rounded bg-slate-50 p-2 text-xs">{JSON.stringify(payload ?? {}, null, 2)}</pre> : null}
      {pending ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" disabled={decide.isPending || undecidable} onClick={() => void run("approve")}>
            核准
          </Button>
          <input
            className="w-48 rounded border border-slate-300 px-2 py-1 text-sm"
            placeholder="退回原因（選填）"
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          <Button variant="danger" disabled={decide.isPending || undecidable} onClick={() => void run("reject")}>
            退回
          </Button>
          {approval.kind === "hardware_action_v2" && (payload as { action_class?: string } | undefined)?.action_class === "hil_test" ? (
            <label className="flex items-center gap-1 text-xs text-slate-600">
              <input type="checkbox" checked={markKnownGood} onChange={(event) => setMarkKnownGood(event.target.checked)} />
              測試通過（收據 verified）後把映像標記為 known-good
            </label>
          ) : null}
          {undecidable ? <span className="text-xs text-amber-700">{undecidableReason}</span> : null}
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
