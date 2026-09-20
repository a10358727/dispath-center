import { Link } from "react-router-dom";
import { useActiveJobs, useApprovals, useAuditEvents, useIdleSummary, useLiveServers, useProjects, useServerConfigs } from "@/api/hooks";
import type { Project } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { projectCompute } from "@/pages/ServersPage";
import { describeActivity } from "@/labels";
import { formatTime } from "@/lib";

type QueryState = { error: unknown; isLoading: boolean; isFetching: boolean; data?: unknown; refetch: () => unknown };

function SectionState({ queries, empty, children }: { queries: QueryState[]; empty: boolean; children: React.ReactNode }) {
  const loading = queries.some((query) => query.isLoading && query.data === undefined);
  const hasData = queries.some((query) => query.data !== undefined);
  const errors = queries.filter((query) => query.error);
  if (loading && !hasData) return <p className="text-sm text-slate-500">載入中…</p>;
  return (
    <>
      {loading ? <p role="status" className="mb-2 text-xs text-slate-500">部分資料仍在載入；先顯示已取得的內容。</p> : null}
      {errors.length > 0 ? (
        <div role="alert" className="mb-2 flex items-center gap-2 rounded bg-rose-50 p-2 text-xs text-rose-800">
          <span>{errors.some((query) => (query.error as { status?: number })?.status === 403) ? "沒有權限查看這部分資料。" : errors.some((query) => query.data !== undefined) ? "更新失敗；目前顯示快取資料。" : "部分資料無法取得；可用資料仍顯示。"}</span>
          <Button onClick={() => queries.forEach((query) => void query.refetch())}>重試</Button>
        </div>
      ) : null}
      {queries.some((query) => query.isFetching && query.data) ? <p role="status" className="mb-2 text-xs text-amber-700">正在更新；目前顯示快取資料。</p> : null}
      {empty && errors.length === 0 && !loading ? <p className="text-sm text-slate-500">目前沒有項目。</p> : children}
    </>
  );
}

function projectAttention(projects: Project[], approvals: ReturnType<typeof useApprovals>["data"]) {
  const byId = new Map(projects.filter((project) => project.id).map((project) => [project.id as string, project]));
  const items: { key: string; project: Project; reason: string }[] = [];
  for (const project of projects) {
    const instances = project.instances ?? [];
    const dirty = instances.filter((instance) => instance.dirty).length;
    const unknown = instances.filter((instance) => instance.state === "unknown").length;
    const unavailable = instances.filter((instance) => instance.state && !["available", "dirty", "unknown"].includes(instance.state)).length;
    const reasons = [dirty ? `${dirty} 個 instance 有未提交變更` : "", unknown ? `${unknown} 個 instance 狀態未知` : "", unavailable ? `${unavailable} 個 instance 需查看` : ""].filter(Boolean);
    if (reasons.length > 0) items.push({ key: `instance-${project.name}`, project, reason: reasons.join("；") });
  }
  for (const approval of approvals ?? []) {
    const projectId = typeof approval.project_id === "string" ? approval.project_id : null;
    const project = projectId ? byId.get(projectId) : undefined;
    if (project) items.push({ key: `approval-${approval.id}`, project, reason: `等待核准 · 卡 #${approval.id}` });
  }
  return items;
}

