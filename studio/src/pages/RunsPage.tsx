import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/api/client";
import {
  useExperiments,
  useJobLog,
  useJobs,
  useProjects,
  type JobRow,
} from "@/api/hooks";
import type { Approval, ExperimentItem, ExperimentMember } from "@/api/types";
import { Badge, stateTone } from "@/components/ui/badge";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { RunComposer } from "@/features/runs/RunComposer";
import { formatTime } from "@/lib";
import { cn } from "@/lib";

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
  const [tab, setTab] = useState<"run" | "jobs">("run");
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
            {([["run", "執行"], ["jobs", "任務"]] as const).map(([key, label]) => (
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
          {tab === "run" ? (
            <>
              <RunComposer projectId={projectId} projectName={projectName} onCreated={() => { void experiments.refetch(); }} />
              {list.length === 0 && !experiments.isLoading ? <div className="text-sm text-slate-400">這個專案還沒有實驗。</div> : null}
              {list.map((experiment) => (
                <ExperimentView key={experiment.experiment_id} experiment={experiment} onLog={(jobId) => setLogJob(jobId)} />
              ))}
            </>
          ) : null}
          {tab === "jobs" ? <JobsTable projectName={projectName} onLog={(jobId) => setLogJob(jobId)} /> : null}
        </>
      ) : (
        <div className="text-sm text-slate-400">選一個專案開始。</div>
      )}
      {logJob != null ? <LogDrawer jobId={logJob} onClose={() => setLogJob(null)} /> : null}
    </div>
  );
}
