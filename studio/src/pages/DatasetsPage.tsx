import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useProjects } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { formatTime } from "@/lib";

interface DatasetRow {
  name: string;
  version: string;
  size_bytes?: number;
  source_path?: string;
  created_at?: string;
  file_count?: number;
  sync_mode?: string;
  card?: { description?: string; method?: string; derived_from?: string | null } | null;
}

function gb(bytes: number | undefined): string {
  if (!bytes) return "—";
  return bytes >= 1 << 30 ? `${(bytes / (1 << 30)).toFixed(1)} GB` : `${Math.round(bytes / (1 << 20))} MB`;
}

function CardEditor({ dataset, onDone }: { dataset: DatasetRow; onDone: () => void }) {
  const [description, setDescription] = useState(dataset.card?.description ?? "");
  const [method, setMethod] = useState(dataset.card?.method ?? "");
  const save = useMutation({
    mutationFn: () =>
      api(`/api/v2/legacy-datasets/${encodeURIComponent(dataset.name)}/${encodeURIComponent(dataset.version)}/card`, {
        method: "PATCH",
        json: { description, method, derived_from: dataset.card?.derived_from ?? null },
      }),
    onSuccess: onDone,
  });
  return (
    <div className="mt-2 space-y-2 rounded border border-slate-200 bg-slate-50 p-2 text-sm">
      <label className="block">
        <span className="text-xs text-slate-600">描述</span>
        <textarea className="w-full rounded border border-slate-300 p-1.5" rows={2} value={description} onChange={(event) => setDescription(event.target.value)} />
      </label>
      <label className="block">
        <span className="text-xs text-slate-600">取得/處理方式</span>
        <textarea className="w-full rounded border border-slate-300 p-1.5" rows={2} value={method} onChange={(event) => setMethod(event.target.value)} />
      </label>
      <div className="flex gap-2">
        <Button variant="primary" disabled={!description || !method || save.isPending} onClick={() => save.mutate()}>儲存 data card</Button>
        {save.error ? <span className="text-xs text-rose-700">{(save.error as Error).message}</span> : null}
      </div>
    </div>
  );
}

