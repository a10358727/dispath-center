import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useWorkspace } from "@/api/hooks";
import type { Approval, TemplateParameter } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { TargetReadiness } from "@/features/project/TargetReadiness";
import { buildExperimentBody, buildRunBody, composeError, runCount, splitParameters } from "./compose";
import { ServerChips } from "./ServerChips";

interface ExperimentPreview {
  run_count: number;
  plan_digests: string[];
  members: { target_server: string; parameter_values: Record<string, unknown>; plan_digest: string }[];
}

/** 快速指令: the legacy enqueue path the pilot actually uses — no version,
 *  template or environment needed; under WEB_DIRECT_EXECUTE the browser's
 *  own request is decided in the same call (auto_approved), otherwise the
 *  card renders here. */
function QuickCommand({ projectName, servers, onQueued }: { projectName: string; servers: string[]; onQueued: () => void }) {
  const [command, setCommand] = useState("");
  const [server, setServer] = useState(servers[0] ?? "");
  const [gpus, setGpus] = useState<number | "">("");
  const [approval, setApproval] = useState<Approval | null>(null);
  const [queuedJob, setQueuedJob] = useState<number | null>(null);
  const submit = useMutation({
    mutationFn: () =>
      api<{ approval: Approval; auto_approved?: boolean; job?: { id: number } | null }>("/api/v2/dispatch-requests", {
        method: "POST",
        json: { command, project: projectName, pin_server: server || null, gpus_needed: gpus === "" ? null : gpus, source: "web", type: "adhoc" },
      }),
    onSuccess: (result) => {
      if (result.auto_approved && result.job?.id) {
        setQueuedJob(result.job.id);
        setCommand("");
        onQueued();
      } else {
        setApproval(result.approval);
      }
    },
  });
  return (
    <div className="space-y-2">
      <div className="text-xs text-slate-500">不需版本或模板；直接在專案副本目錄執行這條指令，會留下稽核。危險指令在建立當下就會被拒絕。</div>
      <input
        className="w-full rounded border border-slate-300 p-1.5 font-mono text-xs"
        placeholder="python train.py --lr 0.1"
        value={command}
        onChange={(event) => setCommand(event.target.value)}
      />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label className="flex items-center gap-1">
          <span className="text-slate-600">執行機器</span>
          <select className="rounded border border-slate-300 p-1" value={server} onChange={(event) => setServer(event.target.value)}>
            {servers.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1">
          <span className="text-slate-600">GPU 數（選填）</span>
          <input type="number" min={0} className="w-16 rounded border border-slate-300 p-1" value={gpus} onChange={(event) => setGpus(event.target.value === "" ? "" : Number(event.target.value))} />
        </label>
        <Button variant="primary" disabled={!command.trim() || submit.isPending} onClick={() => submit.mutate()}>
          立即執行
        </Button>
        {submit.error ? <span className="text-xs text-rose-700">{(submit.error as Error).message}</span> : null}
        {queuedJob != null ? <span className="text-xs text-emerald-700">已排入任務 #{queuedJob}</span> : null}
      </div>
      {approval ? <ApprovalCard approval={approval} onDecided={() => { setApproval(null); onQueued(); }} /> : null}
    </div>
  );
}

/** One run form (整頓 U6): pick the version and the machines, give each
 *  parameter one value or a list; lists expand into an experiment matrix,
 *  otherwise it is a single Run. Preview → one card, decided in place. */
export function RunComposer({ projectId, projectName, onCreated }: { projectId: string; projectName: string; onCreated: () => void }) {
  const workspace = useWorkspace(projectId);
  const [mode, setMode] = useState<"template" | "quick">("template");
  const [values, setValues] = useState<Record<string, string>>({});
  const [servers, setServers] = useState<string[]>([]);
  const [versionId, setVersionId] = useState("");
  const [experimentPreview, setExperimentPreview] = useState<ExperimentPreview | null>(null);
  const [runPreview, setRunPreview] = useState<{ plan_digest: string } | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [confirmNow, setConfirmNow] = useState(false);

  const parameters: TemplateParameter[] = workspace.data?.run_template?.parameters ?? [];
  const versions = workspace.data?.run_creation_options?.project_version_candidates ?? [];
  const candidates = workspace.data?.run_creation_options?.ssh_target_candidates ?? [];
  const effectiveVersion = versionId || versions[0]?.id || "";
  const hasDefaults = Boolean(workspace.data?.defaults?.revision_id);
  const split = splitParameters(values, parameters);
  const isMatrix = split.axes.length > 0;
  const error = composeError(split, parameters, hasDefaults, servers, effectiveVersion);
  const targetRevisionId = (candidates.find((candidate) => candidate.server_name === servers[0]) as { id?: string } | undefined)?.id ?? "";
  const reset = () => {
    setExperimentPreview(null);
    setRunPreview(null);
    setApproval(null);
  };

  const preview = useMutation({
    mutationFn: async () => {
      if (isMatrix) {
        const result = await api<ExperimentPreview>(`/api/v2/projects/${projectId}/experiment-previews`, { method: "POST", json: buildExperimentBody({ versionId: effectiveVersion, workspace: workspace.data, split, servers }) });
        setExperimentPreview(result);
      } else {
        const result = await api<{ plan_digest: string }>(`/api/v2/projects/${projectId}/run-previews`, { method: "POST", json: buildRunBody({ versionId: effectiveVersion, workspace: workspace.data, split, targetRevisionId }) });
        setRunPreview(result);
      }
    },
  });
  const request = useMutation({
    mutationFn: async (confirm: boolean) => {
      setConfirmNow(confirm);
      const headers = { "Idempotency-Key": crypto.randomUUID() };
      const created = isMatrix
        ? await api<{ approval_id: number }>(`/api/v2/projects/${projectId}/experiment-requests`, {
            method: "POST",
            json: { ...buildExperimentBody({ versionId: effectiveVersion, workspace: workspace.data, split, servers }), expected_plan_digests: experimentPreview?.plan_digests ?? null },
            headers,
          })
        : await api<{ approval_id: number }>(`/api/v2/projects/${projectId}/run-requests`, {
            method: "POST",
            json: { ...buildRunBody({ versionId: effectiveVersion, workspace: workspace.data, split, targetRevisionId }), expected_plan_digest: runPreview?.plan_digest },
            headers,
          });
      return api<Approval>(`/api/v2/approvals/${created.approval_id}`);
    },
    onSuccess: (card) => setApproval(card),
  });
  const mutError = (preview.error ?? request.error) as Error | undefined;
  const previewed = isMatrix ? experimentPreview != null : runPreview != null;
  const allServerNames = candidates.map((candidate) => candidate.server_name);

  return (
    <Card className="space-y-3">
      <div className="flex items-center gap-3">
        <CardTitle>執行</CardTitle>
        <div className="flex gap-1 text-xs">
          {([["template", "模板 Run／實驗"], ["quick", "快速指令"]] as const).map(([key, label]) => (
            <button key={key} type="button" className={`rounded-full border px-2 py-0.5 ${mode === key ? "border-sky-600 bg-sky-50 text-sky-800" : "border-slate-300 text-slate-600"}`} onClick={() => setMode(key)}>
              {label}
            </button>
          ))}
        </div>
      </div>
      {mode === "quick" ? (
        <QuickCommand projectName={projectName} servers={allServerNames} onQueued={onCreated} />
      ) : !workspace.data?.run_template ? (
        <div className="text-sm text-amber-700">這個專案還沒有執行模板；先到專案頁的「執行設定」建立環境與模板，或改用「快速指令」。</div>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="block text-sm">
              <span className="text-slate-600">版本</span>
              <select className="mt-1 w-full rounded border border-slate-300 p-1.5" value={effectiveVersion} onChange={(event) => { setVersionId(event.target.value); reset(); }}>
                {versions.map((version) => (
                  <option key={version.id} value={version.id}>
                    {version.id.slice(0, 8)} · {version.state ?? ""} · {version.created_at ?? ""}
                  </option>
                ))}
              </select>
              {versions.length === 0 ? <div className="text-xs text-amber-700">還沒有已晉升的正式版本。</div> : null}
            </label>
            <div className="text-sm">
              <span className="text-slate-600">模板</span>
              <div className="mt-1 rounded border border-slate-200 bg-slate-50 p-1.5 text-xs">
                {workspace.data.run_template.name}
                <div className="text-slate-500">{hasDefaults ? "沒填的參數用專案預設參數。" : "沒有預設參數：每個參數都要填。"}</div>
              </div>
            </div>
          </div>
          <div className="space-y-2">
            <span className="text-sm text-slate-600">參數（一個值＝單一 Run；用逗號給多個值＝展開成實驗，最多 32 個 run）</span>
            {parameters.map((parameter) => (
              <label key={parameter.name} className="flex flex-wrap items-center gap-2 text-sm">
                <span className="w-32 text-slate-600">
                  {parameter.name} <span className="text-slate-400">（{parameter.type ?? "string"}）</span>
                </span>
                <input
                  className="min-w-64 flex-1 rounded border border-slate-300 p-1.5 text-sm"
                  placeholder={parameter.type === "number" ? "例：0.1 或 0.1, 0.01" : parameter.type === "integer" ? "例：16 或 16, 32" : ""}
                  value={values[parameter.name] ?? ""}
                  onChange={(event) => { setValues({ ...values, [parameter.name]: event.target.value }); reset(); }}
                />
                {split.errors[parameter.name] ? <span className="text-xs text-rose-700">{split.errors[parameter.name]}</span> : null}
              </label>
            ))}
          </div>
          <div className="space-y-1">
            <span className="text-sm text-slate-600">{isMatrix ? "執行機器（run i → 第 i%N 台，輪流分配）" : "執行機器"}</span>
            <ServerChips
              candidates={candidates}
              selected={servers}
              single={!isMatrix}
              onToggle={(name) => { setServers(isMatrix ? (servers.includes(name) ? servers.filter((s) => s !== name) : [...servers, name]) : [name]); reset(); }}
            />
            {candidates.some((candidate) => candidate.ready === false) ? (
              <TargetReadiness projectId={projectId} targets={candidates.filter((candidate) => candidate.ready === false)} latestPromotedVersionId={versions[0]?.id ?? null} />
            ) : null}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Badge tone={error ? "neutral" : "info"}>{isMatrix ? `展開 ${runCount(split)} 個 run` : "單一 Run"}</Badge>
            {error ? <span className="text-xs text-amber-700">{error}</span> : null}
            <Button disabled={Boolean(error) || preview.isPending} onClick={() => preview.mutate()}>
              預覽
            </Button>
            <Button disabled={!previewed || request.isPending || Boolean(error)} onClick={() => request.mutate(false)}>
              建立核准卡
            </Button>
            <Button variant="primary" disabled={!previewed || request.isPending || Boolean(error)} onClick={() => request.mutate(true)} title="預覽即審閱：建卡後由你本人立即核准（DG-SINGLE-OPERATOR-CONFIRM v1）">
              確認並執行
            </Button>
            {mutError ? <span className="text-xs text-rose-700">{mutError.message}</span> : null}
          </div>
          {experimentPreview ? (
            <div className="rounded border border-slate-200 bg-slate-50 p-2 text-xs">
              <div className="mb-1 font-medium">預覽：{experimentPreview.run_count} 個 run</div>
              <table className="w-full">
                <tbody>
                  {experimentPreview.members.map((member, index) => (
                    <tr key={member.plan_digest ?? index}>
                      <td className="pr-2 text-slate-500">#{index + 1}</td>
                      <td className="pr-2">{member.target_server}</td>
                      <td className="font-mono">{Object.entries(member.parameter_values).map(([key, value]) => `${key}=${String(value)}`).join(" ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {runPreview ? <div className="text-xs text-emerald-700">預覽通過，可以建立核准卡。</div> : null}
          {approval ? <ApprovalCard approval={approval} confirmImmediately={confirmNow} onDecided={() => { setApproval(null); setConfirmNow(false); onCreated(); }} /> : null}
        </>
      )}
    </Card>
  );
}
