import { useCostSummary, useMe, useRunners } from "@/api/hooks";
import { Card, CardTitle } from "@/components/ui/card";

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
        <CardTitle>功能旗標</CardTitle>
        <div className="text-sm">AGENT_RUNTIME_V3_ENABLED：{runners.data ? (runners.data.enabled ? "開" : "關") : "…"}</div>
      </Card>
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
      <Card>
        <CardTitle>舊介面</CardTitle>
        <a className="text-sm text-sky-700 underline" href="/">
          開啟舊 Workspace
        </a>
      </Card>
    </div>
  );
}
