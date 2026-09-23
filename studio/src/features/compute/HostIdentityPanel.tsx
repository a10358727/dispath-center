import { useState } from "react";
import { ApiError, api } from "@/api/client";
import type { SSHHostIdentity } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type Observation = {
  algorithm: string;
  fingerprint_sha256: string;
  public_key?: string;
  state?: SSHHostIdentity["state"];
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "未知錯誤";
}

const ACTION_LABELS: Record<"trust" | "rebind" | "replace", string> = { trust: "信任", rebind: "重新綁定", replace: "更換" };

export function HostIdentityPanel({ name, identity, onChanged }: { name: string; identity?: SSHHostIdentity | null; onChanged: () => void }) {
  const [observation, setObservation] = useState<Observation | null>(null);
  const [expected, setExpected] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const call = async (path: string, body?: Record<string, unknown>) => {
    setBusy(true); setError(null);
    try {
      const suffix = path === "observe" || path === "revoke" ? path : `actions/${path}`;
      const result = await api<Observation>(`/api/v2/server-configs/${encodeURIComponent(name)}/host-identity/${suffix}`, { method: "POST", json: body ?? {} });
      if (path === "observe") setObservation(result);
      else { setObservation(null); setExpected(""); onChanged(); }
    } catch (exc) { setError(errorText(exc)); }
    finally { setBusy(false); }
  };

  const action = identity?.state === "mismatch" ? "replace" : identity?.state === "rebind_required" ? "rebind" : "trust";
  const label = identity?.state === "mismatch" ? "已封鎖——主機身分已變更" : identity?.state === "rebind_required" ? "需要重新綁定" : identity?.verification_method === "tofu" ? "以 TOFU 信任——未獨立驗證" : identity ? "已獨立驗證" : "主機身分尚未信任";
  const tone = identity?.state === "mismatch" ? "bad" : identity?.state === "rebind_required" || !identity ? "warn" : "ok";

  return <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-2">
    <div className="flex flex-wrap items-center gap-2"><strong><span>主機身分</span><span className="ml-1 text-xs font-normal text-slate-400">Host identity</span></strong><Badge tone={tone}>{label}</Badge></div>
    {identity?.fingerprint_sha256 ? <code className="block break-all text-xs">{identity.algorithm} {identity.fingerprint_sha256}</code> : null}
    <Button disabled={busy} onClick={() => void call("observe")}>{busy ? "觀察中…" : "觀察目前主機金鑰"}</Button>
    {observation ? <div className="space-y-2 rounded bg-white p-2">
      <p className="text-xs">觀察於 <strong>{identity?.host ?? "已設定的主機"}:{identity?.port ?? 22}</strong></p>
      <code className="block break-all text-xs">{observation.algorithm} {observation.fingerprint_sha256}</code>
      <label className="block text-xs"><span className="block">供應商指紋（建議提供）</span><input aria-label="供應商 SHA256 指紋" className="w-full rounded border px-2 py-1" placeholder="SHA256:…" value={expected} onChange={(event) => setExpected(event.target.value)} /></label>
      <label className="block text-xs"><span className="block">原因（更換／重新綁定必填）</span><input aria-label="主機身分原因" className="w-full rounded border px-2 py-1" value={reason} onChange={(event) => setReason(event.target.value)} /></label>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={busy || !expected.trim() || (action !== "trust" && !reason.trim())} onClick={() => void call(action, { verification_method: "oob", expected_fingerprint_sha256: expected.trim(), reason: reason.trim() || undefined })}>核對供應商指紋並{ACTION_LABELS[action]}</Button>
        <Button disabled={busy || (action !== "trust" && !reason.trim())} onClick={() => void call(action, { verification_method: "tofu", acknowledge_tofu: true, reason: reason.trim() || undefined })}>明確接受觀察到的金鑰（TOFU）</Button>
      </div>
      <p className="text-xs text-amber-800">僅在供應商沒有可獨立核對的指紋時使用 TOFU；此紀錄會標記為未獨立驗證。</p>
    </div> : null}
    {identity && identity.state !== "revoked" ? <Button variant="ghost" disabled={busy || !reason.trim()} onClick={() => void call("revoke", { reason: reason.trim() })}>撤銷主機身分</Button> : null}
    {error ? <p role="alert" className="text-xs text-rose-800">{error}</p> : null}
  </div>;
}
