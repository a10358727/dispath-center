import { Link } from "react-router-dom";
import { useApprovalDetail } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { Badge } from "@/components/ui/badge";
import { Card, CardTitle } from "@/components/ui/card";
import type { TranscriptItem } from "./transcript";
import { RunMonitorCard } from "./RunMonitorCard";

type ToolItem = Extract<TranscriptItem, { type: "tool" }>;

type RunReference =
  | { state: "loading" }
  | { state: "tool-error"; message: string }
  | { state: "malformed" }
  | { state: "ready"; approvalId: number };

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

/** A request_run result is only a pointer to platform state. Never infer a
 * proposal from assistant prose or from the tool input. */
export function runReference(item: ToolItem): RunReference {
  if (!item.result) return { state: "loading" };
  if (item.result.isError) return { state: "tool-error", message: item.result.content };
  try {
    const parsed = record(JSON.parse(item.result.content));
    const approvalId = parsed?.approval_id;
    return typeof approvalId === "number" && Number.isSafeInteger(approvalId) && approvalId > 0
      ? { state: "ready", approvalId }
      : { state: "malformed" };
  } catch {
    return { state: "malformed" };
  }
}

function short(value: string): string {
  return value.length > 12 ? value.slice(0, 12) : value;
}

function scalarEntries(value: unknown): [string, string][] {
  const source = record(value);
  if (!source) return [];
  return Object.entries(source)
    .filter((entry): entry is [string, string | number | boolean] => ["string", "number", "boolean"].includes(typeof entry[1]))
    .map(([key, item]) => [key, String(item)]);
}

function verifiedContract(approval: Approval | undefined): Record<string, unknown> | null {
  if (!approval || approval.kind !== "execution_plan_v2" || approval.payload_verified !== true) return null;
  const review = record(approval.review);
  const contract = record(review?.contract);
  return contract?.contract_version === "execution-plan-v2" ? contract : null;
}

export function approvedRunPlanId(approval: Approval | undefined): string | null {
  if (!approval || approval.kind !== "execution_plan_v2" || approval.status !== "approved" || approval.payload_verified !== true) return null;
  const review = record(approval.review);
  const contract = record(review?.contract);
  if (contract?.contract_version !== "execution-plan-v2") return null;
  const candidate = contract?.execution_plan_id ?? review?.execution_plan_id;
  return typeof candidate === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(candidate) ? candidate : null;
}

function ToolDetails({ item }: { item: ToolItem }) {
  return (
    <details className="text-xs text-slate-500">
      <summary className="cursor-pointer text-sky-700">進階工具詳情<span className="ml-1 text-xs font-normal text-slate-400">Advanced tool details</span></summary>
      <div className="mt-2 space-y-1">
        <div>工具：<code>{item.name}</code>{item.toolUseId ? <> · 呼叫 <code>{item.toolUseId}</code></> : null}</div>
        <pre className="max-h-48 overflow-auto rounded bg-slate-50 p-2">{JSON.stringify(item.input, null, 2)}</pre>
        {item.result ? <pre className="max-h-48 overflow-auto rounded bg-slate-50 p-2">{item.result.content}</pre> : null}
      </div>
    </details>
  );
}

