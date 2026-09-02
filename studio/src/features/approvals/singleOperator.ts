/** DG-SINGLE-OPERATOR-CONFIRM v1 (2026-09-02): the closed list of kinds the
 *  Studio may offer 「確認並執行」for — the same signed-in person creates the
 *  card and decides it right away through the ordinary digest-bound v2
 *  decision (audited, `approved_by="human"`). Never auto-approval: nothing
 *  here touches `maybe_auto_approve()` or INV-APPROVAL-4.
 *
 *  Kinds that stay two-step by ruling and must never appear here: promotion
 *  (`engineering_task_promote`, P-1), deletion, server config, runner/node/
 *  service credentials, membership/role changes, hardware physical actions. */
export const SINGLE_OPERATOR_CONFIRM_KINDS: ReadonlySet<string> = new Set([
  "execution_plan_v2",
  "experiment_create_v2",
  "environment_change_v2",
  "run_template_change_v2",
  "project_defaults_change_v2",
  "project_instance_update_v2",
  "agent_session_open",
  "agent_session_checkpoint",
  "inventory_scan",
  "ignore_project_candidate",
  "ignore_nested_candidates",
  "import_project",
  "dataset_publish_v2",
  "dataset_asset_adoption_v2",
  "dataset_alias_change_v2",
]);

export function canConfirmImmediately(kind: string): boolean {
  return SINGLE_OPERATOR_CONFIRM_KINDS.has(kind);
}
