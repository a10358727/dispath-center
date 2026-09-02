import { useLiveServers, useServerConfigs } from "@/api/hooks";
import { cn } from "@/lib";

/** Selectable execution-target chips with live GPU/online hints. */
export function ServerChips({
  candidates,
  selected,
  onToggle,
  single = false,
}: {
  candidates: { server_name: string; ready?: boolean }[];
  selected: string[];
  onToggle: (name: string) => void;
  /** single: picking a chip replaces the selection instead of toggling. */
  single?: boolean;
}) {
  const live = useLiveServers();
  const configs = useServerConfigs();
  const liveByName = new Map((live.data ?? []).map((server) => [server.name, server]));
  const tagsByName = new Map((configs.data ?? []).map((config) => [config.name, config.tags ?? []]));
  if (candidates.length === 0) return <div className="text-xs text-slate-500">這個專案還沒有可用的執行機器；到專案頁的「執行設定」處理。</div>;
  return (
    <div className="flex flex-wrap gap-2" data-single={single ? "true" : undefined}>
      {candidates.map((candidate) => {
        const name = candidate.server_name;
        const info = liveByName.get(name);
        const picked = selected.includes(name);
        const gpuText = info?.gpu_count ? `GPU×${info.gpu_count}${info.gpu_util_max != null ? ` ${info.gpu_util_max}%` : ""}` : "";
        return (
          <button
            key={name}
            type="button"
            onClick={() => onToggle(name)}
            className={cn(
              "rounded-full border px-3 py-1 text-xs",
              picked ? "border-sky-600 bg-sky-50 text-sky-800" : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50",
            )}
            title={(tagsByName.get(name) ?? []).join(", ")}
          >
            <span className={info?.online ? "text-emerald-500" : "text-slate-300"}>●</span> {name}
            {gpuText ? <span className="ml-1 text-slate-500">{gpuText}</span> : null}
            {candidate.ready === false ? <span className="ml-1 text-amber-600">未就緒</span> : null}
          </button>
        );
      })}
    </div>
  );
}
