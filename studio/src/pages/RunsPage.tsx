import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import {
  useExperiments,
  useJobLog,
  useJobs,
  useLiveServers,
  useProjects,
  useServerConfigs,
  useWorkspace,
  type JobRow,
} from "@/api/hooks";
import type { Approval, ExperimentItem, ExperimentMember, TemplateParameter } from "@/api/types";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { expandedRunCount, parseAxisValues, type MatrixAxis } from "@/features/experiments/matrix";
import { formatTime } from "@/lib";
import { cn } from "@/lib";

interface AxisDraft {
  key: number;
  name: string;
  valuesText: string;
}

function jobTone(status: string | null | undefined) {
  switch (status) {
    case "running":
      return "info" as const;
    case "done":
      return "ok" as const;
    case "failed":
      return "bad" as const;
    case "queued":
      return "warn" as const;
    default:
      return "neutral" as const;
  }
}

function MetricsCell({ jobId }: { jobId: number }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<{ key: string; value_text: string }[] | null>(null);
  const load = async () => {
    setOpen(!open);
    if (rows === null) {
      try {
        const data = await api<{ collection_status: string; metrics: { key: string; value_text: string }[] }>(
          `/jobs/${jobId}/metrics`,
        );
        setRows(data.metrics ?? []);
      } catch {
        setRows([]);
      }
    }
  };
  return (
    <div>
      <button type="button" className="text-xs text-sky-700 underline" onClick={() => void load()}>
        metrics
      </button>
      {open && rows ? (
        <div className="mt-1 space-y-0.5 text-xs text-slate-600">
          {rows.length === 0 ? <div>（無）</div> : rows.map((row) => (
            <div key={row.key}>
              {row.key} = {row.value_text}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function LogDrawer({ jobId, onClose }: { jobId: number; onClose: () => void }) {
  const log = useJobLog(jobId);
  return (
    <div className="fixed inset-y-0 right-0 z-20 flex w-[34rem] max-w-full flex-col border-l border-slate-300 bg-white shadow-2xl">
      <div className="flex items-center justify-between border-b border-slate-200 px-3 py-2 text-sm font-semibold">
        <span>
          Job #{jobId} log{log.data ? `（${log.data.status}${log.data.live ? "，live" : ""}）` : ""}
        </span>
        <button type="button" className="text-slate-500" onClick={onClose}>
          關閉
        </button>
      </div>
      <pre className="min-h-0 flex-1 overflow-auto bg-slate-950 p-3 text-xs leading-relaxed text-slate-100">
        {log.data?.log_tail || "（還沒有 log）"}
      </pre>
    </div>
  );
}

function ServerChips({
  candidates,
  selected,
  onToggle,
}: {
  candidates: { server_name: string; ready?: boolean }[];
  selected: string[];
  onToggle: (name: string) => void;
}) {
  const live = useLiveServers();
  const configs = useServerConfigs();
  const liveByName = new Map((live.data ?? []).map((server) => [server.name, server]));
  const tagsByName = new Map((configs.data ?? []).map((config) => [config.name, config.tags ?? []]));
  if (candidates.length === 0) return <div className="text-xs text-slate-500">這個專案還沒有可用的執行機器（instance 未就緒）。</div>;
  return (
    <div className="flex flex-wrap gap-2">
      {candidates.map((candidate) => {
        const name = candidate.server_name;
        const info = liveByName.get(name);
        const picked = selected.includes(name);
        const gpuText = info?.gpu_count ? `GPU×${info.gpu_count}${info.gpu_util_max != null ? ` ${info.gpu_util_max}%` : ""}` : "";
        return (
          <button
            key={name}
            type="button"
            onClick={() => onToggle(name)}
            className={cn(
              "rounded-full border px-3 py-1 text-xs",
              picked ? "border-sky-600 bg-sky-50 text-sky-800" : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50",
            )}
            title={(tagsByName.get(name) ?? []).join(", ")}
          >
            <span className={info?.online ? "text-emerald-500" : "text-slate-300"}>●</span> {name}
            {gpuText ? <span className="ml-1 text-slate-500">{gpuText}</span> : null}
            {candidate.ready === false ? <span className="ml-1 text-amber-600">未就緒</span> : null}
          </button>
        );
      })}
    </div>
  );
}

interface PreviewResult {
  run_count: number;
  plan_digests: string[];
  members: { target_server: string; parameter_values: Record<string, unknown>; plan_digest: string }[];
}

function ExperimentComposer({ projectId, onCreated }: { projectId: string; onCreated: () => void }) {
  const workspace = useWorkspace(projectId);
  const client = useQueryClient();
  const [axes, setAxes] = useState<AxisDraft[]>([{ key: 1, name: "", valuesText: "" }]);
  const [servers, setServers] = useState<string[]>([]);
  const [versionId, setVersionId] = useState("");
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);

  const parameters: TemplateParameter[] = workspace.data?.run_template?.parameters ?? [];
  const paramType = (name: string) => parameters.find((parameter) => parameter.name === name)?.type;
  const versions = workspace.data?.run_creation_options?.project_version_candidates ?? [];
  const candidates = workspace.data?.run_creation_options?.ssh_target_candidates ?? [];
  const effectiveVersion = versionId || versions[0]?.id || "";

  const parsedAxes: { axis: MatrixAxis | null; error: string | null; draft: AxisDraft }[] = axes.map((draft) => {
    if (!draft.name) return { axis: null, error: null, draft };
    const parsed = parseAxisValues(draft.valuesText, paramType(draft.name));
    return parsed.error ? { axis: null, error: parsed.error, draft } : { axis: { name: draft.name, values: parsed.values }, error: null, draft };
  });
  const readyAxes = parsedAxes.filter((entry) => entry.axis != null).map((entry) => entry.axis as MatrixAxis);
  const runCount = expandedRunCount(readyAxes);
  const composeError =
    readyAxes.length === 0
      ? "至少要一個軸"
      : parsedAxes.some((entry) => entry.error)
        ? "有軸的值格式不對"
        : runCount > 32
          ? `展開 ${runCount} 個 run，超過上限 32`
          : servers.length === 0
            ? "至少選一台伺服器"
            : !effectiveVersion
              ? "沒有可用的專案版本"
              : null;

  const body = () => ({
    project_version_id: effectiveVersion,
    template_selection: { kind: "project_defaults", project_defaults_revision_id: workspace.data?.defaults?.revision_id ?? null },
    dataset_selection: { kind: "none" },
    matrix: { axes: readyAxes },
    guard: { total_runs: runCount, target_servers: servers, est_gpu_hours: null, est_storage: null },
  });

  const previewMutation = useMutation({
    mutationFn: () => api<PreviewResult>(`/api/v2/projects/${projectId}/experiment-previews`, { method: "POST", json: body() }),
    onSuccess: (result) => setPreview(result),
  });
  const requestMutation = useMutation({
    mutationFn: async () => {
      const created = await api<{ approval_id: number }>(`/api/v2/projects/${projectId}/experiment-requests`, {
        method: "POST",
        json: { ...body(), expected_plan_digests: preview?.plan_digests ?? null },
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      return api<Approval>(`/api/v2/approvals/${created.approval_id}`);
    },
    onSuccess: (card) => setApproval(card),
  });

  const invalidate = () => {
    setPreview(null);
    setApproval(null);
  };
  const mutError = (previewMutation.error ?? requestMutation.error) as Error | undefined;

  return (
    <Card className="space-y-3">
      <CardTitle>建立實驗（矩陣 → 一張核准卡 → N 個 run）</CardTitle>
      {workspace.data && !workspace.data.run_template ? (
        <div className="text-sm text-amber-700">這個專案還沒有執行模板；模板設定介面即將加入專案頁（目前需經 API 建立 run_template_change_v2）。</div>
      ) : null}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label className="block text-sm">
          <span className="text-slate-600">基底版本</span>
          <select className="mt-1 w-full rounded border border-slate-300 p-1.5" value={effectiveVersion} onChange={(event) => { setVersionId(event.target.value); invalidate(); }}>
            {versions.map((version) => (
              <option key={version.id} value={version.id}>
                {version.id.slice(0, 8)} · {version.state ?? ""} · {version.created_at ?? ""}
              </option>
            ))}
          </select>
        </label>
        <div className="text-sm">
          <span className="text-slate-600">模板</span>
          <div className="mt-1 rounded border border-slate-200 bg-slate-50 p-1.5 text-xs">
            {workspace.data?.run_template?.name ?? "…"}（參數：{parameters.map((parameter) => parameter.name).join("、") || "無"}）
            <div className="text-slate-500">沒有列成軸的參數會用專案 defaults 的值。</div>
          </div>
        </div>
      </div>
      <div className="space-y-2">
        <span className="text-sm text-slate-600">矩陣軸（≤8 軸、展開 ≤32 run；值用逗號或換行分隔）</span>
        {axes.map((draft, index) => {
          const entry = parsedAxes[index];
          return (
            <div key={draft.key} className="flex flex-wrap items-start gap-2">
              <select
                className="rounded border border-slate-300 p-1.5 text-sm"
                value={draft.name}
                onChange={(event) => { setAxes(axes.map((a) => (a.key === draft.key ? { ...a, name: event.target.value } : a))); invalidate(); }}
              >
                <option value="">選參數…</option>
                {parameters
                  .filter((parameter) => parameter.name === draft.name || !axes.some((a) => a.name === parameter.name))
                  .map((parameter) => (
                    <option key={parameter.name} value={parameter.name}>
                      {parameter.name}（{parameter.type ?? "string"}）
                    </option>
                  ))}
              </select>
              <div className="min-w-64 flex-1">
                <input
                  className="w-full rounded border border-slate-300 p-1.5 text-sm"
                  placeholder={paramType(draft.name) === "number" ? "例：0.1, 0.01" : "例：16, 32"}
                  value={draft.valuesText}
                  onChange={(event) => { setAxes(axes.map((a) => (a.key === draft.key ? { ...a, valuesText: event.target.value } : a))); invalidate(); }}
                />
                {entry?.error ? <div className="text-xs text-rose-700">{entry.error}</div> : null}
              </div>
              <Button variant="ghost" onClick={() => { setAxes(axes.filter((a) => a.key !== draft.key)); invalidate(); }}>
                移除
              </Button>
            </div>
          );
        })}
        <Button disabled={axes.length >= 8} onClick={() => setAxes([...axes, { key: Date.now(), name: "", valuesText: "" }])}>
          ＋ 加軸
        </Button>
      </div>
      <div className="space-y-1">
        <span className="text-sm text-slate-600">目標伺服器（run i → 第 i%N 台，輪流分配）</span>
        <ServerChips
          candidates={candidates}
          selected={servers}
          onToggle={(name) => { setServers(servers.includes(name) ? servers.filter((s) => s !== name) : [...servers, name]); invalidate(); }}
        />
      </div>
      <div className="flex items-center gap-3">
        <Badge tone={composeError ? "neutral" : "info"}>展開 {runCount} 個 run</Badge>
        {composeError ? <span className="text-xs text-slate-500">{composeError}</span> : null}
        <div className="ml-auto flex gap-2">
          <Button variant="secondary" disabled={Boolean(composeError) || previewMutation.isPending} onClick={() => previewMutation.mutate()}>
            預覽
          </Button>
          <Button variant="primary" disabled={!preview || requestMutation.isPending} onClick={() => requestMutation.mutate()} title={!preview ? "先預覽" : ""}>
            建立核准卡
          </Button>
        </div>
      </div>
      {mutError ? <div className="rounded bg-rose-50 px-2 py-1 text-xs text-rose-800">{mutError.message}</div> : null}
      {preview ? (
        <div className="overflow-x-auto rounded border border-slate-200">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-left text-slate-500">
              <tr>
                <th className="px-2 py-1">#</th>
                <th className="px-2 py-1">伺服器</th>
                <th className="px-2 py-1">參數</th>
              </tr>
            </thead>
            <tbody>
              {preview.members.map((member, index) => (
                <tr key={member.plan_digest} className="border-t border-slate-100">
                  <td className="px-2 py-1">{index + 1}</td>
                  <td className="px-2 py-1">{member.target_server}</td>
                  <td className="px-2 py-1 font-mono">
                    {Object.entries(member.parameter_values).map(([key, value]) => `${key}=${String(value)}`).join("  ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {approval ? (
        <ApprovalCard
          approval={approval}
          decideVia="v2"
          onDecided={() => {
            setApproval(null);
            setPreview(null);
            void client.invalidateQueries({ queryKey: ["experiments"] });
            onCreated();
          }}
        />
      ) : null}
    </Card>
  );
}

function StopButton({ planId }: { planId: string }) {
  const [approval, setApproval] = useState<Approval | null>(null);
  const stop = useMutation({
    mutationFn: async () => {
      const result = await api<{ approval?: Approval } & Record<string, unknown>>(`/api/v2/runs/${planId}/stop-requests`, {
        method: "POST",
        json: {},
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      return (result.approval ?? result) as Approval;
    },
    onSuccess: (card) => setApproval(card && (card as Approval).id ? (card as Approval) : null),
  });
  return (
    <div>
      <button type="button" className="text-xs text-rose-700 underline" disabled={stop.isPending} onClick={() => stop.mutate()}>
        stop
      </button>
      {stop.error ? <div className="text-xs text-rose-700">{(stop.error as Error).message}</div> : null}
      {approval ? <div className="mt-1 w-96"><ApprovalCard approval={approval} onDecided={() => setApproval(null)} /></div> : null}
    </div>
  );
}

function ExperimentView({ experiment, onLog }: { experiment: ExperimentItem; onLog: (jobId: number) => void }) {
  const axes = experiment.matrix?.axes ?? [];
  return (
    <Card className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <CardTitle className="mb-0">實驗 #{experiment.experiment_id}</CardTitle>
        <Badge tone={stateTone(experiment.status === "approved" ? "ok" : experiment.status === "pending" ? "input-required" : "failed")}>
          {experiment.status}
        </Badge>
        <span className="text-xs text-slate-500">
          {experiment.run_count} runs · {axes.map((axis) => `${axis.name}×${axis.values.length}`).join("，")} · 卡 #{experiment.approval_id}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-slate-50 text-left text-slate-500">
            <tr>
              <th className="px-2 py-1">參數</th>
              <th className="px-2 py-1">伺服器</th>
              <th className="px-2 py-1">狀態</th>
              <th className="px-2 py-1">metrics</th>
              <th className="px-2 py-1">動作</th>
            </tr>
          </thead>
          <tbody>
            {(experiment.members ?? []).map((member: ExperimentMember) => (
              <tr key={member.execution_plan_id ?? member.plan_digest} className="border-t border-slate-100 align-top">
                <td className="px-2 py-1 font-mono">
                  {Object.entries(member.parameter_values ?? {}).map(([key, value]) => `${key}=${String(value)}`).join("  ")}
                </td>
                <td className="px-2 py-1">{member.server_name}</td>
                <td className="px-2 py-1">
                  <Badge tone={jobTone(member.canonical_job_status)}>{member.canonical_job_status ?? "未生效"}</Badge>
                </td>
                <td className="px-2 py-1">
                  <span className="text-slate-500">{member.metrics_status ?? "—"}</span>
                  {member.job_id != null && member.canonical_job_status === "done" ? <MetricsCell jobId={member.job_id} /> : null}
                </td>
                <td className="px-2 py-1">
                  <div className="flex gap-2">
                    {member.job_id != null ? (
                      <button type="button" className="text-xs text-sky-700 underline" onClick={() => onLog(member.job_id as number)}>
                        log
                      </button>
                    ) : null}
                    {member.execution_plan_id && (member.canonical_job_status === "running" || member.canonical_job_status === "queued") ? (
                      <StopButton planId={member.execution_plan_id} />
                    ) : null}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function SingleRunComposer({ projectId, onCreated }: { projectId: string; onCreated: () => void }) {
  const workspace = useWorkspace(projectId);
  const [versionId, setVersionId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<{ plan_digest: string; plan?: Record<string, unknown> } | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);

  const parameters: TemplateParameter[] = workspace.data?.run_template?.parameters ?? [];
  const versions = workspace.data?.run_creation_options?.project_version_candidates ?? [];
  const candidates = workspace.data?.run_creation_options?.ssh_target_candidates ?? [];
  const effectiveVersion = versionId || versions[0]?.id || "";
  const effectiveTarget = targetId || (candidates.find((candidate) => candidate.ready !== false) as { id?: string } | undefined)?.id || (candidates[0] as { id?: string } | undefined)?.id || "";

  const typedOverrides = () => {
    const out: Record<string, string | number | boolean> = {};
    for (const [name, raw] of Object.entries(overrides)) {
      const trimmed = raw.trim();
      if (!trimmed) continue;
      const type = parameters.find((parameter) => parameter.name === name)?.type;
      if (type === "integer") out[name] = Number.parseInt(trimmed, 10);
      else if (type === "boolean") out[name] = trimmed === "true";
      else out[name] = trimmed; // number 保持十進位字串
    }
    return out;
  };
  const body = () => ({
    project_version_id: effectiveVersion,
    template_selection: { kind: "project_defaults", project_defaults_revision_id: workspace.data?.defaults?.revision_id ?? null },
    parameter_overrides: typedOverrides(),
    dataset_selection: { kind: "none" },
    target_selection: { kind: "server_config_revision", server_config_revision_id: effectiveTarget },
  });
  const previewMutation = useMutation({
    mutationFn: () => api<{ plan_digest: string }>(`/api/v2/projects/${projectId}/run-previews`, { method: "POST", json: body() }),
    onSuccess: (result) => setPreview(result),
  });
  const requestMutation = useMutation({
    mutationFn: async () => {
      const created = await api<{ approval_id: number }>(`/api/v2/projects/${projectId}/run-requests`, {
        method: "POST",
        json: { ...body(), expected_plan_digest: preview?.plan_digest },
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      return api<Approval>(`/api/v2/approvals/${created.approval_id}`);
    },
    onSuccess: (card) => setApproval(card),
  });
  const invalidate = () => {
    setPreview(null);
    setApproval(null);
  };
  const mutError = (previewMutation.error ?? requestMutation.error) as Error | undefined;
  const disabled = !effectiveVersion || !effectiveTarget;

  return (
    <Card className="space-y-3">
      <CardTitle>單一 Run（一張執行設定卡：版本＋模板＋參數＋目標機器）</CardTitle>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label className="block text-sm">
          <span className="text-slate-600">基底版本</span>
          <select className="mt-1 w-full rounded border border-slate-300 p-1.5" value={effectiveVersion} onChange={(event) => { setVersionId(event.target.value); invalidate(); }}>
            {versions.map((version) => (
              <option key={version.id} value={version.id}>
                {version.id.slice(0, 8)} · {version.state ?? ""} · {version.created_at ?? ""}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm">
          <span className="text-slate-600">目標機器</span>
          <select className="mt-1 w-full rounded border border-slate-300 p-1.5" value={effectiveTarget} onChange={(event) => { setTargetId(event.target.value); invalidate(); }}>
            {candidates.map((candidate) => {
              const entry = candidate as { id?: string; server_name: string; ready?: boolean };
              return (
                <option key={entry.id} value={entry.id}>
                  {entry.server_name}
                  {entry.ready === false ? "（未就緒）" : ""}
                </option>
              );
            })}
          </select>
        </label>
      </div>
      {parameters.length > 0 ? (
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {parameters.map((parameter) => (
            <label key={parameter.name} className="block text-sm">
              <span className="text-slate-600">
                {parameter.name}（{parameter.type ?? "string"}
                {parameter.required ? "，必填" : "，留空用 defaults"}）
              </span>
              {parameter.enum_values?.length ? (
                <select className="mt-1 w-full rounded border border-slate-300 p-1.5" value={overrides[parameter.name] ?? ""} onChange={(event) => { setOverrides({ ...overrides, [parameter.name]: event.target.value }); invalidate(); }}>
                  <option value="">（defaults）</option>
                  {parameter.enum_values.map((value) => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </select>
              ) : (
                <input className="mt-1 w-full rounded border border-slate-300 p-1.5" value={overrides[parameter.name] ?? ""} onChange={(event) => { setOverrides({ ...overrides, [parameter.name]: event.target.value }); invalidate(); }} />
              )}
            </label>
          ))}
        </div>
      ) : null}
      <div className="flex items-center gap-2">
        <div className="ml-auto flex gap-2">
          <Button variant="secondary" disabled={disabled || previewMutation.isPending} onClick={() => previewMutation.mutate()}>
            預覽
          </Button>
          <Button variant="primary" disabled={!preview || requestMutation.isPending} onClick={() => requestMutation.mutate()} title={!preview ? "先預覽" : ""}>
            建立核准卡
          </Button>
        </div>
      </div>
      {preview ? <div className="text-xs text-slate-500">plan digest：<code>{preview.plan_digest.slice(0, 16)}…</code></div> : null}
      {mutError ? <div className="rounded bg-rose-50 px-2 py-1 text-xs text-rose-800">{mutError.message}</div> : null}
      {approval ? (
        <ApprovalCard approval={approval} decideVia="v2" onDecided={() => { setApproval(null); setPreview(null); onCreated(); }} />
      ) : null}
    </Card>
  );
}

const JOB_STATUS_FILTERS = ["", "queued", "running", "done", "failed"] as const;

function JobsTable({ projectName, onLog }: { projectName: string; onLog: (jobId: number) => void }) {
  const [statusFilter, setStatusFilter] = useState<string>("");
  const jobs = useJobs(projectName, statusFilter);
  const stop = useMutation({
    mutationFn: (jobId: number) => api(`/api/v2/jobs/${jobId}/stop-requests`, { method: "POST", json: { source: "web" } }),
    onSuccess: () => void jobs.refetch(),
  });
  const cancel = useMutation({
    mutationFn: (jobId: number) => api(`/api/v2/jobs/${jobId}/cancel`, { method: "POST" }),
    onSuccess: () => void jobs.refetch(),
  });
  const rows = jobs.data ?? [];
  return (
    <Card className="space-y-2">
      <div className="flex items-center gap-2">
        <CardTitle className="mb-0">任務</CardTitle>
        {JOB_STATUS_FILTERS.map((status) => (
          <button
            key={status || "all"}
            type="button"
            className={cn("rounded-full border px-2 py-0.5 text-xs", statusFilter === status ? "border-sky-600 bg-sky-50 text-sky-800" : "border-slate-300 text-slate-600")}
            onClick={() => setStatusFilter(status)}
          >
            {status || "全部"}
          </button>
        ))}
        {(stop.error ?? cancel.error) ? <span className="text-xs text-rose-700">{((stop.error ?? cancel.error) as Error).message}</span> : null}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-slate-50 text-left text-slate-500">
            <tr>
              <th className="px-2 py-1">#</th>
              <th className="px-2 py-1">type</th>
              <th className="px-2 py-1">狀態</th>
              <th className="px-2 py-1">伺服器</th>
              <th className="px-2 py-1">建立</th>
              <th className="px-2 py-1">exit</th>
              <th className="px-2 py-1">動作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((job: JobRow) => (
              <tr key={job.id} className="border-t border-slate-100">
                <td className="px-2 py-1">{job.id}</td>
                <td className="px-2 py-1">{job.type}</td>
                <td className="px-2 py-1">
                  <Badge tone={jobTone(job.status)}>{job.status}</Badge>
                  {job.stalled_suspect ? <span className="ml-1 text-amber-600">疑似卡死</span> : null}
                </td>
                <td className="px-2 py-1">{job.server ?? "—"}</td>
                <td className="px-2 py-1">{formatTime(job.created_at)}</td>
                <td className="px-2 py-1">{job.exit_code ?? ""}</td>
                <td className="px-2 py-1">
                  <div className="flex gap-2">
                    <button type="button" className="text-sky-700 underline" onClick={() => onLog(job.id)}>log</button>
                    {job.status === "running" ? (
                      <button type="button" className="text-rose-700 underline" onClick={() => stop.mutate(job.id)}>stop</button>
                    ) : null}
                    {job.status === "queued" ? (
                      <button type="button" className="text-rose-700 underline" onClick={() => cancel.mutate(job.id)}>cancel</button>
                    ) : null}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && !jobs.isLoading ? <div className="py-3 text-center text-xs text-slate-400">沒有任務。</div> : null}
      </div>
    </Card>
  );
}

export function RunsPage() {
  const projects = useProjects();
  const [searchParams, setSearchParams] = useSearchParams();
  const projectName = searchParams.get("project") ?? "";
  const setProjectName = (name: string) => setSearchParams(name ? { project: name } : {}, { replace: true });
  const selected = (projects.data ?? []).find((project) => project.name === projectName) ?? null;
  const projectId = selected?.id ?? "";
  const experiments = useExperiments(projectId || undefined);
  const [logJob, setLogJob] = useState<number | null>(null);
  const [tab, setTab] = useState<"experiments" | "single" | "jobs">("experiments");
  const list = useMemo(() => experiments.data ?? [], [experiments.data]);

  return (
    <div className="min-h-0 space-y-4 overflow-y-auto p-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">實驗與 Run</h1>
        <select className="rounded border border-slate-300 p-1.5 text-sm" value={projectName} onChange={(event) => setProjectName(event.target.value)}>
          <option value="">選專案…</option>
          {(projects.data ?? []).map((project) => (
            <option key={project.name} value={project.name}>
              {project.name}
            </option>
          ))}
        </select>
      </div>
      {selected && !projectId ? <div className="text-sm text-amber-700">後端還沒回報這個專案的 UUID（重新整理或重佈 Server A）。</div> : null}
      {projectId ? (
        <>
          <div className="flex gap-1 border-b border-slate-200">
            {([["experiments", "實驗（矩陣）"], ["single", "單一 Run"], ["jobs", "任務"]] as const).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={cn("rounded-t-md px-3 py-1.5 text-sm", tab === key ? "border border-b-0 border-slate-200 bg-white font-medium" : "text-slate-500 hover:text-slate-800")}
                onClick={() => setTab(key)}
              >
                {label}
              </button>
            ))}
          </div>
          {tab === "experiments" ? (
            <>
              <ExperimentComposer projectId={projectId} onCreated={() => void experiments.refetch()} />
              {list.length === 0 && !experiments.isLoading ? <div className="text-sm text-slate-400">這個專案還沒有實驗。</div> : null}
              {list.map((experiment) => (
                <ExperimentView key={experiment.experiment_id} experiment={experiment} onLog={(jobId) => setLogJob(jobId)} />
              ))}
            </>
          ) : null}
          {tab === "single" ? <SingleRunComposer projectId={projectId} onCreated={() => setTab("jobs")} /> : null}
          {tab === "jobs" ? <JobsTable projectName={projectName} onLog={(jobId) => setLogJob(jobId)} /> : null}
        </>
      ) : (
        <div className="text-sm text-slate-400">選一個專案開始。</div>
      )}
      {logJob != null ? <LogDrawer jobId={logJob} onClose={() => setLogJob(null)} /> : null}
    </div>
  );
}
