import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/api/client";
import { useProjects, useServerConfigs, useVersions, useWorkspace } from "@/api/hooks";
import type { Approval, EnvironmentHead } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { shortCommit } from "@/lib";
import { ReasonList, TargetReadiness } from "./TargetReadiness";
import { canonicalParameterValue, compileCommandTemplate } from "./template";

/** Renders the safe validation envelope (`error.details.errors[]`) inline. */
function ErrorLines({ error }: { error: unknown }) {
  if (!error) return null;
  const details = error instanceof ApiError ? (error.details as { errors?: { location?: string[]; message?: string }[] } | undefined) : undefined;
  const lines = details?.errors?.map((item) => `${(item.location ?? []).join(".")}：${item.message ?? ""}`) ?? [];
  return (
    <div className="text-xs text-rose-700">
      <div>{(error as Error).message}</div>
      {lines.map((line) => (
        <div key={line}>{line}</div>
      ))}
    </div>
  );
}

function Step({ index, title, done, children }: { index: number; title: string; done: boolean | null; children?: React.ReactNode }) {
  return (
    <Card className="space-y-2">
      <div className="flex items-center gap-2">
        <span className={done ? "text-emerald-600" : done === false ? "text-amber-600" : "text-slate-400"}>{done ? "✓" : done === false ? "○" : "…"}</span>
        <CardTitle>
          {index}. {title}
        </CardTitle>
      </div>
      {children}
    </Card>
  );
}

/** 執行設定 checklist for one project (整頓 U4a): register the current
 *  version (direct, audited hub-sync), then create the Environment revision
 *  through its approval card. Template/defaults (U4b) and instance sync (U5)
 *  hang off the same list. */
