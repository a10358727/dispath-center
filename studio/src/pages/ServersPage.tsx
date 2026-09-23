import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "@/api/client";
import { useActiveJobs, useIdleSummary, useLiveServers, useRunners, useServerConfigs, useServerOccupancy } from "@/api/hooks";
import type { Approval, LiveServer, ServerConfig } from "@/api/types";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { AddComputeWizard } from "@/features/compute/AddComputeWizard";
import { HostIdentityPanel } from "@/features/compute/HostIdentityPanel";

type ServerConfigRow = ServerConfig;

function ServerAdmin({ createRequest = 0 }: { createRequest?: number }) {
  const client = useQueryClient();
  const configs = useServerConfigs();
  const [editing, setEditing] = useState<ServerConfigRow | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [deleteApproval, setDeleteApproval] = useState<Approval | null>(null);
  const [adding, setAdding] = useState(false);
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["server-configs"] });
    void client.invalidateQueries({ queryKey: ["live-servers"] });
  };
  const startEdit = (row: ServerConfigRow | null) => {
    setEditing(row ?? { name: "" });
    setForm({
      name: row?.name ?? "",
      host: row?.host ?? "",
      user: row?.user ?? "",
      key: row?.key ?? "",
      port: row?.port != null ? String(row.port) : "22",
      tags: (row?.tags ?? []).join(","),
      project_roots: (row?.project_roots ?? []).join(","),
      dataset_roots: (row?.dataset_roots ?? []).join(","),
      note: row?.note ?? "",
    });
    setMessage(null);
  };
  const payload = () => ({
    name: form.name.trim(),
    host: form.host.trim(),
    user: form.user.trim(),
    key: form.key.trim(),
    port: Number.parseInt(form.port || "22", 10),
    tags: form.tags.split(",").map((tag) => tag.trim()).filter(Boolean),
    project_roots: form.project_roots.split(",").map((item) => item.trim()).filter(Boolean),
    dataset_roots: form.dataset_roots.split(",").map((item) => item.trim()).filter(Boolean),
    enabled: true,
    note: form.note.trim() || null,
  });
  const save = useMutation({
    mutationFn: async () => {
      if (editing && editing.name) {
        const updates: Record<string, unknown> = { ...payload() };
        delete updates.name;
        return api(`/api/v2/server-configs/${encodeURIComponent(editing.name)}/update`, { method: "POST", json: { name: editing.name, updates } });
      }
      return api("/api/v2/server-configs", { method: "POST", json: payload() });
    },
    onSuccess: () => {
      setMessage("已儲存（直接生效：寫入 servers.yaml＋備份＋熱重載，稽核已記錄）");
      setEditing(null);
      refresh();
    },
  });
  const testSsh = useMutation({
    mutationFn: async (row: ServerConfigRow) => api<{ ok: boolean; errors?: string[]; detail?: string }>("/api/v2/server-configs/test-ssh", { method: "POST", json: row }),
    onSuccess: (result, row: ServerConfigRow) => setMessage(result.ok ? `${row.name}：連線正常` : `${row.name}：連線失敗——${(result.errors ?? [result.detail ?? "未知原因"]).join("；")}`),
  });
  const preflight = useMutation({
    mutationFn: (name: string) => api<Record<string, unknown>>(`/api/v2/server-configs/${encodeURIComponent(name)}/attempt-preflight`, { method: "POST", json: {} }),
    onSuccess: (result, name: string) => setMessage(`${name} 預檢：${JSON.stringify(result).slice(0, 200)}`),
  });
  const toggle = useMutation({
    mutationFn: async (row: ServerConfigRow) => {
      if (row.enabled === false) {
        return api(`/api/v2/server-configs/${encodeURIComponent(row.name)}/update`, { method: "POST", json: { name: row.name, updates: { enabled: true } } });
      }
      return api(`/api/v2/server-configs/${encodeURIComponent(row.name)}/disable`, { method: "POST", json: { name: row.name } });
    },
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: async (name: string) => (await api<{ approval: Approval }>("/api/v2/server-configs/delete-requests", { method: "POST", json: { name } })).approval,
    onSuccess: (card) => setDeleteApproval(card),
  });
  const busy = save.isPending || testSsh.isPending || preflight.isPending || toggle.isPending || remove.isPending;
  const error = [save, testSsh, preflight, toggle, remove].map((mutation) => mutation.error).find(Boolean) as Error | undefined;
  const fields: [string, string][] = [["name", "名稱"], ["host", "host"], ["user", "user"], ["key", "key 路徑"], ["port", "port"], ["tags", "tags（逗號）"], ["project_roots", "project roots（逗號）"], ["dataset_roots", "dataset roots（逗號）"], ["note", "備註"]];
  useEffect(() => {
    if (createRequest > 0) setAdding(true);
  }, [createRequest]);
  return (
    <Card className="space-y-2">
      <CardTitle className="flex items-center justify-between">
        機器管理（新增/修改直接生效；刪除出核准卡）
        <Button onClick={() => setAdding(true)}>＋ 新增運算資源</Button>
      </CardTitle>
      {adding ? <AddComputeWizard existingNames={(configs.data ?? []).map((row) => row.name)} onAdded={refresh} onCancel={() => setAdding(false)} /> : null}
      {message ? <div className="rounded bg-slate-100 px-2 py-1 text-xs">{message}</div> : null}
      {error ? <div className="rounded bg-rose-50 px-2 py-1 text-xs text-rose-800">{error.message}</div> : null}
      {deleteApproval ? <ApprovalCard approval={deleteApproval} onDecided={() => { setDeleteApproval(null); refresh(); }} /> : null}
      {editing ? (
        <div className="grid grid-cols-2 gap-2 rounded border border-slate-200 bg-slate-50 p-2 md:grid-cols-3">
          {fields.map(([key, label]) => (
            <label key={key} className="text-xs">
              <span className="block text-slate-600">{label}</span>
              <input
                className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
                value={form[key] ?? ""}
                disabled={key === "name" && Boolean(editing.name)}
                onChange={(event) => setForm({ ...form, [key]: event.target.value })}
              />
            </label>
          ))}
          <div className="col-span-full flex gap-2">
            <Button variant="primary" disabled={busy || !form.name || !form.host || !form.user || !form.key} onClick={() => save.mutate()}>
              {editing.name ? "儲存修改" : "新增"}
            </Button>
            <Button variant="ghost" onClick={() => setEditing(null)}>取消</Button>
          </div>
        </div>
      ) : null}
      <table className="w-full text-xs">
        <tbody>
          {(configs.data ?? []).map((row) => (
            <tr key={row.name} className="border-t border-slate-100 align-top">
              <td className="px-2 py-1 font-medium">{row.name}</td>
              <td className="px-2 py-1 text-slate-500">{(row as ServerConfigRow).user}@{(row as ServerConfigRow).host}:{(row as ServerConfigRow).port ?? 22}</td>
              <td className="px-2 py-1 text-slate-500">{(row.tags ?? []).join(", ")}</td>
              <td className="px-2 py-1">
                <div className="flex gap-2">
                  <button type="button" className="text-sky-700 underline" disabled={busy} onClick={() => startEdit(row as ServerConfigRow)}>編輯</button>
                  <button type="button" className="text-sky-700 underline" disabled={busy} onClick={() => testSsh.mutate(row as ServerConfigRow)}>測試SSH</button>
                  <button type="button" className="text-sky-700 underline" disabled={busy} onClick={() => preflight.mutate(row.name)}>預檢</button>
                  <button type="button" className="text-amber-700 underline" disabled={busy} onClick={() => toggle.mutate(row as ServerConfigRow)}>
                    {(row as ServerConfigRow).enabled === false ? "啟用" : "停用"}
                  </button>
                  <button type="button" className="text-rose-700 underline" disabled={busy} onClick={() => remove.mutate(row.name)}>刪除…</button>
                </div>
                <div className="mt-2"><HostIdentityPanel name={row.name} identity={row.host_identity} onChanged={refresh} /></div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function GpuBar({ percent }: { percent: number | null | undefined }) {
  if (percent == null) return null;
  return (
    <div className="h-1.5 w-24 overflow-hidden rounded bg-slate-200" title={`GPU ${percent}%`}>
      <div className={percent > 80 ? "h-full bg-rose-500" : percent > 30 ? "h-full bg-amber-500" : "h-full bg-emerald-500"} style={{ width: `${Math.min(100, percent)}%` }} />
    </div>
  );
}

function idleText(seconds: number | null | undefined): string {
  if (seconds == null) return "";
  if (seconds >= 3600) return `已閒置 ${Math.floor(seconds / 3600)} 小時`;
  if (seconds >= 60) return `已閒置 ${Math.floor(seconds / 60)} 分`;
  return "";
}

const FRESH_SECONDS = 120;

export type ComputeProjection = {
  name: string;
  config?: ServerConfig;
  live?: LiveServer;
  status: "已停用" | "已封鎖" | "狀態未知" | "已斷線" | "需要留意" | "已連線";
  tone: "neutral" | "warn" | "bad" | "ok";
  stale: boolean;
};

export function projectCompute(configs: ServerConfig[] = [], liveRows: LiveServer[] = [], freshness: Map<string, number | null> = new Map()): ComputeProjection[] {
  const names = new Set([...configs.map((row) => row.name), ...liveRows.map((row) => row.name)]);
  const configByName = new Map(configs.map((row) => [row.name, row]));
  const liveByName = new Map(liveRows.map((row) => [row.name, row]));
  return [...names].sort().map((name) => {
    const config = configByName.get(name);
    const live = liveByName.get(name);
    const age = freshness.get(name);
    const stale = age != null && age > FRESH_SECONDS;
    if (config?.enabled === false || (!config && live?.enabled === false)) return { name, config, live, status: "已停用", tone: "neutral", stale };
    if (config?.attempt_backend_preflight === "ineligible_non_local_fs") return { name, config, live, status: "已封鎖", tone: "bad", stale };
    if (!live || age == null || !Number.isFinite(age)) return { name, config, live, status: "狀態未知", tone: "neutral", stale: false };
    if (stale) return { name, config, live, status: "狀態未知", tone: "warn", stale: true };
    if (live.online === false) return { name, config, live, status: "已斷線", tone: "bad", stale: false };
    if (live.error) return { name, config, live, status: "需要留意", tone: "warn", stale: false };
    if (live.online === true) return { name, config, live, status: "已連線", tone: "ok", stale: false };
    return { name, config, live, status: "狀態未知", tone: "neutral", stale: false };
  });
}

function formatBytes(value: number | null | undefined): string | null {
  if (value == null) return null;
  return `${Math.round(value / 1024 / 1024 / 1024)} GB`;
}

function capabilityLabel(row: ComputeProjection): string {
  if ((row.config?.devices?.length ?? 0) > 0 || (row.live?.devices && Object.keys(row.live.devices).length > 0)) return "硬體主機";
  if (row.config?.gpu || (row.live?.gpu_count ?? row.live?.gpus?.length ?? 0) > 0) return "GPU 運算資源";
  return "一般運算資源";
}

export function ServersPage() {
  const runners = useRunners();
  const live = useLiveServers();
  const idle = useIdleSummary();
  const occupancy = useServerOccupancy();
  const running = useActiveJobs("running");
  const queued = useActiveJobs("queued");
  const configs = useServerConfigs();
  const [searchParams] = useSearchParams();
  const selectedServer = searchParams.get("server");
  const idleByName = new Map((idle.data ?? []).map((row) => [row.server_name, row]));
  const runnerByServer = new Map((runners.data?.runners ?? []).map((runner) => [runner.server, runner]));
  const [server, setServer] = useState("");
  const [label, setLabel] = useState("");
  const [approval, setApproval] = useState<Approval | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [createRequest, setCreateRequest] = useState(0);
  const rows = projectCompute(configs.data, live.data, new Map((idle.data ?? []).map((row) => [row.server_name, row.freshness_seconds ?? null])));
  const activeJobs = [...(running.data ?? []), ...(queued.data ?? [])];
  const enroll = async () => {
    setError(null);
    try {
      const result = await api<{ approval: Approval }>("/api/v2/agent-runners/enroll-requests", { method: "POST", json: { server, label: label || null } });
      setApproval(result.approval);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };
  return (
    <div className="mx-auto max-w-5xl space-y-4 overflow-y-auto p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div><h1 className="text-lg font-semibold"><span>運算資源</span><span className="ml-1 text-xs font-normal text-slate-400">Compute</span></h1><p className="text-xs text-slate-500">可用機器與目前觀測；已連線只代表最新連線健康，不代表排程 Ready。</p></div>
        <div className="flex gap-2"><Button variant="primary" onClick={() => { setAdvanced(true); setCreateRequest((value) => value + 1); }}>＋ 新增運算資源</Button><Button onClick={() => setAdvanced((value) => !value)}>{advanced ? "隱藏進階設定" : "進階設定"}</Button></div>
      </div>
      {navigator.onLine === false ? <div role="status" className="rounded border border-amber-300 bg-amber-50 p-2 text-sm text-amber-900">瀏覽器目前離線；畫面可能是快取資料，已暫停更新。</div> : null}
      {(live.isLoading || configs.isLoading || idle.isLoading) && rows.length === 0 ? <div className="text-sm text-slate-500">載入 Compute…</div> : null}
      {[live, configs, idle].some((query) => query.error) ? <div role="alert" className="flex items-center gap-2 rounded border border-rose-200 bg-rose-50 p-2 text-sm text-rose-800"><span>{[live, configs, idle].some((query) => (query.error as { status?: number } | null)?.status === 403) ? "沒有權限查看部分 Compute 資料。" : "部分 Compute 資料無法取得；其餘資料仍顯示。"}</span><Button onClick={() => { void live.refetch(); void configs.refetch(); void idle.refetch(); }}>重試</Button></div> : null}
      {[live, configs, idle].some((query) => query.isFetching && query.data) ? <div role="status" className="text-xs text-amber-700">正在更新；目前顯示快取資料。</div> : null}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {rows.map((row) => {
          const server = row.live;
          const runner = runnerByServer.get(row.name);
          const summary = idleByName.get(row.name);
          const counts = occupancy.data?.counts[row.name];
          return (
            <Card key={row.name} id={`compute-${row.name}`} className={selectedServer === row.name ? "space-y-2 border-sky-500 ring-2 ring-sky-100" : "space-y-2"}>
              <CardTitle className="mb-0 flex items-center gap-2">
                {row.name}<Badge tone={row.tone}>{row.status}</Badge>{row.stale ? <span className="text-xs font-normal text-amber-700">舊觀測（超過 2 分鐘）</span> : null}
              </CardTitle>
              <div className="text-xs font-medium text-slate-600">{capabilityLabel(row)}</div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600">
                {(server?.gpus ?? []).map((gpu, index) => (
                  <span key={index} className="flex items-center gap-1">
                    GPU{index} <GpuBar percent={gpu.util_percent} />
                    {gpu.mem_total_mb ? `${Math.round((gpu.mem_used_mb ?? 0) / 1024)}/${Math.round(gpu.mem_total_mb / 1024)}G` : ""}
                  </span>
                ))}
                {counts ? <span>執行 {counts.running}｜排隊 {counts.queued}</span> : null}
                {summary ? <span className="text-emerald-700">{idleText(summary.continuous_idle_seconds)}</span> : null}
                {summary?.gpu_util_p50 != null ? <span>24h GPU p50 {summary.gpu_util_p50}%</span> : null}
                {row.config?.port != null ? <span>SSH :{row.config.port}</span> : null}
                {formatBytes(server?.disk_avail_bytes) ? <span>磁碟可用 {formatBytes(server?.disk_avail_bytes)}</span> : null}
              </div>
              {server?.devices && Object.keys(server.devices).length > 0 ? (
                <div className="flex flex-wrap gap-1">
                  {Object.entries(server.devices).map(([id, presence]) => (
                    <Badge key={id} tone={presence === "present" ? "ok" : presence === "absent" ? "bad" : "neutral"}>
                      {id}：{presence === "present" ? "在場" : presence === "absent" ? "不在場" : "未觀測"}
                    </Badge>
                  ))}
                </div>
              ) : null}
              <p className="text-xs text-slate-500">擁有方式：未提供。租用 GPU、自有伺服器與硬體主機都在此管理。</p>
              {activeJobs.filter((job) => job.server === row.name).slice(0, 3).map((job) => (
                <Link key={job.id} className="block text-xs text-sky-700 underline" to={`/runs?${job.project ? `project=${encodeURIComponent(job.project)}&` : ""}job=${job.id}&tab=jobs`}>
                  查看 {job.status === "running" ? "執行中的" : "排隊中的"} Run · 工作 #{job.id}
                </Link>
              ))}
              <div className="flex items-center gap-1">
                {runner ? (
                  <>
                    <Badge tone={runner.connected ? "ok" : "neutral"}>{runner.connected ? "agent 已連線" : "agent 離線"}</Badge>
                    <Badge tone={stateTone(runner.active ? "ok" : "failed")}>{runner.status}</Badge>
                    <span className="text-xs text-slate-400">runner {runner.id.slice(0, 8)}</span>
                  </>
                ) : (
                  <span className="text-xs text-slate-400">尚無 runner agent（SSH 派工照常）</span>
                )}
              </div>
            </Card>
          );
        })}
      </div>
      {rows.length === 0 && !live.isLoading && !configs.isLoading && !live.error && !configs.error ? <Card><p className="text-sm text-slate-600">尚未加入運算資源。</p><Button className="mt-2" onClick={() => { setAdvanced(true); setCreateRequest((value) => value + 1); }}>新增運算資源</Button></Card> : null}
      {advanced ? <section id="advanced-compute" aria-labelledby="advanced-title" className="space-y-4"><h2 id="advanced-title" className="text-base font-semibold"><span>進階運算資源設定</span><span className="ml-1 text-xs font-normal text-slate-400">Advanced Compute</span></h2><p className="text-xs text-slate-500">設定、連線測試、預檢與 runner 管理。這些操作沿用既有 Server 身分與歷史。</p><ServerAdmin createRequest={createRequest} />
      <Card className="space-y-2">
        <CardTitle>登錄 runner agent</CardTitle>
        {approval ? (
          <ApprovalCard approval={approval} onDecided={() => void runners.refetch()} />
        ) : (
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-sm">
              <span className="block text-slate-600">伺服器名稱（servers.yaml）</span>
              <input className="rounded border border-slate-300 px-2 py-1" value={server} onChange={(event) => setServer(event.target.value)} />
            </label>
            <label className="text-sm">
              <span className="block text-slate-600">標籤（選填）</span>
              <input className="rounded border border-slate-300 px-2 py-1" value={label} onChange={(event) => setLabel(event.target.value)} />
            </label>
            <Button variant="primary" disabled={!server} onClick={() => void enroll()}>
              建立核准卡
            </Button>
            {error ? <span className="text-xs text-rose-700">{error}</span> : null}
          </div>
        )}
        <p className="text-xs text-slate-500">核准後會顯示一次性的登錄憑證；存到 runner 的 <code>~/.dispatch-agent/credential</code>（0600）。runner 只出站連回這裡，工作機不開任何入站埠。</p>
      </Card>
      </section> : null}
    </div>
  );
}
