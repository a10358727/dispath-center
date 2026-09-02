import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import { useServerConfigs } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";

interface Candidate {
  id: number;
  server?: string;
  path?: string;
  name?: string | null;
  status?: string;
  summary?: string | null;
  [key: string]: unknown;
}

function useCandidates(server: string, status: string, q: string) {
  const params = new URLSearchParams();
  if (server) params.set("server", server);
  if (status) params.set("status", status);
  if (q) params.set("q", q);
  return useQuery({
    queryKey: ["inventory-candidates", server, status, q],
    queryFn: () => api<Candidate[]>(`/api/v2/inventory/candidates?${params.toString()}`),
    refetchInterval: 20_000,
  });
}

function ImportForm({ candidate, onDone }: { candidate: Candidate; onDone: () => void }) {
  const [name, setName] = useState(candidate.name ?? "");
  const [command, setCommand] = useState("");
  const [approval, setApproval] = useState<Approval | null>(null);
  const [confirmNow, setConfirmNow] = useState(false);
  const request = useMutation({
    mutationFn: async (confirm: boolean) => {
      setConfirmNow(confirm);
      const result = await api<{ approval: Approval }>(`/api/v2/inventory/candidates/${candidate.id}/import-requests`, {
        method: "POST",
        json: { name: name || undefined, default_command: command || undefined },
      });
      return result.approval;
    },
    onSuccess: (card) => setApproval(card),
  });
  return (
    <div className="mt-2 space-y-2 rounded border border-slate-200 bg-slate-50 p-2">
      {approval ? (
        <ApprovalCard approval={approval} confirmImmediately={confirmNow} onDecided={() => { setApproval(null); setConfirmNow(false); onDone(); }} />
      ) : (
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs">
            <span className="block text-slate-600">專案名稱</span>
            <input className="rounded border border-slate-300 px-2 py-1 text-sm" value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          <label className="text-xs">
            <span className="block text-slate-600">預設指令（選填）</span>
            <input className="w-72 rounded border border-slate-300 px-2 py-1 text-sm" value={command} onChange={(event) => setCommand(event.target.value)} placeholder="python train.py" />
          </label>
          <Button disabled={request.isPending} onClick={() => request.mutate(false)}>
            建立匯入卡
          </Button>
          <Button variant="primary" disabled={request.isPending} onClick={() => request.mutate(true)}>
            確認並匯入
          </Button>
          {request.error ? <span className="text-xs text-rose-700">{(request.error as Error).message}</span> : null}
        </div>
      )}
    </div>
  );
}

