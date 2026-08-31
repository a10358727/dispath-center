import { useMemo, useState } from "react";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useSession, useSessionActions } from "@/api/hooks";
import { Transcript } from "./Transcript";
import { buildTranscript } from "./transcript";
import { useSessionStream } from "./useSessionStream";
import { MODEL_CHOICES, PERMISSION_MODE_CHOICES } from "./SessionOptionsFields";

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
  const runtime = session.data?.runtime;
  const options = runtime?.options ?? {};
  const state = runtime?.task_state ?? null;
  const context = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i -= 1) {
      if (events[i].kind === "context") return events[i].payload.usage as Record<string, unknown> | undefined;
    }
    return undefined;
  }, [events]);
  const contextTokens = (() => {
    if (!context) return null;
    const total = typeof context.total_tokens === "number" ? context.total_tokens : null;
    if (total != null) return total;
    const categories = Array.isArray(context.categories) ? (context.categories as Record<string, unknown>[]) : [];
    return categories.reduce((sum, c) => sum + (typeof c.tokens === "number" ? c.tokens : 0), 0);
  })();
  const contextWindow = context && typeof context.context_window === "number" ? context.context_window : null;
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
  const sendable = !closed && !startable && (draft.trim().length > 0 || pending.length > 0) && !actions.send.isPending;
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

  const submit = () => {
    const text = draft.trim();
    if (text === "/diff") {
      setDraft("");
      setDiffOpen(true);
      actions.diff.mutate();
      return;
    }
    if ((!text && pending.length === 0) || !sendable) return;
    setDraft("");
    const attachments = pending.map((item) => ({ type: "image" as const, media_type: item.media_type, data_base64: item.data_base64 }));
    setPending([]);
    actions.send.mutate({ text: text || "（附圖）", attachments: attachments.length ? attachments : undefined });
  };
  const error = [actions.start, actions.send, actions.interrupt, actions.close, actions.decide, actions.diff, actions.configure].map((m) => m.error).find(Boolean) as Error | undefined;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-white px-4 py-2 text-sm">
        <span className="font-semibold">{session.data?.workspace_branch ?? sessionId.slice(0, 8)}</span>
        <Badge tone={stateTone(closed ? "canceled" : state)}>{closed ? "closed" : (state ?? "not started")}</Badge>
        <Badge tone={runtime?.runner_connected ? "ok" : "neutral"}>{runtime?.runner_connected ? "runner 已連線" : "runner 離線"}</Badge>
        <Badge tone={connected ? "ok" : "neutral"}>{connected ? "串流中" : "串流中斷，重連中"}</Badge>
        {runtime?.cost_usd != null ? <span className="text-xs text-slate-500">${runtime.cost_usd.toFixed(4)}</span> : null}
        {contextTokens != null ? (
          <span className="text-xs text-slate-500" title="上一回合後的 context 使用量">
            context {Math.round(contextTokens / 1000)}k{contextWindow ? ` / ${Math.round(contextWindow / 1000)}k` : ""}
          </span>
        ) : null}
        <select
          className="rounded border border-slate-300 px-1 py-0.5 text-xs"
          title="模型（即時切換）"
          disabled={closed || startable || actions.configure.isPending}
          value={MODEL_CHOICES.some((c) => c.value === (options.model ?? "")) ? (options.model ?? "") : "__custom"}
          onChange={(e) => e.target.value && e.target.value !== "__custom" && actions.configure.mutate({ model: e.target.value })}
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
            Changes
          </Button>
          <Button variant="danger" disabled={closed || actions.close.isPending} onClick={() => actions.close.mutate()}>
            關閉
          </Button>
        </div>
      </header>
      {error ? <div className="bg-rose-50 px-4 py-1 text-xs text-rose-800">{error.message}</div> : null}
      <div className="flex min-h-0 flex-1">
        <div className="flex min-h-0 flex-1 flex-col">
          <Transcript items={items} deciding={actions.decide.isPending} onDecide={(requestId, decision, allowPattern) => actions.decide.mutate({ requestId, decision, allowPattern })} />
          <div className="relative border-t border-slate-200 bg-white p-3">
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
              disabled={closed || startable}
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
                if (event.key === "Enter" && !event.shiftKey) {
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
              <span>Changes</span>
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
