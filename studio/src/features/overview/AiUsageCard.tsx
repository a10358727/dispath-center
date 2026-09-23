import { useEffect, useState } from "react";
import { ApiError } from "@/api/client";
import { useAiProviderQuota } from "@/api/hooks";
import type { AiAccountQuota, AiContextUsage, AiEstimatedCost, AiLocalUsage, AiProviderUsage, AiQuotaWindow } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { formatTime } from "@/lib";

const REASON_LABELS: Record<string, string> = {
  requires_credentialed_api: "需要帳戶憑證 API，本版本未啟用",
  home_missing: "找不到本機資料目錄",
  no_session_files: "沒有本機 session 檔案",
  no_rate_limit_events: "session 檔案沒有額度事件",
  no_context_events: "沒有上下文事件",
  context_window_unknown: "視窗未知",
  scan_error: "讀取本機資料失敗",
  no_pricing_source: "無定價來源",
};

const WINDOW_LABELS: Record<string, string> = {
  "5h": "5 小時",
  weekly: "每週",
};

function reasonLabel(reason: string | null | undefined): string {
  if (!reason) return "原因未提供";
  return REASON_LABELS[reason] ?? reason;
}

function windowLabel(label: string): string {
  return WINDOW_LABELS[label] ?? label;
}

function formatCompactTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 100_000) return `${(value / 1_000).toFixed(1)}k`;
  return value.toLocaleString("en-US");
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "未知錯誤";
}

/** Countdown label from `resets_at` compared against `now` (both epoch ms). */
function countdownLabel(resetsAt: string, now: number): string {
  const target = new Date(resetsAt).getTime();
  if (Number.isNaN(target)) return "未知";
  const deltaMs = target - now;
  if (deltaMs <= 0) return "已到期";
  const totalMinutes = Math.floor(deltaMs / 60_000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours > 0) return `${hours} 小時 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分`;
  return "不到 1 分";
}

function AccountQuotaRow({ label, quota, now }: { label: string; quota: AiAccountQuota; now: number }) {
  if (quota.availability === "unavailable") {
    return (
      <div>
        <div className="text-xs font-medium text-slate-500">帳戶額度</div>
        <div className="text-sm text-slate-700">無法取得：{reasonLabel(quota.reason)}</div>
      </div>
    );
  }
  return (
    <div>
      <div className="flex items-center gap-2 text-xs font-medium text-slate-500">
        帳戶額度
        {quota.availability === "stale" ? <Badge tone="warn">舊觀測</Badge> : null}
        {quota.plan_type ? <Badge tone="neutral">方案 {quota.plan_type}</Badge> : null}
      </div>
      {quota.availability === "stale" ? <div className="text-xs text-slate-500">最後觀測 {formatTime(quota.observed_at)}</div> : null}
      {quota.windows.length === 0 ? (
        <div className="text-sm text-slate-700">沒有回報中的額度視窗</div>
      ) : (
        <div className="space-y-2">
          {quota.windows.map((window) => (
            <QuotaWindowRow key={window.id} providerLabel={label} window={window} now={now} />
          ))}
        </div>
      )}
    </div>
  );
}

function QuotaWindowRow({ providerLabel, window, now }: { providerLabel: string; window: AiQuotaWindow; now: number }) {
  const displayLabel = windowLabel(window.label);
  const pct = window.used_percent;
  let resetText: string;
  if (window.state === "current") {
    resetText = window.resets_at ? `重置於 ${countdownLabel(window.resets_at, now)}（${formatTime(window.resets_at)}）` : "重置時間未知";
  } else if (window.state === "stale") {
    resetText = `舊觀測 · 重置於 ${formatTime(window.resets_at)}`;
  } else {
    resetText = `視窗已重置（觀測於 ${formatTime(window.resets_at)}）`;
  }
  return (
    <div key={window.id} className="text-sm">
      <div className="flex items-center justify-between text-xs text-slate-600">
        <span>{displayLabel}</span>
        <span>{pct == null ? "未知" : `${pct}%`} 已用</span>
      </div>
      <div
        role="progressbar"
        aria-label={`${providerLabel} ${displayLabel} 使用率`}
        aria-valuenow={pct ?? undefined}
        aria-valuemin={0}
        aria-valuemax={100}
        className="mt-1 h-2 w-full overflow-hidden rounded bg-slate-100"
      >
        <div className="h-full rounded bg-sky-500" style={{ width: `${pct ?? 0}%` }} />
      </div>
      <div className="mt-0.5 text-xs text-slate-500">{resetText}</div>
    </div>
  );
}

