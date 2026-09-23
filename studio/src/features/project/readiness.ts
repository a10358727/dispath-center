/** Chinese labels for the backend readiness reason codes (整頓 U4a/U5).
 *  Codes come from `app/db.py` (`ssh_target_candidates[].readiness_reasons`)
 *  and `app/project_environments.py` (`evaluate_environment_readiness`). */
export const READINESS_REASONS: Record<string, { label: string; hint?: string }> = {
  no_matching_promoted_version: { label: "機器上的程式碼不是任何正式版本", hint: "用「同步到機器」把正式版本放上去" },
  project_instance_dirty: { label: "機器上有未提交的變更", hint: "先把工作目錄清乾淨" },
  project_instance_not_available: { label: "還沒登記這台機器的專案副本", hint: "先匯入或部署到這台機器" },
  project_instance_ambiguous: { label: "同一台機器有多份專案副本", hint: "只保留一份" },
  environment_archived: { label: "環境已封存" },
  verified_host_configuration_unavailable: { label: "沒有已驗證的機器設定", hint: "先在「伺服器與硬體」核准一版機器設定" },
  required_server_tags_missing: { label: "沒有機器帶齊環境要求的標籤", hint: "調整環境的標籤或機器標籤" },
  host_observation_unknown: { label: "機器狀態未知", hint: "等 monitor 下一輪探測" },
  host_observation_stale: { label: "機器觀測已過期", hint: "等 monitor 下一輪探測" },
  host_observation_not_ready: { label: "機器未就緒" },
  typed_host_evidence_unavailable: { label: "缺少機器的型別化證據" },
  executable_missing: { label: "機器缺少環境要求的執行檔" },
  ssh_host_identity_untrusted: { label: "SSH host identity 尚未信任", hint: "到「Advanced Compute」核對 provider fingerprint，或明確接受 TOFU" },
  ssh_host_identity_rebind_required: { label: "SSH host/port 已變更，需要人工 Rebind", hint: "核對新端點與 host key 後執行 Rebind" },
  ssh_host_identity_changed: { label: "BLOCKED — host identity changed", hint: "停止連線並核對是否重灌或換機；確認後執行 Replace Identity" },
  ssh_host_identity_revoked: { label: "SSH host identity 已撤銷", hint: "重新觀察並由管理者明確 Trust" },
};

export function describeReason(reason: string): { label: string; hint?: string } {
  return READINESS_REASONS[reason] ?? { label: reason };
}
