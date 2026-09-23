import { useMemo, useState } from "react";
import { ApiError, api } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HostIdentityPanel } from "./HostIdentityPanel";

type ComputeKind = "rental" | "owned" | "fpga";
interface Draft { kind: ComputeKind; name: string; host: string; port: string; user: string; key: string; tags: string; }
interface ProbeResult { ok: boolean; results?: Record<string, unknown>; errors?: string[]; }
interface PreflightResult { status?: string; filesystem_type?: string | null; }
const kinds = [
  { value: "rental" as const, title: "租用 GPU", description: "租用的 SSH 主機。每個租用主機都需要一個不重複的運算資源名稱。", tags: ["rental", "gpu"], gpu: true },
  { value: "owned" as const, title: "自有伺服器", description: "由你管理、透過 SSH 連線的伺服器。", tags: ["owned"], gpu: false },
  { value: "fpga" as const, title: "FPGA 主機", description: "分類為硬體工作、透過 SSH 連線的主機。", tags: ["fpga"], gpu: false },
];
const initial: Draft = { kind: "rental", name: "", host: "", port: "22", user: "", key: "", tags: "" };
const errorText = (error: unknown) => error instanceof ApiError ? error.message : error instanceof Error ? error.message : "未知錯誤";

export function AddComputeWizard({ existingNames, onAdded, onCancel }: { existingNames: string[]; onAdded: () => void; onCancel: () => void }) {
  const [step, setStep] = useState(1), [draft, setDraft] = useState(initial);
  const [created, setCreated] = useState(false), [trusted, setTrusted] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [completed, setCompleted] = useState<{ state: "eligible" | "blocked" | "unknown" | "error"; detail: string } | null>(null);
  const kind = kinds.find((item) => item.value === draft.kind) ?? kinds[0];
  const payload = useMemo(() => ({ name: draft.name.trim(), host: draft.host.trim(), port: Number.parseInt(draft.port, 10), user: draft.user.trim(), key: draft.key.trim(), tags: [...new Set([...kind.tags, ...draft.tags.split(",").map((tag) => tag.trim()).filter(Boolean)])], gpu: kind.gpu, enabled: true }), [draft, kind]);
  const valid = Boolean(payload.name && payload.host && payload.user && payload.key && payload.port > 0 && payload.port <= 65535 && !existingNames.includes(payload.name));

  async function persist() { setBusy(true); setError(null); try { await api("/api/v2/server-configs", { method: "POST", json: payload }); setCreated(true); onAdded(); } catch (exc) { setError(errorText(exc)); } finally { setBusy(false); } }
  async function verify() {
    setBusy(true); setError(null); setProbe(null);
    try {
      const result = await api<ProbeResult>("/api/v2/server-configs/test-ssh", { method: "POST", json: payload }); setProbe(result);
      if (!result.ok) { setError(`運算資源仍保持已新增與已信任狀態。${(result.errors ?? ["連線測試失敗。"]).join(" ")}`); return; }
      try {
        const preflight = await api<PreflightResult>(`/api/v2/server-configs/${encodeURIComponent(payload.name)}/attempt-preflight`, { method: "POST", json: {} });
        if (preflight.status === "eligible") setCompleted({ state: "eligible", detail: `已偵測到本機 attempt 檔案系統${preflight.filesystem_type ? `（${preflight.filesystem_type}）` : ""}。` });
        else if (preflight.status === "ineligible_non_local_fs") setCompleted({ state: "blocked", detail: "attempt 檔案系統不是本機檔案系統。" });
        else setCompleted({ state: "unknown", detail: "檔案系統預檢未回傳可辨識的結果。" });
      } catch (exc) { setCompleted({ state: "error", detail: `運算資源仍保持已新增與已信任狀態，但預檢失敗：${errorText(exc)}` }); }
    } catch (exc) { setError(`運算資源仍保持已新增與已信任狀態。連線測試失敗：${errorText(exc)}`); }
    finally { setBusy(false); }
  }

  if (completed) return <section aria-label="新增運算資源" className="space-y-3 rounded border border-emerald-200 bg-emerald-50 p-4"><h3 className="font-semibold">運算資源已新增：{payload.name}</h3><div role="status"><Badge tone={completed.state === "eligible" ? "ok" : completed.state === "unknown" ? "warn" : "bad"}>{completed.state === "eligible" ? "預檢通過" : completed.state === "blocked" ? "預檢被擋" : completed.state === "error" ? "預檢錯誤" : "預檢結果未知"}</Badge> <span>{completed.detail}</span></div><Button variant="primary" onClick={onCancel}>完成</Button></section>;

  const fields: Array<[keyof Draft, string]> = [["name", "運算資源名稱（不可重複）"], ["host", "主機"], ["port", "SSH 連接埠"], ["user", "SSH 使用者"], ["key", "私鑰路徑參照"], ["tags", "額外標籤（逗號分隔）"]];
  return <section aria-label="新增運算資源" className="space-y-4 rounded border border-sky-200 bg-sky-50 p-4">
    <div className="flex justify-between"><div><h3 className="font-semibold"><span>新增運算資源</span><span className="ml-1 text-xs font-normal text-slate-400">Add Compute</span></h3><p className="text-xs">第 {step} 步，共 4 步</p></div><Button variant="ghost" onClick={onCancel}>取消</Button></div>
    {step === 1 ? <div className="grid gap-2 md:grid-cols-3">{kinds.map((item) => <button key={item.value} type="button" aria-pressed={draft.kind === item.value} className="rounded border bg-white p-3 text-left" onClick={() => setDraft({ ...draft, kind: item.value })}><strong>{item.title}</strong><span className="block text-xs">{item.description}</span></button>)}</div> : null}
    {step === 2 ? <div className="grid gap-3 md:grid-cols-2">{fields.map(([field, label]) => <label key={field} className="text-sm"><span className="block">{label}</span><input aria-label={label} className="w-full rounded border px-2 py-1" type={field === "port" ? "number" : "text"} value={draft[field]} onChange={(event) => setDraft({ ...draft, [field]: event.target.value })} /></label>)}<p className="col-span-full text-xs">請使用私鑰路徑參照，切勿貼上金鑰內容。</p></div> : null}
    {step === 3 ? <div className="space-y-3"><p>會先寫入運算資源設定，再驗證並綁定主機身分；信任建立前不會執行任何 SSH 指令。</p>{!created ? <Button variant="primary" disabled={busy} onClick={() => void persist()}>{busy ? "新增中…" : "新增運算資源設定"}</Button> : <><Badge tone="ok">已新增運算資源設定</Badge>{trusted ? <Badge tone="ok">主機身分已信任</Badge> : <HostIdentityPanel name={payload.name} identity={null} onChanged={() => setTrusted(true)} />}</>}</div> : null}
    {step === 4 ? <div className="space-y-3"><p>{payload.user}@{payload.host}:{payload.port}</p><p className="text-xs">主機身分已綁定。SSH 連線測試成功後會接著執行檔案系統預檢。</p>{probe?.ok ? <Badge tone="ok">連線成功</Badge> : null}<Button variant="primary" disabled={busy} onClick={() => void verify()}>{busy ? "驗證中…" : "測試已信任的連線並執行預檢"}</Button></div> : null}
    {error ? <div role="alert" className="rounded bg-rose-100 p-2 text-sm">{error}</div> : null}
    <div className="flex justify-between"><Button variant="ghost" disabled={step === 1 || busy || created} onClick={() => setStep(step - 1)}>上一步</Button>{step < 4 ? <Button variant="primary" disabled={(step === 2 && !valid) || (step === 3 && !trusted)} onClick={() => setStep(step + 1)}>繼續</Button> : null}</div>
  </section>;
}
