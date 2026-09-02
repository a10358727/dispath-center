import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { Approval } from "@/api/types";
import { Button } from "@/components/ui/button";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { shortCommit } from "@/lib";

interface Preview {
  preview_digest: string;
  before?: { git_commit?: string | null; git_branch?: string | null };
  desired?: { project_version_id?: string; git_commit?: string };
}

/** 「同步到 {server}」(整頓 U5): preview the exact checkout change (a live
 *  read-only SSH probe), then create one `project_instance_update_v2` card
 *  pinned to that preview digest. Decided in place like every other card. */
export function InstanceSyncButton({ projectId, projectVersionId, instanceId, serverName }: { projectId: string; projectVersionId: string; instanceId: string; serverName: string }) {
  const client = useQueryClient();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);
  const previewMutation = useMutation({
    mutationFn: () => api<Preview>(`/api/v2/projects/${encodeURIComponent(projectId)}/instance-update-previews`, { method: "POST", json: { project_version_id: projectVersionId, instance_id: instanceId } }),
    onSuccess: (result) => setPreview(result),
  });
  const requestMutation = useMutation({
    mutationFn: async (digest: string) => {
      const created = await api<{ approval_id: number }>(`/api/v2/projects/${encodeURIComponent(projectId)}/instance-update-requests`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        json: { project_version_id: projectVersionId, instance_id: instanceId, expected_preview_digest: digest },
      });
      return api<Approval>(`/api/v2/approvals/${created.approval_id}`);
    },
    onSuccess: (result) => setApproval(result),
  });
  const error = (previewMutation.error ?? requestMutation.error) as Error | undefined;

  if (approval) {
    return (
      <ApprovalCard
        approval={approval}
        onDecided={() => {
          setApproval(null);
          setPreview(null);
          void client.invalidateQueries({ queryKey: ["workspace"] });
          void client.invalidateQueries({ queryKey: ["projects"] });
        }}
      />
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" data-testid={`instance-sync-${serverName}`}>
      {preview ? (
        <>
          <span className="text-slate-600">
            {serverName}：{shortCommit(preview.before?.git_commit)} → {shortCommit(preview.desired?.git_commit)}
          </span>
          <Button variant="primary" disabled={requestMutation.isPending} onClick={() => requestMutation.mutate(preview.preview_digest)}>
            建立同步卡
          </Button>
          <Button onClick={() => setPreview(null)}>取消</Button>
        </>
      ) : (
        <Button disabled={previewMutation.isPending} onClick={() => previewMutation.mutate()}>
          {previewMutation.isPending ? "檢查機器中…" : `同步到 ${serverName}`}
        </Button>
      )}
      {error ? <span className="text-rose-700">{error.message}</span> : null}
    </div>
  );
}
