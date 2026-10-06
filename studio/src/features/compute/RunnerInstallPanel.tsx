import { useState } from "react";
import { ApiError, api } from "@/api/client";
import type { AgentRunner, Approval } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";

const errorText = (error: unknown) => (error instanceof ApiError ? error.message : error instanceof Error ? error.message : "未知錯誤");

/** DG-AGENT-RUNNER-INSTALL-v1: install a runner agent from the Studio.
 *  The enrol card carries an `install` spec; the platform provisions the
 *  machine over the trusted SSH channel when the card is approved (two-step
 *  by ruling: runner credentials never auto-confirm). */
export function RunnerInstallPanel({ server, onChanged }: { server: string; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [workspaceRoot, setWorkspaceRoot] = useState("~/dispatch_workspaces");
  const [approval, setApproval] = useState<Approval | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function request() {
    setBusy(true); setError(null);
    try {
      const result = await api<{ approval: Approval }>("/api/v2/agent-runners/enroll-requests", { method: "POST", json: { server, install: { workspace_root: workspaceRoot.trim() || "~/dispatch_workspaces" } } });
      setApproval(result.approval);
    } catch (exc) { setError(errorText(exc)); }
    finally { setBusy(false); }
  }

  if (approval) return <div className="space-y-2"><ApprovalCard approval={approval} onDecided={() => onChanged()} /><Button variant="ghost" onClick={() => { setApproval(null); setOpen(false); }}>關閉</Button></div>;
  if (!open) return <Button onClick={() => setOpen(true)}>安裝 runner agent</Button>;
  return <form role="form" aria-label="安裝 runner agent" className="space-y-2 rounded border border-sky-200 bg-sky-50 p-2" onSubmit={(event) => { event.preventDefault(); void request(); }}>
    <p className="text-xs text-slate-700">核准後，平台會經由已信任的 SSH 把 runner 程式安裝到這台機器、寫入設定與登錄憑證，並在 tmux 中啟動；之後若斷線會自動重新拉起。不需要 sudo，機器不開任何入站埠。</p>
    <label className="block text-xs"><span className="block">工作區根目錄（家目錄底下）</span><input aria-label="工作區根目錄" className="w-64 rounded border px-2 py-1" value={workspaceRoot} onChange={(event) => setWorkspaceRoot(event.target.value)} /></label>
    <div className="flex gap-2"><Button type="submit" variant="primary" disabled={busy}>{busy ? "建立中…" : "建立安裝核准卡"}</Button><Button type="button" variant="ghost" disabled={busy} onClick={() => setOpen(false)}>取消</Button></div>
    {error ? <p role="alert" className="text-xs text-rose-800">{error}</p> : null}
  </form>;
}

/** Write the Claude Code OAuth token (`claude setup-token`) into a managed
 *  runner's claude.env and restart it. The token is sent once and never shown. */
export function RunnerClaudeTokenDialog({ runner, onDone }: { runner: AgentRunner; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; step: string; detail?: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true); setError(null); setResult(null);
    try {
      const out = await api<{ ok: boolean; step: string; detail?: string }>(`/api/v2/agent-runners/${encodeURIComponent(runner.id)}/claude-token`, { method: "POST", json: { token } });
      setResult(out);
      if (out.ok) onDone();
    } catch (exc) { setError(errorText(exc)); }
    finally { setToken(""); setBusy(false); }
  }

  if (!open) return <Button onClick={() => setOpen(true)}>設定 Claude token</Button>;
  return <form role="dialog" aria-label="設定 Claude token" className="space-y-2 rounded border border-sky-200 bg-sky-50 p-2" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <p className="text-xs text-slate-700">在任一台已登入 Claude 的機器執行 <code>claude setup-token</code>，把印出的 token 貼進來。平台只用它寫入這台 runner 的 <code>claude.env</code>（0600）並重啟 runner；token 不會被儲存、記錄或顯示。</p>
    <label className="block text-xs"><span className="block">Claude token（只用一次）</span><input aria-label="Claude token" type="password" autoComplete="off" className="w-80 rounded border px-2 py-1" value={token} disabled={busy} onChange={(event) => setToken(event.target.value)} /></label>
    <div className="flex gap-2"><Button type="submit" variant="primary" disabled={busy || !token}>{busy ? "寫入中…" : "寫入並重啟 runner"}</Button><Button type="button" variant="ghost" disabled={busy} onClick={() => setOpen(false)}>取消</Button></div>
    {result ? <div role="status" className="text-xs"><Badge tone={result.ok ? "ok" : "bad"}>{result.ok ? "已設定" : `失敗（${result.step}）`}</Badge> {result.detail}</div> : null}
    {error ? <p role="alert" className="text-xs text-rose-800">{error}</p> : null}
  </form>;
}
