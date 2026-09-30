import { useState } from "react";
import { ApiError, api } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export interface InstallPublicKeyResult { ok: boolean; outcome: string; detail?: string; public_key?: string; }

const OUTCOME_TEXT: Record<string, string> = {
  installed: "公鑰已寫入該帳號的 authorized_keys。",
  auth_failed: "密碼被拒絕：請確認 SSH 使用者與密碼。",
  host_identity_mismatch: "主機金鑰與已信任的身分不符，已中止。",
  unreachable: "連不上主機（網路、連接埠或該主機關閉了密碼登入）。",
  remote_failed: "已登入但寫入 authorized_keys 失敗（權限或磁碟問題）。",
};

/** Looks like an SSH auth failure on the platform's key: the only case the
 *  one-shot install can fix. */
export function looksLikePermissionDenied(errors: string[] | undefined): boolean {
  return (errors ?? []).some((line) => /permission denied|publickey/i.test(line));
}

export function InstallPublicKeyDialog({ name, user, host, onInstalled, onClose }: { name: string; user: string; host: string; onInstalled: () => void; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<InstallPublicKeyResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true); setError(null); setResult(null);
    try {
      const out = await api<InstallPublicKeyResult>(`/api/v2/server-configs/${encodeURIComponent(name)}/install-public-key`, { method: "POST", json: { password } });
      setResult(out);
      if (out.ok) onInstalled();
    } catch (exc) { setError(exc instanceof ApiError ? exc.message : exc instanceof Error ? exc.message : "未知錯誤"); }
    finally { setPassword(""); setBusy(false); }
  }

  return <div role="dialog" aria-label="安裝公鑰" className="space-y-3 rounded border border-sky-200 bg-sky-50 p-3">
    <div><strong>安裝公鑰到這台主機</strong><span className="ml-1 text-xs text-slate-400">Install public key</span></div>
    <p className="text-xs text-slate-700">輸入 <code>{user}@{host}</code> 的密碼。平台只會用它登入這一次，把設定裡那把私鑰對應的公鑰寫進該帳號的 <code>~/.ssh/authorized_keys</code>；密碼不會被儲存、記錄或顯示。之後所有連線都改用金鑰。</p>
    <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <label className="block text-xs"><span className="block">密碼（只用一次）</span><input aria-label="主機密碼" type="password" autoComplete="off" className="w-64 rounded border px-2 py-1" value={password} disabled={busy} onChange={(event) => setPassword(event.target.value)} /></label>
      <Button type="submit" variant="primary" disabled={busy || !password}>{busy ? "安裝中…" : "登入一次並安裝公鑰"}</Button>
      <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>取消</Button>
    </form>
    {result ? <div role="status" className="space-y-1"><Badge tone={result.ok ? "ok" : "bad"}>{result.ok ? "公鑰已安裝" : "安裝失敗"}</Badge> <span className="text-xs">{OUTCOME_TEXT[result.outcome] ?? result.detail ?? result.outcome}</span>
      {result.outcome === "unreachable" || result.outcome === "remote_failed" ? <p className="text-xs text-slate-600">若該主機關閉了密碼登入，請在該主機上手動把公鑰貼進 <code>~/.ssh/authorized_keys</code>：<code className="block break-all">{result.public_key}</code></p> : null}
    </div> : null}
    {error ? <p role="alert" className="text-xs text-rose-800">{error}</p> : null}
  </div>;
}
