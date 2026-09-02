import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useCostSummary, useMe, useRunners } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";

function AiProvidersCard() {
  const client = useQueryClient();
  const status = useQuery({
    queryKey: ["ai-providers"],
    queryFn: () => api<{ assistant_brain?: { mode?: string; reason?: string }; anthropic?: { package_installed?: boolean; key_configured?: boolean }; vllm?: { configured?: boolean } }>("/api/v2/ai-providers/status"),
  });
  const usage = useQuery({
    queryKey: ["ai-usage"],
    queryFn: () => api<{ days: number; totals?: { turns?: number; input_tokens?: number; output_tokens?: number } }>("/api/v2/ai-providers/usage?days=30"),
  });
  const [keyDraft, setKeyDraft] = useState("");
  const setKey = useMutation({
    mutationFn: (value: string) => api("/api/v2/ai-providers/anthropic-key", { method: "POST", json: { api_key: value } }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["ai-providers"] }),
  });
  const clearKey = useMutation({
    mutationFn: () => api("/api/v2/ai-providers/anthropic-key", { method: "DELETE" }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["ai-providers"] }),
  });
  return (
    <Card>
      <CardTitle>AI providers</CardTitle>
      <div className="space-y-2 text-sm">
        <div>vLLM：{status.data?.vllm?.configured ? "已設定（不代表可達）" : "未設定"}</div>
        <div className="flex flex-wrap items-center gap-2">
          <span>Anthropic API key（對話面板用）：{status.data?.anthropic?.key_configured ? "已設定" : "未設定"}</span>
          <input
            type="password"
            className="w-64 rounded border border-slate-300 px-2 py-1 text-sm"
            placeholder="sk-ant-…"
            value={keyDraft}
            onChange={(event) => setKeyDraft(event.target.value)}
          />
          <Button
            disabled={!keyDraft || setKey.isPending}
            onClick={() => {
              const value = keyDraft;
              setKeyDraft("");
              setKey.mutate(value);
            }}
          >
            設定
          </Button>
          <Button variant="ghost" disabled={clearKey.isPending} onClick={() => clearKey.mutate()}>
            清除
          </Button>
          {(setKey.error ?? clearKey.error) ? <span className="text-xs text-rose-700">{((setKey.error ?? clearKey.error) as Error).message}</span> : null}
        </div>
        <div className="text-xs text-slate-500">
          近 30 天助手用量：{usage.data?.totals?.turns ?? 0} 回合，in {usage.data?.totals?.input_tokens ?? 0} / out {usage.data?.totals?.output_tokens ?? 0} tokens。
          Studio session 走 runner 的 Claude 訂閱，不經這裡。
        </div>
      </div>
    </Card>
  );
}

export function SettingsPage() {
  const me = useMe();
  const runners = useRunners();
  const cost = useCostSummary();
  return (
    <div className="max-w-2xl space-y-3 p-6">
      <h1 className="text-lg font-semibold">設定</h1>
      <Card>
        <CardTitle>身分</CardTitle>
        <div className="text-sm">
          {me.data?.actor?.display_name ?? me.data?.actor?.id ?? "未登入"} · {me.data?.authentication_method ?? ""}
          {me.data?.actor?.platform_admin ? " · platform admin" : ""}
        </div>
      </Card>
      <Card>
        <CardTitle>Agent runner</CardTitle>
        <div className="text-sm">{runners.data ? (runners.data.enabled ? "已啟用" : "未啟用（在 Server A 設定中開啟）") : "…"}</div>
      </Card>
      <AiProvidersCard />
      <Card>
        <CardTitle>AI 成本（SDK 回報值）</CardTitle>
        <div className="space-y-1 text-sm">
          {(cost.data?.projects ?? []).map((row) => (
            <div key={row.project} className="flex justify-between">
              <span>{row.project}（{row.sessions} sessions）</span>
              <span>${row.cost_usd.toFixed(4)}</span>
            </div>
          ))}
          <div className="flex justify-between border-t border-slate-200 pt-1 font-semibold">
            <span>合計</span>
            <span>${(cost.data?.total_cost_usd ?? 0).toFixed(4)}</span>
          </div>
        </div>
      </Card>
    </div>
  );
}
