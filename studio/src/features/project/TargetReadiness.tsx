import type { ProjectWorkspace } from "@/api/types";
import { InstanceSyncButton } from "./InstanceSyncButton";
import { describeReason } from "./readiness";

type Target = NonNullable<NonNullable<ProjectWorkspace["run_creation_options"]>["ssh_target_candidates"]>[number];

export function ReasonList({ reasons }: { reasons: string[] }) {
  if (reasons.length === 0) return null;
  return (
    <ul className="space-y-0.5 text-xs text-amber-700">
      {reasons.map((reason) => {
        const described = describeReason(reason);
        return (
          <li key={reason}>
            {described.label}
            {described.hint ? <span className="text-slate-500">（{described.hint}）</span> : null}
          </li>
        );
      })}
    </ul>
  );
}

/** Every execution target with its readiness in Chinese and, where the
 *  workspace says an exact checkout update is possible, the sync action
 *  (整頓 U5). Shared by the project 執行設定 panel and the Runs page. */
export function TargetReadiness({ projectId, targets, latestPromotedVersionId }: { projectId: string; targets: Target[]; latestPromotedVersionId: string | null }) {
  if (targets.length === 0) return <div className="text-xs text-slate-500">還沒有已核准設定的機器；先在「伺服器與硬體」核准一版機器設定。</div>;
  return (
    <ul className="space-y-1 text-xs">
      {targets.map((target) => (
        <li key={target.server_name} className="space-y-1">
          <div>
            <span className={target.ready ? "text-emerald-600" : "text-amber-600"}>{target.ready ? "✓" : "○"}</span> {target.server_name}
          </div>
          {!target.ready ? <ReasonList reasons={target.readiness_reasons ?? []} /> : null}
          {!target.ready && target.update_available && target.registered_instance_id && latestPromotedVersionId ? (
            <InstanceSyncButton projectId={projectId} projectVersionId={latestPromotedVersionId} instanceId={target.registered_instance_id} serverName={target.server_name} />
          ) : null}
        </li>
      ))}
    </ul>
  );
}
