import type { EffortLevel, PermissionMode, SessionOptions, ThinkingOption } from "@/api/types";

export const MODEL_CHOICES: { value: string; label: string }[] = [
  { value: "", label: "runner 預設" },
  { value: "opus", label: "Opus（最強）" },
  { value: "sonnet", label: "Sonnet（均衡）" },
  { value: "haiku", label: "Haiku（最快）" },
];
export const PERMISSION_MODE_CHOICES: { value: PermissionMode; label: string; hint: string }[] = [
  { value: "default", label: "逐條提示", hint: "工作區外／非驗證指令每條問你" },
  { value: "acceptEdits", label: "自動接受編輯", hint: "工作區內的檔案編輯不問；Bash 仍照規則提示" },
  { value: "plan", label: "只規劃", hint: "先寫計畫，等你同意才動手" },
];
const EFFORTS: EffortLevel[] = ["low", "medium", "high", "xhigh", "max"];
const THINKING: { value: string; label: string; option: ThinkingOption | undefined }[] = [
  { value: "", label: "預設", option: undefined },
  { value: "adaptive", label: "自適應", option: "adaptive" },
  { value: "disabled", label: "關閉", option: "disabled" },
  { value: "8000", label: "固定 8k", option: { budget_tokens: 8000 } },
  { value: "32000", label: "固定 32k", option: { budget_tokens: 32000 } },
];

function thinkingKey(value: ThinkingOption | undefined): string {
  if (!value) return "";
  if (typeof value === "string") return value;
  return String(value.budget_tokens);
}

const field = "mt-1 w-full rounded border border-slate-300 p-1.5 text-sm";

/** The per-session SDK options (DG-STUDIO-UI v1 Phase 2). `bypassPermissions`
 *  is not offered anywhere: prompts can never be switched off (INV-AGENT-2). */
export function SessionOptionsFields({ value, onChange }: { value: SessionOptions; onChange: (next: SessionOptions) => void }) {
  const custom = value.model !== undefined && !MODEL_CHOICES.some((c) => c.value === value.model);
  return (
    <div className="grid grid-cols-2 gap-3">
      <label className="block text-sm">
        <span className="text-slate-600">模型</span>
        <select className={field} value={custom ? "__custom" : (value.model ?? "")} onChange={(e) => onChange({ ...value, model: e.target.value === "__custom" ? "claude-" : e.target.value || undefined })}>
          {MODEL_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>{c.label}</option>
          ))}
          <option value="__custom">自訂 model id…</option>
        </select>
        {custom ? <input className={field} value={value.model ?? ""} onChange={(e) => onChange({ ...value, model: e.target.value || undefined })} placeholder="claude-…" /> : null}
      </label>
      <label className="block text-sm">
        <span className="text-slate-600">推理力度（effort）</span>
        <select className={field} value={value.effort ?? ""} onChange={(e) => onChange({ ...value, effort: (e.target.value || undefined) as EffortLevel | undefined })}>
          <option value="">預設</option>
          {EFFORTS.map((level) => (
            <option key={level} value={level}>{level}</option>
          ))}
        </select>
      </label>
      <label className="block text-sm">
        <span className="text-slate-600">思考（thinking）</span>
        <select className={field} value={thinkingKey(value.thinking)} onChange={(e) => onChange({ ...value, thinking: THINKING.find((t) => t.value === e.target.value)?.option })}>
          {THINKING.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
      </label>
      <label className="block text-sm">
        <span className="text-slate-600">權限模式</span>
        <select className={field} value={value.permission_mode ?? "default"} onChange={(e) => onChange({ ...value, permission_mode: e.target.value as PermissionMode })}>
          {PERMISSION_MODE_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>{c.label}</option>
          ))}
        </select>
        <span className="text-xs text-slate-500">{PERMISSION_MODE_CHOICES.find((c) => c.value === (value.permission_mode ?? "default"))?.hint}</span>
      </label>
    </div>
  );
}
