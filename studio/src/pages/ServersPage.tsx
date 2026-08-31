import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useIdleSummary, useLiveServers, useRunners, useServerConfigs, useServerOccupancy } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";

interface ServerConfigRow {
  name: string;
  host?: string;
  user?: string;
  key?: string;
  port?: number;
  tags?: string[];
  enabled?: boolean;
  note?: string | null;
  project_roots?: string[];
  dataset_roots?: string[];
  [key: string]: unknown;
}

function ServerAdmin() {
  const client = useQueryClient();
  const configs = useServerConfigs();
  const [editing, setEditing] = useState<ServerConfigRow | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [deleteApproval, setDeleteApproval] = useState<Approval | null>(null);
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
    onSuccess: (result, row: ServerConfigRow) => setMessage(result.ok ? `${row.name}: SSH OK` : `${row.name}: ${(result.errors ?? [result.detail ?? "失敗"]).join("；")}`),
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
  return (
    <Card className="space-y-2">
      <CardTitle className="flex items-center justify-between">
        機器管理（新增/修改直接生效；刪除出核准卡）
        <Button onClick={() => startEdit(null)}>＋ 新增機器</Button>
      </CardTitle>
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
            <tr key={row.name} className="border-t border-slate-100">
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

export function ServersPage() {
  const runners = useRunners();
  const live = useLiveServers();
  const idle = useIdleSummary();
  const occupancy = useServerOccupancy();
  const idleByName = new Map((idle.data ?? []).map((row) => [row.server_name, row]));
  const runnerByServer = new Map((runners.data?.runners ?? []).map((runner) => [runner.server, runner]));
  const [server, setServer] = useState("");
  const [label, setLabel] = useState("");
  const [approval, setApproval] = useState<Approval | null>(null);
  const [error, setError] = useState<string | null>(null);
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
    <div className="mx-auto max-w-4xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">伺服器與硬體</h1>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {(live.data ?? []).map((server) => {
          const runner = runnerByServer.get(server.name);
          const summary = idleByName.get(server.name);
          const counts = occupancy.data?.counts[server.name];
          return (
            <Card key={server.name} className="space-y-2">
              <CardTitle className="mb-0 flex items-center gap-2">
                <span className={server.online ? "text-emerald-500" : "text-slate-300"}>●</span>
                {server.name}
                {server.enabled === false ? <Badge tone="bad">已停用</Badge> : null}
              </CardTitle>
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600">
                {(server.gpus ?? []).map((gpu, index) => (
                  <span key={index} className="flex items-center gap-1">
                    GPU{index} <GpuBar percent={gpu.util_percent} />
                    {gpu.mem_total_mb ? `${Math.round((gpu.mem_used_mb ?? 0) / 1024)}/${Math.round(gpu.mem_total_mb / 1024)}G` : ""}
                  </span>
                ))}
                {counts ? <span>執行 {counts.running}｜排隊 {counts.queued}</span> : null}
                {summary ? <span className="text-emerald-700">{idleText(summary.continuous_idle_seconds)}</span> : null}
                {summary?.gpu_util_p50 != null ? <span>24h GPU p50 {summary.gpu_util_p50}%</span> : null}
              </div>
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
      <ServerAdmin />
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
    </div>
  );
}
