import { useState } from "react";
import { api } from "@/api/client";
import { useRunners } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";

export function ServersPage() {
  const runners = useRunners();
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
        {(runners.data?.runners ?? []).map((runner) => (
          <Card key={runner.id}>
            <CardTitle className="flex items-center gap-2">
              <span className={runner.connected ? "text-emerald-500" : "text-slate-300"}>●</span>
              {runner.server}
              {runner.label ? <span className="font-normal text-slate-500">· {runner.label}</span> : null}
            </CardTitle>
            <div className="flex gap-1">
              <Badge tone={runner.connected ? "ok" : "neutral"}>{runner.connected ? "agent 已連線" : "agent 離線"}</Badge>
              <Badge tone={stateTone(runner.active ? "ok" : "failed")}>{runner.status}</Badge>
            </div>
            <div className="mt-2 text-xs text-slate-500">runner id {runner.id.slice(0, 8)}</div>
          </Card>
        ))}
      </div>
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
