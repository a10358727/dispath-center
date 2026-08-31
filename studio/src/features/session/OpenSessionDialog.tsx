import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { keys, useOpenSessionRequest, useRunners, useVersions } from "@/api/hooks";
import type { Approval, SessionOptions } from "@/api/types";
import { SessionOptionsFields } from "./SessionOptionsFields";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { formatTime, shortCommit } from "@/lib";

/** Open-session request: base version + runner -> one approval card, decided
 *  right here (DG-STUDIO-UI v1); the runner then hosts the SDK session. */
export function OpenSessionDialog({
  project,
  onOpened,
  onCancel,
  fork,
}: {
  project: string;
  onOpened: () => void;
  onCancel: () => void;
  /** P2-4: branch off an existing session (version/runner are inherited). */
  fork?: { sessionId: string; baseVersionId: string | null; runnerId: string | null };
}) {
  const versions = useVersions(project);
  const runners = useRunners();
  const request = useOpenSessionRequest(project);
  const client = useQueryClient();
  const [versionId, setVersionId] = useState(fork?.baseVersionId ?? "");
  const [runnerId, setRunnerId] = useState(fork?.runnerId ?? "");
  const [approval, setApproval] = useState<Approval | null>(null);
  const [options, setOptions] = useState<SessionOptions>({});
  const activeRunners = (runners.data?.runners ?? []).filter((runner) => runner.active);

  return (
    <Card className="space-y-3">
      <CardTitle>{fork ? `分支 session（來源 ${fork.sessionId.slice(0, 8)}…）` : "開新 Agent session"}</CardTitle>
      {approval ? (
        <ApprovalCard
          approval={approval}
          onDecided={() => {
            void client.invalidateQueries({ queryKey: keys.sessions(project) });
            onOpened();
          }}
        />
      ) : (
        <>
          <label className="block text-sm">
            <span className="text-slate-600">基底版本</span>
            <select className="mt-1 w-full rounded border border-slate-300 p-1.5" value={versionId} onChange={(event) => setVersionId(event.target.value)}>
              <option value="">選擇版本…</option>
              {(versions.data ?? []).map((version) => (
                <option key={version.id} value={version.id}>
                  {version.git_ref ?? "?"} @ {shortCommit(version.git_commit)} · {formatTime(version.created_at)} {version.promotion_state ? `· ${version.promotion_state}` : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-sm">
            <span className="text-slate-600">runner</span>
            <select className="mt-1 w-full rounded border border-slate-300 p-1.5" value={runnerId} onChange={(event) => setRunnerId(event.target.value)}>
              <option value="">選擇 runner…</option>
              {activeRunners.map((runner) => (
                <option key={runner.id} value={runner.id}>
                  {runner.server}
                  {runner.label ? ` · ${runner.label}` : ""} {runner.connected ? "（已連線）" : "（離線）"}
                </option>
              ))}
            </select>
          </label>
          <SessionOptionsFields value={options} onChange={setOptions} />
          {runners.data && !runners.data.enabled ? <div className="text-xs text-amber-700">AGENT_RUNTIME_V3_ENABLED 未開啟，無法開 session。</div> : null}
          {request.error ? <div className="text-xs text-rose-700">{(request.error as Error).message}</div> : null}
          <div className="flex gap-2">
            <Button
              variant="primary"
              disabled={!versionId || !runnerId || request.isPending}
              onClick={() =>
                request
                  .mutateAsync({
                    base_version_id: versionId,
                    runner_id: runnerId,
                    options: Object.keys(options).length ? options : undefined,
                    fork_from_session_id: fork?.sessionId,
                  })
                  .then((result) => setApproval(result.approval))
              }
            >
              建立核准卡
            </Button>
            <Button variant="ghost" onClick={onCancel}>
              取消
            </Button>
          </div>
        </>
      )}
    </Card>
  );
}
