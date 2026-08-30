import { useApprovals } from "@/api/hooks";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";

export function ApprovalsPage() {
  const pending = useApprovals("pending");
  const decided = useApprovals("approved", 10);
  return (
    <div className="mx-auto max-w-3xl space-y-3 p-6">
      <h1 className="text-lg font-semibold">核准匣</h1>
      <p className="text-xs text-slate-500">每張卡也會出現在它被建立的地方；這裡只是總覽。</p>
      {pending.isLoading ? <div className="text-sm text-slate-400">載入中…</div> : null}
      {(pending.data ?? []).map((approval) => (
        <ApprovalCard key={approval.id} approval={approval} />
      ))}
      {!pending.isLoading && (pending.data ?? []).length === 0 ? <div className="text-sm text-slate-400">沒有待決的卡。</div> : null}
      {(decided.data ?? []).length > 0 ? (
        <details className="text-sm">
          <summary className="cursor-pointer text-slate-600">最近核准</summary>
          <div className="mt-2 space-y-2">
            {(decided.data ?? []).map((approval) => (
              <ApprovalCard key={approval.id} approval={approval} />
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}
