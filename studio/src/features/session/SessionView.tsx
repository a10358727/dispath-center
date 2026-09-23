import { useMemo, useState } from "react";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { promotableCheckpointTasks, useCheckpointTasks, useSession, useSessionActions } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { PromotePanel } from "./PromotePanel";
import { Transcript } from "./Transcript";
import { buildTranscript, RUN_ANALYSIS_MARKER, RUN_CONTINUE_MARKER, type RunAnalysis } from "./transcript";
import { useSessionStream } from "./useSessionStream";
import { MODEL_CHOICES, PERMISSION_MODE_CHOICES } from "./SessionOptionsFields";
import { latestContextUsage } from "./context";

const contextLabel = (label: string): string => (label === "Estimated" ? "估計值" : label === "Provider-reported" ? "供應商回報" : label);

export function SessionView({
  sessionId,
  onFork,
}: {
  sessionId: string;
  onFork?: (info: { sessionId: string; baseVersionId: string | null; runnerId: string | null }) => void;
}) {
  const session = useSession(sessionId);
  const actions = useSessionActions(sessionId);
  const { events, connected } = useSessionStream(sessionId);
  const items = useMemo(() => buildTranscript(events), [events]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<{ media_type: string; data_base64: string; bytes: number }[]>([]);
  const [fileList, setFileList] = useState<string[] | null>(null);
  const [diffOpen, setDiffOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [contextInvalidAfterSeq, setContextInvalidAfterSeq] = useState(-1);
  const [failedSend, setFailedSend] = useState<{ text: string; attachments?: { type: "image"; media_type: string; data_base64: string }[] } | null>(null);
  const [checkpointApproval, setCheckpointApproval] = useState<Approval | null>(null);
  //: 整頓 U3: the checkpoint decision response carries the bridge task id;
  //: after a reload the same task is found through the project's task list.
  const [promoteTaskId, setPromoteTaskId] = useState<string | null>(null);
  const bridgeTasks = useCheckpointTasks(session.data?.project_id);
  const recoveredPromoteTaskId = promotableCheckpointTasks(bridgeTasks.data, sessionId)[0]?.id ?? null;
  const activePromoteTaskId = promoteTaskId ?? recoveredPromoteTaskId;
  const runtime = session.data?.runtime;
  const options = runtime?.options ?? {};
  const state = runtime?.task_state ?? null;
  const context = useMemo(() => latestContextUsage(events, contextInvalidAfterSeq), [events, contextInvalidAfterSeq]);
  const slashCommands = useMemo(() => {
    const names = new Set<string>(["diff"]);
    for (const event of events) {
      if (event.kind === "system" && event.payload.subtype === "init") {
        const data = event.payload.data as Record<string, unknown> | undefined;
        for (const name of Array.isArray(data?.slash_commands) ? (data?.slash_commands as unknown[]) : []) {
          if (typeof name === "string") names.add(name);
        }
      }
    }
    return [...names].sort();
  }, [events]);
  const slashMatches = draft.startsWith("/") && !draft.includes(" ")
    ? slashCommands.filter((name) => `/${name}`.startsWith(draft)).slice(0, 8)
    : [];
  const closed = session.data?.status === "closed";
  const startable = !closed && (state == null || state === "unknown" || state === "failed");
  const sessionUnavailable = session.isError || (!startable && !runtime?.runner_connected);
  const sendable = !closed && !startable && !sessionUnavailable && (draft.trim().length > 0 || pending.length > 0) && !actions.send.isPending;
  const streaming = items.some((item) => item.type === "assistant" && item.streaming);
  const permissionRequired = (session.data?.pending_permissions.length ?? 0) > 0;
  const interactionState = session.isLoading ? "載入 session…" : sessionUnavailable ? "session 不可用" : actions.send.isPending ? "傳送中" : permissionRequired ? "需要授權" : streaming ? "串流中" : state === "working" || state === "submitted" ? "等待 agent" : "就緒";
  const addImages = (files: FileList | File[]) => {
    for (const file of Array.from(files)) {
      if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type) || file.size > 3 * 1024 * 1024 || pending.length >= 4) continue;
      const reader = new FileReader();
      reader.onload = () => {
        const data = String(reader.result ?? "");
        const base64 = data.slice(data.indexOf(",") + 1);
        setPending((prev) => (prev.length >= 4 ? prev : [...prev, { media_type: file.type, data_base64: base64, bytes: file.size }]));
      };
      reader.readAsDataURL(file);
    }
  };
  const mentionPrefix = (() => {
    const match = /@([A-Za-z0-9_./-]*)$/.exec(draft);
    return match ? match[1] : null;
  })();
  const mentionMatches = mentionPrefix != null && fileList ? fileList.filter((f) => f.includes(mentionPrefix)).slice(0, 8) : [];

  const sendPayload = (payload: { text: string; attachments?: { type: "image"; media_type: string; data_base64: string }[] }) => {
    setFailedSend(null);
    actions.send.mutate(payload, {
      onSuccess: () => {
        setDraft((current) => current.trim() === payload.text ? "" : current);
        setPending([]);
      },
      onError: () => setFailedSend(payload),
    });
  };
  const submit = () => {
    const text = draft.trim();
    if (text === "/diff") {
      setDraft("");
      setDiffOpen(true);
      actions.diff.mutate();
      return;
    }
    if ((!text && pending.length === 0) || !sendable) return;
    const attachments = pending.map((item) => ({ type: "image" as const, media_type: item.media_type, data_base64: item.data_base64 }));
    sendPayload({ text: text || "（附圖）", attachments: attachments.length ? attachments : undefined });
  };
  const analyzeRun = (planId: string) => sendPayload({
    text: `${RUN_ANALYSIS_MARKER}${planId}] Analyze the exact verified Product Run ${planId}. Use only the bounded structured Run evidence tools (get_run, get_run_metrics, get_run_artifacts, get_run_log_tail, and compare_runs when relevant). Identify the evidence used, preserve missing or unavailable evidence as unknown, recommend one next step, then stop. Do not request or start another Run.`,
  });
  const continueFromAnalysis = (analysis: RunAnalysis) => sendPayload({
    text: `${RUN_CONTINUE_MARKER}${analysis.planId}] Continue in this same Project session from the completed analysis of Run ${analysis.planId}. Treat this prior recommendation as context, verify before acting, and do not start another Run without a new governed request_run proposal and human approval. Prior recommendation:\n${(analysis.text ?? "Unavailable").slice(0, 1200)}`,
  });
  const error = [actions.start, actions.send, actions.interrupt, actions.close, actions.decide, actions.diff, actions.configure, actions.checkpoint].map((m) => m.error).find(Boolean) as Error | undefined;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-white px-4 py-2 text-sm">
        <span className="font-semibold">{session.data?.workspace_branch ?? sessionId.slice(0, 8)}</span>
        <Badge tone={stateTone(closed ? "canceled" : state)}>{closed ? "已關閉" : (state ?? "尚未啟動")}</Badge>
        <Badge tone={runtime?.runner_connected ? "ok" : "neutral"}>{runtime?.runner_connected ? "runner 已連線" : "runner 離線"}</Badge>
        <Badge tone={connected ? "ok" : "neutral"}>{connected ? "串流中" : "串流中斷，重連中"}</Badge>
        {runtime?.cost_usd != null ? <span className="text-xs text-slate-500">${runtime.cost_usd.toFixed(4)}</span> : null}
        <button type="button" className="text-xs text-slate-500 underline decoration-dotted" onClick={() => setContextOpen((open) => !open)} aria-expanded={contextOpen}>
          {context ? `上下文 ${Math.round(context.percent)}% · ${context.currentTokens.toLocaleString()} / ${context.contextWindow.toLocaleString()} · ${contextLabel(context.label)}` : "上下文使用量不可用"}
          {context && context.percent >= 85 ? " · 接近上限" : ""}
        </button>
        <select
          className="rounded border border-slate-300 px-1 py-0.5 text-xs"
          title="模型（即時切換）"
          disabled={closed || startable || actions.configure.isPending}
          value={MODEL_CHOICES.some((c) => c.value === (options.model ?? "")) ? (options.model ?? "") : "__custom"}
          onChange={(e) => {
            if (!e.target.value || e.target.value === "__custom") return;
            const cutoff = events.at(-1)?.seq ?? 0;
            setContextInvalidAfterSeq(cutoff);
            actions.configure.mutate({ model: e.target.value }, { onError: () => setContextInvalidAfterSeq(-1) });
          }}
        >
          {MODEL_CHOICES.map((c) => (
            <option key={c.value} value={c.value} disabled={c.value === ""}>{c.value === "" ? "模型…" : c.label}</option>
          ))}
          {options.model && !MODEL_CHOICES.some((c) => c.value === options.model) ? <option value="__custom">{options.model}</option> : null}
        </select>
        <select
          className="rounded border border-slate-300 px-1 py-0.5 text-xs"
          title="權限模式（即時切換）"
          disabled={closed || startable || actions.configure.isPending}
          value={options.permission_mode ?? "default"}
          onChange={(e) => actions.configure.mutate({ permission_mode: e.target.value })}
        >
          {PERMISSION_MODE_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>{c.label}</option>
          ))}
        </select>
        <div className="ml-auto flex gap-2">
          {startable ? (
            <Button variant="primary" disabled={actions.start.isPending || !runtime?.runner_connected} onClick={() => actions.start.mutate()}>
              啟動 session
            </Button>
          ) : null}
          <Button disabled={closed || actions.interrupt.isPending} onClick={() => actions.interrupt.mutate()}>
            中斷
          </Button>
          {onFork && runtime?.sdk_session_id ? (
            <Button
              title="以這個對話為起點分支出新 session"
              onClick={() => onFork({ sessionId, baseVersionId: session.data?.base_version_id ?? null, runnerId: runtime?.runner_id ?? null })}
            >
              分支
            </Button>
          ) : null}
          <Button disabled={closed || actions.diff.isPending} onClick={() => { setDiffOpen(true); actions.diff.mutate(); }}>
            變更
          </Button>
          <Button
            title="commit＋秘密檔守門＋git bundle 拉回平台，走既有晉升鏈"
            disabled={closed || actions.checkpoint.isPending}
            onClick={() => actions.checkpoint.mutateAsync().then((result) => setCheckpointApproval(result.approval))}
          >
            檢查點
          </Button>
          <Button variant="danger" disabled={closed || actions.close.isPending} onClick={() => actions.close.mutate()}>
            關閉
          </Button>
        </div>
      </header>
      {error ? <div className="bg-rose-50 px-4 py-1 text-xs text-rose-800">{error.message}</div> : null}
      {contextOpen ? (
        <section className="border-b border-slate-200 bg-slate-50 px-4 py-3 text-xs" aria-label="上下文詳情">
          {context ? <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1"><dt>來源</dt><dd>{contextLabel(context.label)}</dd><dt>目前 tokens</dt><dd>{context.currentTokens.toLocaleString()}</dd><dt>上下文視窗</dt><dd>{context.contextWindow.toLocaleString()}</dd>{context.categories.map((category) => <div key={category.name} className="contents"><dt>{category.name}</dt><dd>{category.tokens.toLocaleString()}</dd></div>)}</dl> : <p>執行環境尚未提供可信的目前 token 數與上下文視窗上限。</p>}
        </section>
      ) : null}
      {checkpointApproval ? (
        <div className="border-b border-slate-200 bg-amber-50/50 px-4 py-2">
          <ApprovalCard
            approval={checkpointApproval}
            onDecided={(result) => {
              setCheckpointApproval(null);
              const taskId = result.engineering_task_id;
              if (typeof taskId === "string" && taskId) setPromoteTaskId(taskId);
              void bridgeTasks.refetch();
            }}
          />
        </div>
      ) : null}
      {activePromoteTaskId ? (
        <div className="border-b border-slate-200 px-4 py-2">
          <PromotePanel
            taskId={activePromoteTaskId}
            project={session.data?.project_id}
            onPromoted={() => {
              setPromoteTaskId(null);
              void bridgeTasks.refetch();
            }}
          />
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1">
        <div className="flex min-h-0 flex-1 flex-col">
          <Transcript items={items} deciding={actions.decide.isPending} onDecide={(requestId, decision, allowPattern) => actions.decide.mutate({ requestId, decision, allowPattern })} onAnalyzeRun={analyzeRun} onContinue={continueFromAnalysis} sending={actions.send.isPending || sessionUnavailable || closed || startable} />
          <div className="relative border-t border-slate-200 bg-white p-3">
            <div className="mb-2 flex items-center gap-2 text-xs" role="status" aria-live="polite">
              <Badge tone={sessionUnavailable || failedSend ? "bad" : permissionRequired ? "warn" : streaming || actions.send.isPending ? "info" : "neutral"}>{failedSend ? "傳送失敗" : interactionState}</Badge>
              {failedSend ? <Button onClick={() => sendPayload(failedSend)} disabled={actions.send.isPending}>重試</Button> : null}
            </div>
            {slashMatches.length > 0 ? (
              <div className="absolute bottom-full left-3 z-10 mb-1 w-80 rounded-md border border-slate-200 bg-white shadow-lg" data-testid="slash-menu">
                {slashMatches.map((name) => (
                  <button
                    key={name}
                    type="button"
                    className="block w-full px-3 py-1 text-left font-mono text-xs hover:bg-slate-100"
                    onClick={() => setDraft(`/${name} `)}
                  >
                    /{name}
                  </button>
                ))}
              </div>
            ) : null}
            {mentionMatches.length > 0 ? (
              <div className="absolute bottom-full left-3 z-10 mb-1 max-h-64 w-96 overflow-y-auto rounded-md border border-slate-200 bg-white shadow-lg" data-testid="mention-menu">
                {mentionMatches.map((name) => (
                  <button key={name} type="button" className="block w-full px-3 py-1 text-left font-mono text-xs hover:bg-slate-100"
                    onClick={() => setDraft(draft.replace(/@([A-Za-z0-9_./-]*)$/, `@${name} `))}>
                    {name}
                  </button>
                ))}
              </div>
            ) : null}
            {pending.length > 0 ? (
              <div className="mb-1 flex flex-wrap gap-2">
                {pending.map((item, index) => (
                  <span key={index} className="inline-flex items-center gap-1 rounded bg-slate-100 px-2 py-0.5 text-xs">
                    🖼 {item.media_type}（{Math.round(item.bytes / 1024)} KB）
                    <button type="button" className="text-slate-500" onClick={() => setPending(pending.filter((_, i) => i !== index))}>×</button>
                  </span>
                ))}
              </div>
            ) : null}
            <textarea
              className="h-20 w-full resize-none rounded-md border border-slate-300 p-2 text-sm"
              placeholder={closed ? "session 已關閉" : startable ? "先啟動 session" : "告訴 agent 要做什麼…（Enter 送出，Shift+Enter 換行）"}
              value={draft}
              disabled={closed || startable || sessionUnavailable || actions.send.isPending}
              onChange={(event) => {
                setDraft(event.target.value);
                if (event.target.value.includes("@") && fileList === null && !actions.files.isPending) {
                  actions.files.mutateAsync().then((result) => setFileList(result.files ?? [])).catch(() => setFileList([]));
                }
              }}
              onPaste={(event) => {
                const images = Array.from(event.clipboardData?.files ?? []).filter((file) => file.type.startsWith("image/"));
                if (images.length) {
                  event.preventDefault();
                  addImages(images);
                }
              }}
              onDrop={(event) => {
                if (event.dataTransfer?.files?.length) {
                  event.preventDefault();
                  addImages(event.dataTransfer.files);
                }
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  submit();
                }
              }}
            />
            <div className="mt-1 flex items-center justify-between text-xs text-slate-500">
              <span>可貼上／拖入圖片（≤4 張、各 ≤3MB）、用 @ 引用工作區檔案、/ 呼叫 skills 與指令。Bash 除驗證指令外每條都會請你允許。</span>
              <Button variant="primary" disabled={!sendable} onClick={submit}>
                送出
              </Button>
            </div>
          </div>
        </div>
        {diffOpen ? (
          <aside className="flex w-[28rem] min-h-0 flex-col border-l border-slate-200 bg-white">
            <div className="flex items-center justify-between border-b border-slate-200 px-3 py-2 text-sm font-semibold">
              <span>變更</span>
              <button type="button" className="text-xs text-slate-500" onClick={() => setDiffOpen(false)}>
                關閉
              </button>
            </div>
            <pre className="min-h-0 flex-1 overflow-auto p-3 text-xs">
              {actions.diff.isPending ? "取得中…" : actions.diff.data?.unreachable ? "runner 未連線" : actions.diff.data?.patch || "（沒有變更）"}
            </pre>
          </aside>
        ) : null}
      </div>
    </div>
  );
}
