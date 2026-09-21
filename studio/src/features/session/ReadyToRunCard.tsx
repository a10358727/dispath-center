import { Link } from "react-router-dom";
import { useApprovalDetail } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { Badge } from "@/components/ui/badge";
import { Card, CardTitle } from "@/components/ui/card";
import type { TranscriptItem } from "./transcript";

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

function ToolDetails({ item }: { item: ToolItem }) {
  return (
    <details className="text-xs text-slate-500">
      <summary className="cursor-pointer text-sky-700">Advanced tool details</summary>
      <div className="mt-2 space-y-1">
        <div>Tool: <code>{item.name}</code>{item.toolUseId ? <> · call <code>{item.toolUseId}</code></> : null}</div>
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
        <CardTitle className="mb-0">{approval.title ?? "Ready to Run"}</CardTitle>
        <Badge tone={approval.status === "pending" || approval.status === "approved" ? "ok" : "bad"}>
          {approval.status === "pending" ? "Ready" : approval.status}
        </Badge>
      </div>
      {approval.summary ? <p className="text-sm text-slate-700">{approval.summary}</p> : null}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-slate-500">Version</dt>
        <dd>{typeof version?.git_commit === "string" ? short(version.git_commit) : "Unavailable"} <span className="text-xs text-slate-500">promoted</span></dd>
        <dt className="text-slate-500">Compute</dt>
        <dd>{typeof target?.server_name === "string" ? target.server_name : "Unavailable"}</dd>
        <dt className="text-slate-500">Readiness</dt>
        <dd>Ready when proposed · rechecked on Run</dd>
        {parameters.length > 0 ? <><dt className="text-slate-500">Parameters</dt><dd>{parameters.map(([key, value]) => `${key}=${value}`).join(" · ")}</dd></> : null}
        {estimateLabel ? <><dt className="text-slate-500">Estimate</dt><dd>{estimateLabel} <span className="text-xs text-slate-500">({String(estimate?.source)})</span></dd></> : null}
      </dl>
      <details className="text-xs text-slate-500">
        <summary className="cursor-pointer text-sky-700">Advanced contract details</summary>
        <pre className="mt-2 max-h-64 overflow-auto rounded bg-slate-50 p-2">{JSON.stringify({ approval_id: approval.id, review: approval.review }, null, 2)}</pre>
      </details>
    </>
  );
}

export function ReadyToRunCard({ item }: { item: ToolItem }) {
  const reference = runReference(item);
  const approvalId = reference.state === "ready" ? reference.approvalId : null;
  const detail = useApprovalDetail(approvalId);
  const contract = verifiedContract(detail.data);
  return (
    <Card className="my-2 space-y-3 border-sky-200" data-testid="ready-to-run-card">
      {reference.state === "loading" ? <><CardTitle>Preparing Run proposal</CardTitle><p className="text-sm text-slate-500" role="status">Waiting for the governed request…</p></> : null}
      {reference.state === "tool-error" ? <><CardTitle>Run proposal unavailable</CardTitle><p className="text-sm text-rose-700">The platform did not create a Run approval.</p></> : null}
      {reference.state === "malformed" ? <><CardTitle>Run proposal unavailable</CardTitle><p className="text-sm text-amber-700">The tool result did not contain a valid approval reference.</p></> : null}
      {approvalId != null && detail.isLoading ? <><CardTitle>Loading Run proposal</CardTitle><p className="text-sm text-slate-500" role="status">Loading the verified approval contract…</p></> : null}
      {approvalId != null && detail.isError ? <><CardTitle>Run proposal unavailable</CardTitle><p className="text-sm text-rose-700">The approval detail is unavailable or you cannot view it.</p></> : null}
      {detail.data && !contract ? <><CardTitle>Run proposal unavailable</CardTitle><p className="text-sm text-rose-700">The platform could not provide a verified typed Run contract.</p></> : null}
      {detail.data && contract ? (
        <>
          <RunDetail approval={detail.data} contract={contract} />
          {detail.data.status === "pending" ? <ApprovalCard approval={detail.data} compact approveLabel="Run" decideVia="v2" onDecided={() => void detail.refetch()} /> : null}
        </>
      ) : null}
      <div className="flex items-center justify-between gap-3">
        <Link className="text-sm text-sky-700 underline" to="/runs">View Runs history</Link>
      </div>
      <ToolDetails item={item} />
    </Card>
  );
}