function PublishForm({ dataset, onDone }: { dataset: DatasetRow; onDone: () => void }) {
  const projects = useProjects();
  const [projectId, setProjectId] = useState("");
  const [assetName, setAssetName] = useState(dataset.name);
  const [approval, setApproval] = useState<Approval | null>(null);
  const body = () => ({
    source: { kind: "local_path", path: dataset.source_path },
    asset: {
      name: assetName,
      description: dataset.card?.description ?? assetName,
      data_card: {
        collection_method: dataset.card?.method ?? "見 legacy data card",
        processing_method: dataset.card?.method ?? "見 legacy data card",
        counts: [],
      },
    },
  });
  const publish = useMutation({
    mutationFn: async () => {
      const preview = await api<{ preview_digest?: string; payload_digest?: string }>(
        `/api/v2/projects/${projectId}/dataset-publish-previews`,
        { method: "POST", json: body() },
      );
      const digest = preview.preview_digest ?? preview.payload_digest;
      const result = await api<{ approval: Approval }>(`/api/v2/projects/${projectId}/dataset-publish-requests`, {
        method: "POST",
        json: { ...body(), expected_preview_digest: digest },
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      return result.approval;
    },
    onSuccess: (card) => setApproval(card),
  });
  return (
    <div className="mt-2 space-y-2 rounded border border-slate-200 bg-slate-50 p-2 text-sm">
      {approval ? (
        <ApprovalCard approval={approval} onDecided={() => { setApproval(null); onDone(); }} />
      ) : (
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs">
            <span className="block text-slate-600">發布到專案</span>
            <select className="rounded border border-slate-300 p-1.5 text-sm" value={projectId} onChange={(event) => setProjectId(event.target.value)}>
              <option value="">選專案…</option>
              {(projects.data ?? []).filter((project) => project.id).map((project) => (
                <option key={project.name} value={project.id}>{project.name}</option>
              ))}
            </select>
          </label>
          <label className="text-xs">
            <span className="block text-slate-600">Asset 名稱</span>
            <input className="rounded border border-slate-300 px-2 py-1 text-sm" value={assetName} onChange={(event) => setAssetName(event.target.value)} />
          </label>
          <Button variant="primary" disabled={!projectId || !assetName || publish.isPending} onClick={() => publish.mutate()}>
            預覽並建卡
          </Button>
          {publish.error ? <span className="text-xs text-rose-700">{(publish.error as Error).message}</span> : null}
        </div>
      )}
    </div>
  );
}

export function DatasetsPage() {
  const client = useQueryClient();
  const datasets = useQuery({
    queryKey: ["legacy-datasets"],
    queryFn: () => api<DatasetRow[]>("/api/v2/legacy-datasets"),
    refetchInterval: 30_000,
  });
  const [open, setOpen] = useState<string | null>(null);
  const [mode, setMode] = useState<"card" | "publish" | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", version: "v1", source_path: "", description: "", method: "" });
  const create = useMutation({
    mutationFn: () => api("/api/v2/legacy-datasets", { method: "POST", json: form }),
    onSuccess: () => {
      setCreating(false);
      setForm({ name: "", version: "v1", source_path: "", description: "", method: "" });
      void client.invalidateQueries({ queryKey: ["legacy-datasets"] });
    },
  });
  const refresh = () => void client.invalidateQueries({ queryKey: ["legacy-datasets"] });
  return (
    <div className="min-h-0 space-y-3 overflow-y-auto p-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">資料集</h1>
        <Button onClick={() => setCreating(!creating)}>＋ 登記資料集</Button>
        <span className="text-xs text-slate-500">登記＝掃描 Server A 本機路徑產生 manifest（直接生效）；發布＝建立一張「發布資料集」核准卡，成為治理面的不可變快照。</span>
      </div>
      {creating ? (
        <Card className="space-y-2">
          <CardTitle>登記新資料集（Server A 本機路徑）</CardTitle>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
            {([["name", "名稱"], ["version", "版本"], ["source_path", "Server A 路徑"], ["description", "描述"], ["method", "取得方式"]] as const).map(([key, label]) => (
              <label key={key} className="text-xs">
                <span className="block text-slate-600">{label}</span>
                <input className="w-full rounded border border-slate-300 px-2 py-1 text-sm" value={form[key]} onChange={(event) => setForm({ ...form, [key]: event.target.value })} />
              </label>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="primary" disabled={!form.name || !form.version || !form.source_path || !form.description || !form.method || create.isPending} onClick={() => create.mutate()}>
              登記（掃描 manifest）
            </Button>
            {create.error ? <span className="text-xs text-rose-700">{(create.error as Error).message}</span> : null}
          </div>
        </Card>
      ) : null}
      <div className="space-y-2">
        {(datasets.data ?? []).map((dataset) => {
          const key = `${dataset.name}@${dataset.version}`;
          return (
            <Card key={key} className="py-3">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="font-medium">{dataset.name}</span>
                <Badge tone="neutral">{dataset.version}</Badge>
                <span className="text-xs text-slate-500">{gb(dataset.size_bytes)} · {dataset.file_count ?? "?"} 檔 · {formatTime(dataset.created_at)}</span>
                <span className="font-mono text-xs text-slate-400">{dataset.source_path}</span>
                <span className="ml-auto flex gap-2 text-xs">
                  <button type="button" className="text-sky-700 underline" onClick={() => { setOpen(open === key && mode === "card" ? null : key); setMode("card"); }}>data card</button>
                  <button type="button" className="text-sky-700 underline" onClick={() => { setOpen(open === key && mode === "publish" ? null : key); setMode("publish"); }}>發布…</button>
                </span>
              </div>
              {dataset.card?.description ? <div className="mt-1 text-xs text-slate-500">{dataset.card.description}</div> : null}
              {open === key && mode === "card" ? <CardEditor dataset={dataset} onDone={() => { setOpen(null); refresh(); }} /> : null}
              {open === key && mode === "publish" ? <PublishForm dataset={dataset} onDone={() => setOpen(null)} /> : null}
            </Card>
          );
        })}
        {(datasets.data ?? []).length === 0 && !datasets.isLoading ? <div className="text-sm text-slate-400">還沒有登記任何資料集。</div> : null}
      </div>
    </div>
  );
}
