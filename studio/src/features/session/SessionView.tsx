import { useMemo, useState } from "react";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useSession, useSessionActions } from "@/api/hooks";
import { Transcript } from "./Transcript";
import { buildTranscript } from "./transcript";
import { useSessionStream } from "./useSessionStream";

export function SessionView({ sessionId }: { sessionId: string }) {
  const session = useSession(sessionId);
  const actions = useSessionActions(sessionId);
  const { events, connected } = useSessionStream(sessionId);
  const items = useMemo(() => buildTranscript(events), [events]);
  const [draft, setDraft] = useState("");
  const [diffOpen, setDiffOpen] = useState(false);
  const runtime = session.data?.runtime;
  const state = runtime?.task_state ?? null;
  const closed = session.data?.status === "closed";
  const startable = !closed && (state == null || state === "unknown" || state === "failed");
  const sendable = !closed && !startable && draft.trim().length > 0 && !actions.send.isPending;

  const submit = () => {
    const text = draft.trim();
    if (!text || !sendable) return;
    setDraft("");
    actions.send.mutate(text);
  };
  const error = [actions.start, actions.send, actions.interrupt, actions.close, actions.decide, actions.diff].map((m) => m.error).find(Boolean) as Error | undefined;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-white px-4 py-2 text-sm">
        <span className="font-semibold">{session.data?.workspace_branch ?? sessionId.slice(0, 8)}</span>
        <Badge tone={stateTone(closed ? "canceled" : state)}>{closed ? "closed" : (state ?? "not started")}</Badge>
        <Badge tone={runtime?.runner_connected ? "ok" : "neutral"}>{runtime?.runner_connected ? "runner 已連線" : "runner 離線"}</Badge>
        <Badge tone={connected ? "ok" : "neutral"}>{connected ? "串流中" : "串流中斷，重連中"}</Badge>
        {runtime?.cost_usd != null ? <span className="text-xs text-slate-500">${runtime.cost_usd.toFixed(4)}</span> : null}
        <div className="ml-auto flex gap-2">
          {startable ? (
            <Button variant="primary" disabled={actions.start.isPending || !runtime?.runner_connected} onClick={() => actions.start.mutate()}>
              啟動 session
            </Button>
          ) : null}
          <Button disabled={closed || actions.interrupt.isPending} onClick={() => actions.interrupt.mutate()}>
            中斷
          </Button>
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
          <div className="border-t border-slate-200 bg-white p-3">
            <textarea
              className="h-20 w-full resize-none rounded-md border border-slate-300 p-2 text-sm"
              placeholder={closed ? "session 已關閉" : startable ? "先啟動 session" : "告訴 agent 要做什麼…（Enter 送出，Shift+Enter 換行）"}
              value={draft}
              disabled={closed || startable}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  submit();
                }
              }}
            />
            <div className="mt-1 flex items-center justify-between text-xs text-slate-500">
              <span>工作區內 Bash 除驗證指令外，每一條都會在這裡請你允許。平台動作（run／實驗／晉升）只會建核准卡。</span>
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
