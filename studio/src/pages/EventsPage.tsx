import { actorLabel, describeAudit } from "@/labels";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { formatTime } from "@/lib";

interface AuditRecord {
  event_id?: string;
  ts?: string;
  action?: string;
  result?: string;
  actor?: { id?: string | null; kind?: string | null; authentication?: string | null } | null;
  source?: string;
  durability?: string;
  [key: string]: unknown;
}

function useEvents() {
  return useQuery({
    queryKey: ["audit-events"],
    queryFn: () => api<AuditRecord[]>("/api/v2/events?limit=200"),
    refetchInterval: 30_000,
  });
}

function Row({ record }: { record: AuditRecord }) {
  const [open, setOpen] = useState(false);
  const ok = record.result == null || record.result === "ok" || record.result === "approved" || record.result === "success";
  return (
    <>
      <tr className="cursor-pointer border-t border-slate-100 hover:bg-slate-50" onClick={() => setOpen(!open)}>
        <td className="whitespace-nowrap px-2 py-1 text-slate-500">{formatTime(record.ts)}</td>
        <td className="px-2 py-1" title={record.action ?? ""}>{describeAudit(record) || "—"}</td>
        <td className="px-2 py-1">
          <Badge tone={ok ? "ok" : record.result === "rejected" ? "warn" : "bad"}>{record.result ?? "ok"}</Badge>
        </td>
        <td className="px-2 py-1 text-slate-500" title={record.actor?.id ? String(record.actor.id) : ""}>{actorLabel(record.actor)}</td>
        <td className="px-2 py-1">
          <Badge tone={record.durability === "transactional" ? "info" : "neutral"}>{record.source ?? ""}</Badge>
        </td>
      </tr>
      {open ? (
        <tr className="border-t border-slate-100 bg-slate-50">
          <td colSpan={5} className="px-2 py-1">
            <pre className="max-h-64 overflow-auto text-xs">{JSON.stringify(record, null, 2)}</pre>
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function EventsPage() {
  const events = useEvents();
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
        <h1 className="text-lg font-semibold">稽核事件</h1>
        <input
          className="w-72 rounded border border-slate-300 px-2 py-1 text-sm"
          placeholder="過濾（action／actor／內容）…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
        <span className="text-xs text-slate-500">最新 {events.data?.length ?? 0} 筆，30 秒自動更新；點列展開完整內容</span>
      </div>
      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-xs">
          <thead className="bg-slate-50 text-left text-slate-500">
            <tr>
              <th className="px-2 py-1">時間</th>
              <th className="px-2 py-1">action</th>
              <th className="px-2 py-1">結果</th>
              <th className="px-2 py-1">actor</th>
              <th className="px-2 py-1">來源</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((record, index) => (
              <Row key={(record.event_id as string) ?? index} record={record} />
            ))}
          </tbody>
        </table>
        {rows.length === 0 && !events.isLoading ? <div className="py-4 text-center text-xs text-slate-400">沒有符合的事件。</div> : null}
      </div>
    </div>
  );
}