export function OverviewPage() {
  const projects = useProjects();
  const approvals = useApprovals("pending", 50);
  const running = useActiveJobs("running");
  const queued = useActiveJobs("queued");
  const configs = useServerConfigs();
  const live = useLiveServers();
  const idle = useIdleSummary();
  const events = useAuditEvents(20);
  const attention = projectAttention(projects.data ?? [], approvals.data);
  const jobs = [...(running.data ?? []), ...(queued.data ?? [])];
  const compute = projectCompute(configs.data, live.data, new Map((idle.data ?? []).map((row) => [row.server_name, row.freshness_seconds ?? null])));
  return (
    <main className="min-h-0 space-y-4 overflow-y-auto p-6">
      <div>
        <h1 className="text-xl font-semibold">Overview</h1>
        <p className="text-sm text-slate-500">目前需要注意的工程工作、Run 與 Compute。</p>
      </div>
      {navigator.onLine === false ? <div role="status" className="rounded border border-amber-300 bg-amber-50 p-2 text-sm text-amber-900">瀏覽器目前離線；各區塊可能顯示快取資料。</div> : null}
      {(projects.data ?? []).length === 0 && !projects.isLoading && !projects.error ? <Card><p className="text-sm text-slate-600">還沒有專案。</p><Link className="text-sm text-sky-700 underline" to="/projects/import">匯入專案</Link></Card> : null}
      <div className="grid gap-4 xl:grid-cols-2">
        <section aria-labelledby="project-attention">
          <Card>
            <CardTitle id="project-attention">需要注意的專案</CardTitle>
            <SectionState queries={[projects, approvals]} empty={attention.length === 0}>
              {attention.map((item) => (
                <div key={item.key} className="flex items-center justify-between border-t border-slate-100 py-2 text-sm">
                  <div><div className="font-medium">{item.project.name}</div><div className="text-xs text-slate-500">{item.reason}</div></div>
                  <Link className="text-sky-700 underline" to={`/projects/${encodeURIComponent(item.project.name)}`}>查看專案</Link>
                </div>
              ))}
            </SectionState>
            <p className="mt-2 text-xs text-slate-400">核准僅顯示目前載入的 50 筆可見卡片，不代表全站總數。</p>
          </Card>
        </section>
        <section aria-labelledby="active-runs"><Card><CardTitle id="active-runs">進行中的 Run</CardTitle><SectionState queries={[running, queued]} empty={jobs.length === 0}>{jobs.map((job) => <div key={`${job.status}-${job.id}`} className="flex items-center justify-between border-t border-slate-100 py-2 text-sm"><div><div className="font-medium">Run · 工作 #{job.id} <Badge tone={job.status === "running" ? "info" : "warn"}>{job.status}</Badge></div><div className="text-xs text-slate-500">{job.project ?? "未連結專案"} · Compute {job.server ?? "尚未指派"}</div></div>{job.project ? <Link className="text-sky-700 underline" to={`/runs?project=${encodeURIComponent(job.project)}&job=${job.id}&tab=jobs`}>查看 Run</Link> : <Link className="text-sky-700 underline" to="/runs?tab=jobs">查看 Runs</Link>}</div>)}</SectionState><p className="mt-2 text-xs text-slate-400">來自既有執行工作佇列；不代表所有 typed plan 的完整清單。</p></Card></section>
        <section aria-labelledby="compute-health">
          <Card>
            <CardTitle id="compute-health">Compute 健康狀態</CardTitle>
            <SectionState queries={[configs, live, idle]} empty={compute.length === 0}>
              {compute.map((row) => {
                const gpu = row.live?.gpus?.[0];
                return (
                  <div key={row.name} className="flex items-center justify-between gap-3 border-t border-slate-100 py-2 text-sm">
                    <div>
                      <div><span className="font-medium">{row.name}</span> <Badge tone={row.tone}>{row.status}</Badge>{row.stale ? <span className="ml-2 text-xs text-amber-700">舊觀測</span> : null}</div>
                      <div className="text-xs text-slate-500">
                        {gpu?.mem_total_mb ? `GPU ${Math.round(gpu.mem_total_mb / 1024)} GB · ` : ""}
                        {row.config?.port != null ? `SSH :${row.config.port} · ` : ""}
                        {row.live?.updated_at ? `最近觀測 ${formatTime(row.live.updated_at)}` : "尚無觀測"}
                      </div>
                    </div>
                    <Link className="shrink-0 text-sky-700 underline" to={`/compute?server=${encodeURIComponent(row.name)}`}>查看 Compute</Link>
                  </div>
                );
              })}
            </SectionState>
            {compute.length === 0 && !configs.isLoading && !live.isLoading && !configs.error && !live.error ? <Link className="mt-2 inline-block text-sm text-sky-700 underline" to="/compute">Add Compute</Link> : null}
          </Card>
        </section>
        <section aria-labelledby="recent-activity"><Card><CardTitle id="recent-activity">近期活動</CardTitle><SectionState queries={[events]} empty={(events.data ?? []).length === 0}>{(events.data ?? []).slice(0, 8).map((record, index) => <div key={record.event_id ?? index} className="border-t border-slate-100 py-2 text-sm"><div>{describeActivity(record) || "未分類活動"}</div><div className="text-xs text-slate-500">{formatTime(record.ts)} · {record.result ?? "結果未提供"}</div></div>)}</SectionState><Link className="mt-2 inline-block text-sm text-sky-700 underline" to="/activity">查看全部活動</Link></Card></section>
      </div>
    </main>
  );
}
