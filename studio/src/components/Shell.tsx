import { NavLink, Outlet } from "react-router-dom";
import { useApprovals, useMe } from "@/api/hooks";
import { cn } from "@/lib";

const NAV = [
  { to: "/projects", label: "專案" },
  { to: "/runs", label: "實驗與 Run" },
  { to: "/servers", label: "伺服器與硬體" },
  { to: "/datasets", label: "資料集" },
  { to: "/approvals", label: "核准匣" },
  { to: "/events", label: "稽核事件" },
  { to: "/settings", label: "設定" },
];

export function Shell() {
  const me = useMe();
  const pending = useApprovals("pending");
  const count = pending.data?.length ?? 0;
  return (
    <div className="flex h-screen">
      <nav className="flex w-44 flex-col border-r border-slate-200 bg-white">
        <div className="px-4 py-3 text-sm font-bold tracking-wide">Dispatch Studio</div>
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => cn("mx-2 flex items-center justify-between rounded-md px-3 py-2 text-sm hover:bg-slate-100", isActive && "bg-slate-200 font-medium")}
          >
            <span>{item.label}</span>
            {item.to === "/approvals" && count > 0 ? <span className="rounded-full bg-amber-500 px-1.5 text-xs text-white">{count}</span> : null}
          </NavLink>
        ))}
        <div className="mt-auto truncate px-4 py-3 text-xs text-slate-500" title={me.data?.actor?.id ?? ""}>
          {me.data?.actor?.display_name ?? me.data?.actor?.id ?? ""}
        </div>
      </nav>
      <div className="min-h-0 min-w-0 flex-1">
        <Outlet />
      </div>
    </div>
  );
}