function RunDetail({ approval, contract }: { approval: Approval; contract: Record<string, unknown> }) {
  const version = record(contract.project_version);
  const target = record(contract.target);
  const parameters = scalarEntries(contract.parameter_values);
  const estimate = record(contract.grounded_estimate);
  const estimateLabel = typeof estimate?.label === "string" && typeof estimate?.source === "string" ? estimate.label : null;
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <CardTitle className="mb-0">{approval.title ?? "準備執行"}</CardTitle>
        <Badge tone={approval.status === "pending" || approval.status === "approved" ? "ok" : "bad"}>
          {approval.status === "pending" ? "已就緒" : approval.status}
        </Badge>
      </div>
      {approval.summary ? <p className="text-sm text-slate-700">{approval.summary}</p> : null}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-slate-500">版本</dt>
        <dd>{typeof version?.git_commit === "string" ? short(version.git_commit) : "不可用"} <span className="text-xs text-slate-500">已晉升</span></dd>
        <dt className="text-slate-500">運算資源</dt>
        <dd>{typeof target?.server_name === "string" ? target.server_name : "不可用"}</dd>
        <dt className="text-slate-500">就緒狀態</dt>
        <dd>提案時已就緒．執行時重新檢查</dd>
        {parameters.length > 0 ? <><dt className="text-slate-500">參數</dt><dd>{parameters.map(([key, value]) => `${key}=${value}`).join(" · ")}</dd></> : null}
        {estimateLabel ? <><dt className="text-slate-500">預估</dt><dd>{estimateLabel} <span className="text-xs text-slate-500">({String(estimate?.source)})</span></dd></> : null}
      </dl>
      <details className="text-xs text-slate-500">
        <summary className="cursor-pointer text-sky-700">進階合約詳情<span className="ml-1 text-xs font-normal text-slate-400">Advanced contract details</span></summary>
        <pre className="mt-2 max-h-64 overflow-auto rounded bg-slate-50 p-2">{JSON.stringify({ approval_id: approval.id, review: approval.review }, null, 2)}</pre>
      </details>
    </>
  );
}

export function ReadyToRunCard({ item, transcriptItems = [], analyzing = false, onAnalyzeRun = () => {} }: { item: ToolItem; transcriptItems?: TranscriptItem[]; analyzing?: boolean; onAnalyzeRun?: (planId: string) => void }) {
  const reference = runReference(item);
  const approvalId = reference.state === "ready" ? reference.approvalId : null;
  const detail = useApprovalDetail(approvalId);
  const contract = verifiedContract(detail.data);
  const planId = approvedRunPlanId(detail.data);
  const target = record(contract?.target);
  return (
    <Card className="my-2 space-y-3 border-sky-200" data-testid="ready-to-run-card">
      {reference.state === "loading" ? <><CardTitle>正在準備執行提案<span className="block text-xs font-normal text-slate-400">Preparing Run proposal</span></CardTitle><p className="text-sm text-slate-500" role="status">等待受管控的請求…</p></> : null}
      {reference.state === "tool-error" ? <><CardTitle>無法取得執行提案<span className="block text-xs font-normal text-slate-400">Run proposal unavailable</span></CardTitle><p className="text-sm text-rose-700">平台未建立執行核准。</p></> : null}
      {reference.state === "malformed" ? <><CardTitle>無法取得執行提案<span className="block text-xs font-normal text-slate-400">Run proposal unavailable</span></CardTitle><p className="text-sm text-amber-700">工具結果未包含有效的核准參照。</p></> : null}
      {approvalId != null && detail.isLoading ? <><CardTitle>載入執行提案中…<span className="block text-xs font-normal text-slate-400">Loading Run proposal</span></CardTitle><p className="text-sm text-slate-500" role="status">正在載入已驗證的核准合約…</p></> : null}
      {approvalId != null && detail.isError ? <><CardTitle>無法取得執行提案<span className="block text-xs font-normal text-slate-400">Run proposal unavailable</span></CardTitle><p className="text-sm text-rose-700">核准詳情不可用，或您無權檢視。</p></> : null}
      {detail.data && !contract ? <><CardTitle>無法取得執行提案<span className="block text-xs font-normal text-slate-400">Run proposal unavailable</span></CardTitle><p className="text-sm text-rose-700">平台無法提供已驗證的型別化執行合約。</p></> : null}
      {detail.data && contract ? (
        <>
          <RunDetail approval={detail.data} contract={contract} />
          {detail.data.status === "pending" ? <ApprovalCard approval={detail.data} compact approveLabel="執行" decideVia="v2" onDecided={() => void detail.refetch()} /> : null}
        </>
      ) : null}
      <div className="flex items-center justify-between gap-3">
        <Link className="text-sm text-sky-700 underline" to="/runs">查看執行紀錄</Link>
      </div>
      <ToolDetails item={item} />
      {planId ? <RunMonitorCard planId={planId} target={typeof target?.server_name === "string" ? target.server_name : null} transcriptItems={transcriptItems} analyzing={analyzing} onAnalyzeRun={onAnalyzeRun} /> : null}
    </Card>
  );
}
