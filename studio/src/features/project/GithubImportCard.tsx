import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError, api } from "@/api/client";
import { useServerConfigs } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";

/** DG-PROJECT-GITHUB-IMPORT-v1: the only way to add a project. The card builds
 *  a `project_github_import` approval; the platform clones on the worker
 *  after the decision and refuses repositories without a non-empty README.md. */

const GITHUB_URL_RE = /^https:\/\/github\.com\/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9_.-]{1,100}?(?:\.git)?\/?$/;
const GITHUB_REMOTE_RE = /^(?:https:\/\/github\.com\/|git@github\.com:|ssh:\/\/git@github\.com\/)[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+?(?:\.git)?\/?$/;

export const README_TEMPLATE = `# <專案名稱>

## 目的
這個專案在做什麼、要解決什麼問題。

## 資料
使用哪些資料集、來源與版本。

## 執行方式
如何安裝、如何訓練／執行、主要參數。
`;

export function isGithubRepoUrl(value: string): boolean {
  return GITHUB_URL_RE.test(value.trim());
}

export function repoNameFromUrl(value: string): string {
  const match = value.trim().match(/github\.com\/[^/]+\/([^/]+?)(?:\.git)?\/?$/);
  return match ? match[1] : "";
}

/** Whether a scanned candidate can still be imported under the GitHub-only rule. */
export function candidateMeetsGithubRule(candidate: Record<string, unknown>): boolean {
  const remote = typeof candidate.git_remote === "string" ? candidate.git_remote.trim() : "";
  const readme = typeof candidate.readme_excerpt === "string" ? candidate.readme_excerpt.trim() : "";
  return GITHUB_REMOTE_RE.test(remote) && readme.length > 0;
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "未知錯誤";
}

export function GithubImportCard({ onDone }: { onDone: () => void }) {
  const configs = useServerConfigs();
  const servers = useMemo(() => (configs.data ?? []).filter((config) => config.enabled !== false), [configs.data]);
  const [repoUrl, setRepoUrl] = useState("");
  const [project, setProject] = useState("");
  const [server, setServer] = useState("");
  const [destPath, setDestPath] = useState("");
  const [ref, setRef] = useState("");
  const [showGuide, setShowGuide] = useState(false);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [confirmNow, setConfirmNow] = useState(false);
  const effectiveServer = server || servers[0]?.name || "";
  const urlValid = isGithubRepoUrl(repoUrl);
  const suggestedName = repoNameFromUrl(repoUrl);

  const request = useMutation({
    mutationFn: async (confirm: boolean) => {
      setConfirmNow(confirm);
      const result = await api<Approval>("/api/v2/projects/github-import-requests", {
        method: "POST",
        json: {
          repo_url: repoUrl.trim(),
          target_server: effectiveServer,
          project: project.trim() || undefined,
          dest_path: destPath.trim() || undefined,
          ref: ref.trim() || undefined,
        },
      });
      return result;
    },
    onSuccess: (card) => setApproval(card),
  });

  return (
    <Card className="space-y-3 border-sky-200">
      <CardTitle>從 GitHub 匯入專案<span className="ml-2 text-xs font-normal text-slate-400">Import from GitHub</span></CardTitle>
      <p className="text-sm text-slate-700">
        規定：專案只能從 GitHub 專案新增（既有專案，或你在 GitHub 新建的空白專案），而且 README.md 必須存在且非空，說明這個專案在做什麼。核准後平台會在目標機上 clone，缺少 README 的專案會被拒絕。
      </p>
      {approval ? (
        <ApprovalCard approval={approval} confirmImmediately={confirmNow} onDecided={() => { setApproval(null); setConfirmNow(false); setRepoUrl(""); setProject(""); setDestPath(""); setRef(""); onDone(); }} />
      ) : (
        <div className="grid gap-2 md:grid-cols-2">
          <label className="text-xs md:col-span-2">
            <span className="block text-slate-600">GitHub 網址</span>
            <input className="w-full rounded border border-slate-300 px-2 py-1 text-sm" placeholder="https://github.com/<owner>/<repo>" value={repoUrl} onChange={(event) => setRepoUrl(event.target.value)} />
            {repoUrl && !urlValid ? <span role="alert" className="text-rose-700">只接受 https://github.com/&lt;owner&gt;/&lt;repo&gt; 形式的網址</span> : null}
          </label>
          <label className="text-xs">
            <span className="block text-slate-600">專案名稱（選填，預設 {suggestedName || "repo 名稱"}）</span>
            <input className="w-full rounded border border-slate-300 px-2 py-1 text-sm" value={project} onChange={(event) => setProject(event.target.value)} />
          </label>
          <label className="text-xs">
            <span className="block text-slate-600">目標機</span>
            <select className="w-full rounded border border-slate-300 p-1 text-sm" value={effectiveServer} onChange={(event) => setServer(event.target.value)}>
              {servers.map((config) => <option key={config.name} value={config.name}>{config.name}</option>)}
            </select>
          </label>
          <label className="text-xs">
            <span className="block text-slate-600">目的路徑（選填，預設為目標機 project_roots 下的專案名稱）</span>
            <input className="w-full rounded border border-slate-300 px-2 py-1 text-sm" value={destPath} onChange={(event) => setDestPath(event.target.value)} />
          </label>
          <label className="text-xs">
            <span className="block text-slate-600">分支（選填，預設為 repo 預設分支）</span>
            <input className="w-full rounded border border-slate-300 px-2 py-1 text-sm" value={ref} onChange={(event) => setRef(event.target.value)} />
          </label>
          <div className="flex flex-wrap items-center gap-2 md:col-span-2">
            <Button disabled={!urlValid || !effectiveServer || request.isPending} onClick={() => request.mutate(false)}>建立匯入卡</Button>
            <Button variant="primary" disabled={!urlValid || !effectiveServer || request.isPending} onClick={() => request.mutate(true)}>確認並匯入</Button>
            <button type="button" className="text-xs text-sky-700 hover:underline" aria-expanded={showGuide} onClick={() => setShowGuide((value) => !value)}>還沒有 GitHub 專案？</button>
            {request.error ? <span role="alert" className="text-xs text-rose-700">{errorText(request.error)}</span> : null}
          </div>
        </div>
      )}
      {showGuide ? (
        <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700">
          <ol className="list-decimal space-y-1 pl-4">
            <li>到 GitHub 建立新的 repository（github.com/new），勾選「Add a README file」。</li>
            <li>把 README.md 寫清楚：專案目的、資料、執行方式（可用下方範本）。</li>
            <li>回到這裡貼上 repository 網址，選目標機，建立匯入卡。私有專案需要先在目標機設定 deploy key。</li>
          </ol>
          <pre className="max-h-48 overflow-auto rounded bg-white p-2">{README_TEMPLATE}</pre>
        </div>
      ) : null}
    </Card>
  );
}
