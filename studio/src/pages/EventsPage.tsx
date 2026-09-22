import { actorLabel, describeActivity } from "@/labels";
import { useMemo, useState } from "react";
import { useAuditEvents } from "@/api/hooks";
import type { AuditRecord } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { formatTime } from "@/lib";

function Row({ record }: { record: AuditRecord }) {
  const [open, setOpen] = useState(false);
  const ok = record.result === "ok" || record.result === "approved" || record.result === "success";
  return (
    <>
      <tr className="border-t border-slate-100 hover:bg-slate-50">
        <td className="whitespace-nowrap px-2 py-1 text-slate-500">{formatTime(record.ts)}</td>
        <td className="px-2 py-1">
          <div>{describeActivity(record) || "未分類活動"}</div>
          <button type="button" className="mt-0.5 text-left text-[11px] text-sky-700 underline-offset-2 hover:underline" aria-expanded={open} onClick={() => setOpen(!open)}>
            Advanced audit details
          </button>
        </td>
        <td className="px-2 py-1">
          <Badge tone={ok ? "ok" : record.result === "rejected" ? "warn" : record.result ? "bad" : "neutral"}>{record.result ?? "結果未提供"}</Badge>
        </td>
        <td className="px-2 py-1 text-slate-500">{actorLabel(record.actor)}</td>
      </tr>
      {open ? (
        <tr className="border-t border-slate-100 bg-slate-50">
          <td colSpan={4} className="px-2 py-1">
            <pre className="max-h-64 overflow-auto text-xs">{JSON.stringify(record, null, 2)}</pre>
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function EventsPage() {
  const events = useAuditEvents(200);
  const [filter, setFilter] = useState("");
  const rows = useMemo(() => {
    const all = events.data ?? [];
    if (!filter.trim()) return all;
    const needle = filter.trim().toLowerCase();
    return all.filter((record) => JSON.stringify(record).toLowerCase().includes(needle));
  }, [events.data, filter]);
  return (
    <div className="min-h-0 space-y-3 overflow-y-auto p-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Activity</h1>
        <input
          className="w-72 rounded border border-slate-300 px-2 py-1 text-sm"
          placeholder="搜尋活動、操作者或內容…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
        <span className="text-xs text-slate-500">最新 {events.data?.length ?? 0} 筆可讀活動；需要時可展開完整稽核記錄</span>
      </div>
      {events.isLoading ? <div className="text-sm text-slate-500">載入活動…</div> : null}
      {events.error ? <div role="alert" className="flex items-center gap-2 text-sm text-rose-700"><span>{(events.error as { status?: number }).status === 403 ? "沒有權限查看活動。" : "活動暫時無法取得。"}</span><button className="underline" onClick={() => void events.refetch()}>重試</button></div> : null}
      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-xs">
          <thead className="bg-slate-50 text-left text-slate-500">
            <tr>
              <th className="px-2 py-1">時間</th>
              <th className="px-2 py-1">活動</th>
              <th className="px-2 py-1">結果</th>
              <th className="px-2 py-1">操作者</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((record, index) => (
              <Row key={(record.event_id as string) ?? index} record={record} />
            ))}
          </tbody>
        </table>
        {rows.length === 0 && !events.isLoading && !events.error ? <div className="py-4 text-center text-xs text-slate-400">沒有符合的事件。</div> : null}
      </div>
    </div>
  );
}
