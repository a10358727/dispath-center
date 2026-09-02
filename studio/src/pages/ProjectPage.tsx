import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { promotableCheckpointTasks, useCheckpointTasks, useProjectSessions } from "@/api/hooks";
import type { SessionSummary } from "@/api/types";
import { OpenSessionDialog } from "@/features/session/OpenSessionDialog";
import { PromotePanel } from "@/features/session/PromotePanel";
import { SetupPanel } from "@/features/project/SetupPanel";
import { SessionView } from "@/features/session/SessionView";
import { cn, formatTime } from "@/lib";

function SessionRow({ session, active }: { session: SessionSummary; active: boolean }) {
  return (
    <Link
      to={`/projects/${encodeURIComponent(session.project_id)}/sessions/${session.id}`}
      className={cn("block rounded-md px-2 py-1.5 text-sm hover:bg-slate-100", active && "bg-slate-200")}
    >
      <div className="flex items-center gap-2">
        <span className="truncate font-medium">{session.workspace_branch ?? session.id.slice(0, 8)}</span>
        <Badge tone={stateTone(session.status === "active" ? "working" : "canceled")}>{session.status}</Badge>
      </div>
      <div className="text-xs text-slate-500">
        {session.provider_id} · {formatTime(session.last_used_at ?? session.created_at)}
      </div>
    </Link>
  );
}

export function ProjectPage() {
  const { name = "", sessionId } = useParams();
  const navigate = useNavigate();
  const sessions = useProjectSessions(name);
  const [opening, setOpening] = useState(false);
  const [setup, setSetup] = useState(false);
  const [fork, setFork] = useState<{ sessionId: string; baseVersionId: string | null; runnerId: string | null } | null>(null);
  const list = [sessions.data?.current, ...(sessions.data?.recent ?? [])].filter((s): s is SessionSummary => Boolean(s));
  const unique = list.filter((session, index) => list.findIndex((other) => other.id === session.id) === index);
  //: 整頓 U3: approved checkpoints that no promotion consumed yet — shown on
  //: the project landing so the step is never lost after navigating away.
  const bridgeTasks = useCheckpointTasks(name);
  const unpromoted = promotableCheckpointTasks(bridgeTasks.data);

  return (
    <div className="flex h-full min-h-0">
      <aside className="flex w-64 flex-col border-r border-slate-200 bg-white">
        <div className="flex items-center justify-between px-3 py-2">
          <Link to="/projects" className="text-xs text-slate-500 hover:underline">
            ← 專案
          </Link>
          <span className="truncate font-semibold">{name}</span>
        </div>
        <div className="space-y-1 px-3 pb-2">
          <Button variant="primary" className="w-full" onClick={() => { setSetup(false); setOpening(true); }}>
            ＋ 新 session
          </Button>
          <Button className="w-full" onClick={() => { setOpening(false); setFork(null); setSetup(true); navigate(`/projects/${encodeURIComponent(name)}`); }}>
            執行設定
          </Button>
        </div>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-2">
          {sessions.isLoading ? <div className="px-2 text-xs text-slate-400">載入中…</div> : null}
          {unique.map((session) => (
            <SessionRow key={session.id} session={session} active={session.id === sessionId} />
          ))}
          {!sessions.isLoading && unique.length === 0 ? <div className="px-2 text-xs text-slate-400">還沒有 session</div> : null}
        </div>
      </aside>
      <main className="min-h-0 flex-1">
        {opening || fork ? (
          <div className="p-4">
            <OpenSessionDialog
              project={name}
              fork={fork ?? undefined}
              onCancel={() => {
                setOpening(false);
                setFork(null);
              }}
              onOpened={() => {
                setOpening(false);
                setFork(null);
                void sessions.refetch().then((result) => {
                  const current = result.data?.current;
                  if (current) navigate(`/projects/${encodeURIComponent(name)}/sessions/${current.id}`);
                });
              }}
            />
          </div>
        ) : sessionId ? (
          <SessionView key={sessionId} sessionId={sessionId} onFork={(info) => setFork(info)} />
        ) : setup ? (
          <div className="h-full overflow-y-auto">
            <SetupPanel project={name} />
          </div>
        ) : (
          <div className="flex h-full flex-col">
            {unpromoted.length > 0 ? (
              <div className="space-y-2 border-b border-slate-200 p-4">
                <div className="text-sm font-medium text-slate-700">有 {unpromoted.length} 個存檔尚未晉升</div>
                {unpromoted.map((task) => (
                  <PromotePanel key={task.id} taskId={task.id} project={name} onPromoted={() => void bridgeTasks.refetch()} />
                ))}
              </div>
            ) : null}
            <div className="flex flex-1 items-center justify-center text-sm text-slate-400">選一個 session，或開一個新的。</div>
          </div>
        )}
      </main>
    </div>
  );
}