function LocalUsageRow({ usage }: { usage: AiLocalUsage }) {
  if (usage.availability === "unavailable") {
    return (
      <div>
        <div className="text-xs font-medium text-slate-500">今日 tokens</div>
        <div className="text-sm text-slate-700">無法取得：{reasonLabel(usage.reason)}</div>
      </div>
    );
  }
  const today = usage.today;
  if (!today || today.total_tokens === 0) {
    return (
      <div>
        <div className="text-xs font-medium text-slate-500">今日 tokens</div>
        <div className="text-sm text-slate-700">今日尚無使用紀錄</div>
      </div>
    );
  }
  return (
    <div>
      <div className="text-xs font-medium text-slate-500">今日 tokens</div>
      <div className="text-sm text-slate-900" title={today.total_tokens.toLocaleString("en-US")}>
        {formatCompactTokens(today.total_tokens)}
      </div>
      <div className="text-xs text-slate-500">
        輸入 {formatCompactTokens(today.input_tokens)} · 輸出 {formatCompactTokens(today.output_tokens)} · {usage.sessions_today} 個 session
      </div>
      {usage.scan.truncated ? <div className="text-xs text-amber-700">掃描已截斷，數字可能偏低</div> : null}
    </div>
  );
}

function ContextUsageRow({ context }: { context: AiContextUsage }) {
  if (context.availability === "unavailable") {
    return (
      <div>
        <div className="text-xs font-medium text-slate-500">上下文</div>
        <div className="text-sm text-slate-700">無資料</div>
      </div>
    );
  }
  if (context.availability === "partial") {
    return (
      <div>
        <div className="text-xs font-medium text-slate-500">上下文</div>
        <div className="text-sm text-slate-700">
          上次回合 {context.used_tokens != null ? context.used_tokens.toLocaleString("en-US") : "未知"} tokens（視窗未知）{context.model ? ` · ${context.model}` : ""}
        </div>
      </div>
    );
  }
  const pct = context.used_percent != null ? context.used_percent.toFixed(1) : "未知";
  return (
    <div>
      <div className="text-xs font-medium text-slate-500">上下文</div>
      <div className="text-sm text-slate-700">
        {context.used_tokens != null ? context.used_tokens.toLocaleString("en-US") : "未知"} / {context.context_window != null ? context.context_window.toLocaleString("en-US") : "未知"} tokens（{pct}%）{context.model ? ` · ${context.model}` : ""}
      </div>
    </div>
  );
}

function EstimatedCostRow({ cost }: { cost: AiEstimatedCost }) {
  return (
    <div>
      <div className="text-xs font-medium text-slate-500">估計成本</div>
      <div className="text-sm text-slate-700">未提供（{reasonLabel(cost.reason)}）</div>
    </div>
  );
}

function ProviderBlock({ usage, now }: { usage: AiProviderUsage; now: number }) {
  return (
    <div className="space-y-3 rounded border border-slate-100 p-3">
      <h4 className="text-sm font-semibold text-slate-900">{usage.label}</h4>
      <AccountQuotaRow label={usage.label} quota={usage.account_quota} now={now} />
      <LocalUsageRow usage={usage.local_usage} />
      <ContextUsageRow context={usage.context_usage} />
      <EstimatedCostRow cost={usage.estimated_cost} />
    </div>
  );
}

export function AiUsageCard() {
  const quota = useAiProviderQuota();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const interval = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(interval);
  }, []);

  const data = quota.data;
  const loading = quota.isLoading && data === undefined;
  const error = quota.error;
  const forbidden = error instanceof ApiError && error.status === 403;

  return (
    <Card>
      <CardTitle id="ai-usage">AI 使用量</CardTitle>
      <p className="mb-2 text-xs text-slate-500">Claude Code 與 Codex 的本機唯讀投影；不會切換 provider，也不會變更帳戶。</p>
      {loading ? <p className="text-sm text-slate-500">載入中…</p> : null}
      {!loading && forbidden ? <p className="text-sm text-slate-500">沒有權限查看 AI 使用量。</p> : null}
      {!loading && error && !forbidden ? (
        <div role="alert" className="flex items-center gap-2 rounded bg-rose-50 p-2 text-xs text-rose-800">
          <span>AI 使用量無法取得：{errorText(error)}</span>
          <Button onClick={() => void quota.refetch()}>重試</Button>
        </div>
      ) : null}
      {!loading && !error && data ? (
        <>
          {quota.isFetching ? <p role="status" className="mb-2 text-xs text-amber-700">正在更新；目前顯示快取資料。</p> : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <ProviderBlock usage={data.providers.claude_code} now={now} />
            <ProviderBlock usage={data.providers.codex} now={now} />
          </div>
          <p className="mt-2 text-xs text-slate-400">
            資料時間 {formatTime(data.generated_at)} · 每 60 秒自動更新 · 今日以 {data.today.timezone} 計算
          </p>
        </>
      ) : null}
    </Card>
  );
}