export function SetupPanel({ project }: { project: string }) {
  const client = useQueryClient();
  const projects = useProjects();
  const projectRow = (projects.data ?? []).find((row) => row.name === project);
  const projectId = projectRow?.id;
  const versions = useVersions(project);
  const workspace = useWorkspace(projectId);
  const configs = useServerConfigs();
  const environments = useQuery({
    queryKey: ["environments", projectId ?? ""],
    enabled: Boolean(projectId) && Boolean(workspace.data),
    queryFn: async () => (await api<{ items: EnvironmentHead[] }>(`/api/v2/projects/${encodeURIComponent(projectId ?? "")}/environments`)).items ?? [],
  });

  const servers = (projectRow?.instances ?? []).map((instance) => instance.server).filter((name): name is string => Boolean(name));
  const [syncServer, setSyncServer] = useState<string>("");
  const hubSync = useMutation({
    mutationFn: (server: string) => api<Record<string, unknown>>(`/api/v2/legacy-projects/${encodeURIComponent(project)}/hub-sync`, { method: "POST", json: { server } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["versions", project] });
      void client.invalidateQueries({ queryKey: ["workspace"] });
      void client.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const [confirmNow, setConfirmNow] = useState(false);
  const [envForm, setEnvForm] = useState({ name: "default", setup_command: "", tags: [] as string[] });
  const [envApproval, setEnvApproval] = useState<Approval | null>(null);
  const createEnvironment = useMutation({
    mutationFn: async (confirm: boolean) => {
      setConfirmNow(confirm);
      const created = await api<{ approval_id: number }>(`/api/v2/projects/${encodeURIComponent(projectId ?? "")}/environment-change-requests`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        json: {
          operation: "create",
          expected_revision: 0,
          expected_head_revision_id: null,
          environment: {
            name: envForm.name,
            setup_command: envForm.setup_command,
            required_server_tags: envForm.tags,
            working_directory_policy: "project_checkout",
            non_secret_env: [],
            secret_references: [],
            preflight_checks: [],
          },
        },
      });
      return api<Approval>(`/api/v2/approvals/${created.approval_id}`);
    },
    onSuccess: (approval) => setEnvApproval(approval),
  });

  //: 整頓 U4b — template + defaults, each one card, chained on the environment head.
  const [templateForm, setTemplateForm] = useState({ name: "train", command: "", min_gpu_count: 1 });
  const [templateApproval, setTemplateApproval] = useState<Approval | null>(null);
  const compiled = compileCommandTemplate(templateForm.command);
  const createTemplate = useMutation({
    mutationFn: async ({ environmentRevisionId, confirm }: { environmentRevisionId: string; confirm: boolean }) => {
      setConfirmNow(confirm);
      const created = await api<{ approval_id: number }>(`/api/v2/projects/${encodeURIComponent(projectId ?? "")}/run-template-change-requests`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        json: {
          operation: "create",
          expected_revision: 0,
          expected_head_run_profile_id: null,
          expected_head_classification: null,
          expected_head_spec_digest: null,
          expected_environment_head_revision_id: environmentRevisionId,
          template: {
            name: templateForm.name,
            argv_template: compiled.argv_template,
            parameter_schema: compiled.parameter_schema,
            resource_requirements: {
              required_tags: workspace.data?.environment?.required_server_tags ?? [],
              required_devices: [],
              min_gpu_count: templateForm.min_gpu_count,
              min_gpu_memory_mb: 0,
              min_available_ram_mb: 0,
              min_available_disk_mb: 0,
              exclusive_worker: true,
            },
            output_declarations: [],
          },
        },
      });
      return api<Approval>(`/api/v2/approvals/${created.approval_id}`);
    },
    onSuccess: (approval) => setTemplateApproval(approval),
  });

  const [defaultsForm, setDefaultsForm] = useState<Record<string, string>>({});
  const [defaultsApproval, setDefaultsApproval] = useState<Approval | null>(null);
  const createDefaults = useMutation({
    mutationFn: async (input: { templateId: string; specDigest: string; environmentRevisionId: string; values: Record<string, string | number | boolean>; confirm: boolean }) => {
      setConfirmNow(input.confirm);
      const created = await api<{ approval_id: number }>(`/api/v2/projects/${encodeURIComponent(projectId ?? "")}/default-change-requests`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        json: {
          operation: "create",
          expected_revision: 0,
          expected_head_revision_id: null,
          expected_head_revision_digest: null,
          expected_template_head_run_profile_id: input.templateId,
          expected_template_spec_digest: input.specDigest,
          expected_environment_head_revision_id: input.environmentRevisionId,
          defaults: { parameter_values: input.values },
        },
      });
      return api<Approval>(`/api/v2/approvals/${created.approval_id}`);
    },
    onSuccess: (approval) => setDefaultsApproval(approval),
  });

  if (projects.isLoading || (projectId && workspace.isLoading)) return <div className="p-4 text-sm text-slate-400">載入中…</div>;
  if (!projectId) return <div className="p-4 text-sm text-amber-700">後端還沒回報這個專案的識別碼，重新整理後再試。</div>;
  if (workspace.error instanceof ApiError && workspace.error.status === 404) {
    return <div className="p-4 text-sm text-amber-700">執行設定功能未啟用（Server A 的 Project bootstrap v2 未開）。</div>;
  }

  const versionList = versions.data ?? [];
  const promoted = versionList.filter((version) => version.promotion_state === "promoted");
  const environment = workspace.data?.environment ?? null;
  const template = workspace.data?.run_template ?? null;
  const defaults = workspace.data?.defaults ?? null;
  const targets = workspace.data?.run_creation_options?.ssh_target_candidates ?? [];
  //: `project_version_candidates` lists only promoted versions (newest first).
  const latestPromotedVersionId = workspace.data?.run_creation_options?.project_version_candidates?.[0]?.id ?? null;
  const allTags = Array.from(new Set((configs.data ?? []).flatMap((config) => config.tags ?? []))).sort();
  const envHead = (environments.data ?? []).find((item) => item.environment_id === environment?.id) ?? null;

  return (
    <div className="space-y-3 p-4" data-testid="setup-panel">
      <div className="text-sm text-slate-600">跑實驗前需要：一個正式版本、一個環境、一個執行模板，以及至少一台已同步的執行機器。每一步都在這裡完成。</div>

      <Step index={1} title="登記版本" done={versions.isLoading ? null : versionList.length > 0}>
        {versionList.length > 0 ? (
          <div className="text-xs text-slate-600">
            已登記 {versionList.length} 個版本，其中 {promoted.length} 個已晉升
            {versionList[0] ? <span className="text-slate-400">（最新 {shortCommit(versionList[0].git_commit)}）</span> : null}。
            {promoted.length === 0 ? <div className="text-amber-700">還沒有已晉升的正式版本：開一個 session 改一行 → 存檔 → 晉升。</div> : null}
          </div>
        ) : (
          <div className="text-xs text-slate-600">還沒有任何版本。從一台已有專案副本的機器登記目前的 commit（直接執行、留稽核）：</div>
        )}
        {servers.length > 0 ? (
          <div className="flex flex-wrap items-center gap-2">
            <select className="rounded border border-slate-300 p-1 text-sm" value={syncServer || servers[0]} onChange={(event) => setSyncServer(event.target.value)}>
              {servers.map((server) => (
                <option key={server} value={server}>
                  {server}
                </option>
              ))}
            </select>
            <Button disabled={hubSync.isPending} onClick={() => hubSync.mutate(syncServer || servers[0])}>
              登記目前版本
            </Button>
            <ErrorLines error={hubSync.error} />
          </div>
        ) : (
          <div className="text-xs text-slate-500">這個專案還沒有任何機器上的副本。</div>
        )}
      </Step>

      <Step index={2} title="環境" done={environment ? true : false}>
        {environment ? (
          <div className="space-y-1 text-xs text-slate-600">
            <div>
              {environment.name} · 第 {environment.revision} 版
              {environment.required_server_tags?.length ? <span className="text-slate-400">（標籤：{environment.required_server_tags.join("、")}）</span> : null}
            </div>
            {envHead?.readiness ? (
              envHead.readiness.state === "ready" ? (
                <div className="text-emerald-700">至少一台機器符合這個環境。</div>
              ) : (
                <ReasonList reasons={envHead.readiness.reasons ?? []} />
              )
            ) : null}
          </div>
        ) : envApproval ? (
          <ApprovalCard
            approval={envApproval}
            confirmImmediately={confirmNow}
            onDecided={() => {
              setEnvApproval(null);
              setConfirmNow(false);
              void client.invalidateQueries({ queryKey: ["workspace"] });
              void client.invalidateQueries({ queryKey: ["environments"] });
            }}
          />
        ) : (
          <div className="space-y-2">
            <label className="block text-sm">
              <span className="text-slate-600">名稱</span>
              <input className="mt-1 w-full rounded border border-slate-300 p-1.5" value={envForm.name} onChange={(event) => setEnvForm({ ...envForm, name: event.target.value })} placeholder="1–64 個英數、. _ -" />
            </label>
            <label className="block text-sm">
              <span className="text-slate-600">環境準備指令（在專案副本目錄執行，可多行）</span>
              <textarea className="mt-1 w-full rounded border border-slate-300 p-1.5 font-mono text-xs" rows={3} value={envForm.setup_command} onChange={(event) => setEnvForm({ ...envForm, setup_command: event.target.value })} placeholder="pip install -r requirements.txt" />
            </label>
            {allTags.length > 0 ? (
              <div className="text-sm">
                <span className="text-slate-600">需要的機器標籤</span>
                <div className="mt-1 flex flex-wrap gap-2">
                  {allTags.map((tag) => {
                    const picked = envForm.tags.includes(tag);
                    return (
                      <button
                        key={tag}
                        type="button"
                        className={`rounded-full border px-2 py-0.5 text-xs ${picked ? "border-sky-600 bg-sky-50 text-sky-800" : "border-slate-300 text-slate-700"}`}
                        onClick={() => setEnvForm({ ...envForm, tags: picked ? envForm.tags.filter((t) => t !== tag) : [...envForm.tags, tag] })}
                      >
                        {tag}
                      </button>
                    );
                  })}
                </div>
              </div>
            ) : null}
            <div className="flex items-center gap-2">
              <Button disabled={createEnvironment.isPending || !envForm.name} onClick={() => createEnvironment.mutate(false)}>
                建立環境（一張核准卡）
              </Button>
              <Button variant="primary" disabled={createEnvironment.isPending || !envForm.name} onClick={() => createEnvironment.mutate(true)}>
                確認並執行
              </Button>
              <ErrorLines error={createEnvironment.error} />
            </div>
          </div>
        )}
      </Step>

      <Step index={3} title="執行模板" done={template ? true : false}>
        {template ? (
          <div className="text-xs text-slate-600">
            {template.name} · 第 {template.revision} 版（參數：{(template.parameters ?? []).map((parameter) => parameter.name).join("、") || "無"}）
          </div>
        ) : templateApproval ? (
          <ApprovalCard
            approval={templateApproval}
            confirmImmediately={confirmNow}
            onDecided={() => {
              setTemplateApproval(null);
              setConfirmNow(false);
              void client.invalidateQueries({ queryKey: ["workspace"] });
            }}
          />
        ) : !environment?.revision_id ? (
          <div className="text-xs text-slate-600">先建立環境。</div>
        ) : (
          <div className="space-y-2">
            <label className="block text-sm">
              <span className="text-slate-600">名稱</span>
              <input className="mt-1 w-full rounded border border-slate-300 p-1.5" value={templateForm.name} onChange={(event) => setTemplateForm({ ...templateForm, name: event.target.value })} />
            </label>
            <label className="block text-sm">
              <span className="text-slate-600">指令（參數寫成 {"{名稱:型別}"}；型別 string／integer／number／boolean／enum(a|b)）</span>
              <input
                className="mt-1 w-full rounded border border-slate-300 p-1.5 font-mono text-xs"
                value={templateForm.command}
                onChange={(event) => setTemplateForm({ ...templateForm, command: event.target.value })}
                placeholder="python train.py --lr {lr:number} --epochs {epochs:integer}"
              />
            </label>
            {templateForm.command && compiled.errors.length === 0 ? (
              <div className="text-xs text-slate-500">參數：{compiled.parameter_schema.map((spec) => `${spec.name}（${spec.type}）`).join("、") || "無"}</div>
            ) : null}
            {compiled.errors.map((line) => (
              <div key={line} className="text-xs text-rose-700">
                {line}
              </div>
            ))}
            <label className="block text-sm">
              <span className="text-slate-600">最少 GPU 數</span>
              <input type="number" min={0} max={64} className="mt-1 w-24 rounded border border-slate-300 p-1.5" value={templateForm.min_gpu_count} onChange={(event) => setTemplateForm({ ...templateForm, min_gpu_count: Number(event.target.value) })} />
            </label>
            <div className="flex items-center gap-2">
              <Button disabled={createTemplate.isPending || !templateForm.name || !templateForm.command || compiled.errors.length > 0} onClick={() => createTemplate.mutate({ environmentRevisionId: environment.revision_id as string, confirm: false })}>
                建立模板（一張核准卡）
              </Button>
              <Button variant="primary" disabled={createTemplate.isPending || !templateForm.name || !templateForm.command || compiled.errors.length > 0} onClick={() => createTemplate.mutate({ environmentRevisionId: environment.revision_id as string, confirm: true })}>
                確認並執行
              </Button>
              <ErrorLines error={createTemplate.error} />
            </div>
          </div>
        )}
      </Step>

      <Step index={4} title="預設參數（選填）" done={defaults ? true : template ? false : null}>
        {defaults ? (
          <div className="text-xs text-slate-600">已設定專案預設參數。</div>
        ) : defaultsApproval ? (
          <ApprovalCard
            approval={defaultsApproval}
            confirmImmediately={confirmNow}
            onDecided={() => {
              setDefaultsApproval(null);
              setConfirmNow(false);
              void client.invalidateQueries({ queryKey: ["workspace"] });
            }}
          />
        ) : !template?.id || !template.spec_digest || !environment?.revision_id ? (
          <div className="text-xs text-slate-600">沒有預設參數時，每次跑 Run 需填齊所有參數。先建立模板。</div>
        ) : (template.parameters ?? []).length === 0 ? (
          <div className="text-xs text-slate-600">這個模板沒有參數，不需要預設值。</div>
        ) : (
          <div className="space-y-2">
            {(template.parameters ?? []).map((parameter) => {
              const check = canonicalParameterValue({ type: parameter.type as never, enum_values: parameter.enum_values }, defaultsForm[parameter.name] ?? "");
              return (
                <label key={parameter.name} className="block text-sm">
                  <span className="text-slate-600">
                    {parameter.name} <span className="text-slate-400">（{parameter.type}）</span>
                  </span>
                  <input className="mt-1 w-full rounded border border-slate-300 p-1.5 font-mono text-xs" value={defaultsForm[parameter.name] ?? ""} onChange={(event) => setDefaultsForm({ ...defaultsForm, [parameter.name]: event.target.value })} />
                  {defaultsForm[parameter.name] && check.error ? <span className="text-xs text-rose-700">{check.error}</span> : null}
                </label>
              );
            })}
            <div className="flex items-center gap-2">
              {([false, true] as const).map((confirm) => (
                <Button
                  key={String(confirm)}
                  variant={confirm ? "primary" : undefined}
                  disabled={createDefaults.isPending || (template.parameters ?? []).some((parameter) => canonicalParameterValue({ type: parameter.type as never, enum_values: parameter.enum_values }, defaultsForm[parameter.name] ?? "").error !== undefined)}
                  onClick={() => {
                    const values: Record<string, string | number | boolean> = {};
                    for (const parameter of template.parameters ?? []) {
                      const check = canonicalParameterValue({ type: parameter.type as never, enum_values: parameter.enum_values }, defaultsForm[parameter.name] ?? "");
                      if (check.value !== undefined) values[parameter.name] = check.value;
                    }
                    createDefaults.mutate({ templateId: template.id as string, specDigest: template.spec_digest as string, environmentRevisionId: environment.revision_id as string, values, confirm });
                  }}
                >
                  {confirm ? "確認並執行" : "設定預設參數（一張核准卡）"}
                </Button>
              ))}
              <ErrorLines error={createDefaults.error} />
            </div>
          </div>
        )}
      </Step>

      <Step index={5} title="執行機器" done={targets.length === 0 ? null : targets.some((target) => target.ready)}>
        <TargetReadiness projectId={projectId} targets={targets} latestPromotedVersionId={latestPromotedVersionId} />
      </Step>
    </div>
  );
}
