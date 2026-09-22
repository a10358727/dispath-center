import { NavLink, Outlet } from "react-router-dom";
import { useApprovals, useMe } from "@/api/hooks";
import { cn } from "@/lib";

const PRIMARY_NAV = [
  { to: "/overview", label: "Overview" },
  { to: "/projects", label: "Projects" },
  { to: "/compute", label: "Compute" },
  { to: "/activity", label: "Activity" },
  { to: "/settings", label: "Settings" },
];

const ADVANCED_NAV = [
  { to: "/runs", label: "Runs" },
  { to: "/datasets", label: "Datasets" },
  { to: "/approvals", label: "Approvals" },
];

export function Shell() {
  const me = useMe();
  const pending = useApprovals("pending");
  const count = pending.data?.length ?? 0;
  return (
    <div className="flex h-screen">
      <nav className="flex w-44 flex-col border-r border-slate-200 bg-white">
        <div className="px-4 py-3 text-sm font-bold tracking-wide">Dispatch Studio</div>
        {PRIMARY_NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => cn("mx-2 flex items-center justify-between rounded-md px-3 py-2 text-sm hover:bg-slate-100", isActive && "bg-slate-200 font-medium")}
          >
            <span>{item.label}</span>
            {item.to === "/approvals" && count > 0 ? <span className="rounded-full bg-amber-500 px-1.5 text-xs text-white">{count}</span> : null}
          </NavLink>
        ))}
        <div className="mx-4 mt-4 border-t border-slate-200 pt-3 text-[11px] font-semibold uppercase tracking-wide text-slate-400">Advanced</div>
        {ADVANCED_NAV.map((item) => (
          <NavLink key={item.to} to={item.to} className={({ isActive }) => cn("mx-2 flex items-center justify-between rounded-md px-3 py-2 text-sm hover:bg-slate-100", isActive && "bg-slate-200 font-medium")}>
            <span>{item.label}</span>{item.to === "/approvals" && count > 0 ? <span className="rounded-full bg-amber-500 px-1.5 text-xs text-white" aria-label={`${count} 筆待核准`}>{count}</span> : null}
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
