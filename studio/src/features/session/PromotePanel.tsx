import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { usePromoteRequest } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";

/** After a checkpoint card is approved, offer the promotion step (整頓 U3).
 *  The button only creates the `engineering_task_promote` card; the person
 *  then decides it in place — promotion is never one-click (DG-CODE-PROMOTE
 *  P-1, INV-PLANE-1). */
export function PromotePanel({ taskId, project, onPromoted }: { taskId: string; project?: string; onPromoted?: () => void }) {
  const client = useQueryClient();
  const promote = usePromoteRequest();
  const [approval, setApproval] = useState<Approval | null>(null);
  return (
    <div className="space-y-2 rounded border border-emerald-200 bg-emerald-50/50 p-2 text-sm" data-testid={`promote-${taskId}`}>
      {approval ? (
        <ApprovalCard
          approval={approval}
          onDecided={(result) => {
            setApproval(null);
            if (result.status === "approved") {
              if (project) void client.invalidateQueries({ queryKey: ["versions", project] });
              void client.invalidateQueries({ queryKey: ["projects"] });
              void client.invalidateQueries({ queryKey: ["engineering-tasks"] });
              onPromoted?.();
            }
          }}
        />
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-slate-700">存檔已核准。晉升後這個版本才能拿來跑實驗；晉升永遠需要你親自核准。</span>
          <Button variant="primary" disabled={promote.isPending} onClick={() => void promote.mutateAsync(taskId).then((result) => setApproval(result.approval))}>
            晉升為正式版本
          </Button>
          {promote.error ? <span className="text-xs text-rose-700">{(promote.error as Error).message}</span> : null}
        </div>
      )}
    </div>
  );
}