export function ImportPage() {
  const configs = useServerConfigs();
  const client = useQueryClient();
  //: 整頓 U7: scan one machine by default — `server: "all"` fans out one
  //: inventory_scan card per enabled server (app/approvals.py).
  const [server, setServer] = useState("");
  const effectiveServer = server || configs.data?.[0]?.name || "";
  const [scanConfirm, setScanConfirm] = useState(false);
  const [status, setStatus] = useState("pending");
  const [q, setQ] = useState("");
  const candidates = useCandidates(server, status, q);
  const [openImport, setOpenImport] = useState<number | null>(null);
  const [scanApprovals, setScanApprovals] = useState<Approval[]>([]);
  const [manual, setManual] = useState({ server: "", path: "", name: "" });

  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["inventory-candidates"] });
    void client.invalidateQueries({ queryKey: ["projects"] });
  };
  const scan = useMutation({
    mutationFn: async ({ target, confirm }: { target: string; confirm: boolean }) => {
      setScanConfirm(confirm);
      const result = await api<{ approval?: Approval; approvals?: Approval[] }>("/api/v2/inventory/scan-requests", {
        method: "POST",
        json: { server: target },
      });
      return result.approvals ?? (result.approval ? [result.approval] : []);
    },
    onSuccess: (cards) => setScanApprovals(cards),
  });
  const [ignoreConfirm, setIgnoreConfirm] = useState(false);
  const ignore = useMutation({
    mutationFn: async ({ candidateId, confirm }: { candidateId: number; confirm: boolean }) => {
      setIgnoreConfirm(confirm);
      return (await api<{ approval: Approval }>(`/api/v2/inventory/candidates/${candidateId}/ignore-requests`, { method: "POST", json: {} })).approval;
    },
  });
  const ignoreNested = useMutation({
    mutationFn: async () => (await api<{ approval: Approval }>("/api/v2/inventory/candidates/ignore-nested-requests", { method: "POST", json: {} })).approval,
    onSuccess: (card) => { setIgnoreConfirm(true); setIgnoreApproval(card); },
  });
  const [ignoreApproval, setIgnoreApproval] = useState<Approval | null>(null);
  const manualAdd = useMutation({
    mutationFn: () => api("/api/v2/inventory/candidates", { method: "POST", json: { server: manual.server, path: manual.path, name: manual.name || undefined } }),
    onSuccess: () => {
      setManual({ server: "", path: "", name: "" });
      refresh();
    },
  });

  return (
    <div className="min-h-0 space-y-4 overflow-y-auto p-6">
      <div className="flex items-center gap-3">
        <Link to="/projects" className="text-xs text-slate-500 hover:underline">← 專案</Link>
        <h1 className="text-lg font-semibold">匯入專案</h1>
        <select className="rounded border border-slate-300 p-1 text-sm" value={effectiveServer} onChange={(event) => setServer(event.target.value)}>
          {(configs.data ?? []).map((config) => (
            <option key={config.name} value={config.name}>{config.name}</option>
          ))}
        </select>
        <select className="rounded border border-slate-300 p-1 text-sm" value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="pending">pending</option>
          <option value="imported">imported</option>
          <option value="ignored">ignored</option>
          <option value="">全部</option>
        </select>
        <input className="w-56 rounded border border-slate-300 px-2 py-1 text-sm" placeholder="搜尋路徑/名稱…" value={q} onChange={(event) => setQ(event.target.value)} />
        <Button variant="primary" disabled={scan.isPending || !effectiveServer} onClick={() => scan.mutate({ target: effectiveServer, confirm: true })} title="SSH 唯讀掃描候選專案；建卡後由你本人立即核准">
          掃描 {effectiveServer}
        </Button>
        <Button variant="ghost" disabled={scan.isPending} onClick={() => scan.mutate({ target: "all", confirm: false })} title={`會建立 ${(configs.data ?? []).length} 張卡（每台一張），逐張核准`}>
          掃描全部…
        </Button>
        <Button variant="ghost" disabled={ignoreNested.isPending} onClick={() => ignoreNested.mutate()} title="一張卡忽略所有巢狀候選（子目錄重複的候選）">
          忽略巢狀候選
        </Button>
        {scan.error ? <span className="text-xs text-rose-700">{(scan.error as Error).message}</span> : null}
        {ignoreNested.error ? <span className="text-xs text-rose-700">{(ignoreNested.error as Error).message}</span> : null}
      </div>
      {scanApprovals.map((card) => (
        <ApprovalCard key={card.id} approval={card} confirmImmediately={scanConfirm} onDecided={() => { setScanApprovals(scanApprovals.filter((item) => item.id !== card.id)); refresh(); }} />
      ))}
      {ignoreApproval ? <ApprovalCard approval={ignoreApproval} confirmImmediately={ignoreConfirm} onDecided={() => { setIgnoreApproval(null); setIgnoreConfirm(false); refresh(); }} /> : null}
      <div className="space-y-2">
        {(candidates.data ?? []).map((candidate) => (
          <Card key={candidate.id} className="py-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone={candidate.status === "pending" ? "warn" : "neutral"}>{candidate.status}</Badge>
              <span className="font-medium">{candidate.name ?? "（未命名）"}</span>
              <span className="font-mono text-xs text-slate-500">{candidate.server}:{candidate.path}</span>
              {candidate.summary ? <span className="text-xs text-slate-500">{String(candidate.summary)}</span> : null}
              {candidate.status === "pending" ? (
                <span className="ml-auto flex gap-2">
                  <Button onClick={() => setOpenImport(openImport === candidate.id ? null : candidate.id)}>匯入…</Button>
                  <Button variant="ghost" disabled={ignore.isPending} onClick={() => ignore.mutateAsync({ candidateId: candidate.id, confirm: true }).then(setIgnoreApproval)}>
                    忽略
                  </Button>
                </span>
              ) : null}
            </div>
            {openImport === candidate.id ? <ImportForm candidate={candidate} onDone={() => { setOpenImport(null); refresh(); }} /> : null}
          </Card>
        ))}
        {(candidates.data ?? []).length === 0 && !candidates.isLoading ? (
          <div className="text-sm text-slate-400">沒有候選專案——按「掃描」以 SSH 唯讀方式搜尋，或用下方手動加入。</div>
        ) : null}
      </div>
      <Card className="space-y-2">
        <CardTitle>手動加入候選</CardTitle>
        <div className="flex flex-wrap items-end gap-2">
          <select className="rounded border border-slate-300 p-1.5 text-sm" value={manual.server} onChange={(event) => setManual({ ...manual, server: event.target.value })}>
            <option value="">伺服器…</option>
            {(configs.data ?? []).map((config) => (
              <option key={config.name} value={config.name}>{config.name}</option>
            ))}
          </select>
          <input className="w-72 rounded border border-slate-300 px-2 py-1 text-sm" placeholder="路徑（伺服器上）" value={manual.path} onChange={(event) => setManual({ ...manual, path: event.target.value })} />
          <input className="rounded border border-slate-300 px-2 py-1 text-sm" placeholder="名稱（選填）" value={manual.name} onChange={(event) => setManual({ ...manual, name: event.target.value })} />
          <Button variant="primary" disabled={!manual.server || !manual.path || manualAdd.isPending} onClick={() => manualAdd.mutate()}>
            加入
          </Button>
          {manualAdd.error ? <span className="text-xs text-rose-700">{(manualAdd.error as Error).message}</span> : null}
        </div>
      </Card>
    </div>
  );
}
