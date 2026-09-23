import { describe, expect, it } from "vitest";
import { projectCompute } from "./ServersPage";

describe("projectCompute", () => {
  it("keeps configured but unobserved Compute unknown and preserves custom ports", () => {
    const [row] = projectCompute([{ name: "rental-1", host: "host", port: 31827, enabled: true }], [], new Map());
    expect(row).toMatchObject({ name: "rental-1", status: "狀態未知", config: { port: 31827 } });
  });

  it("distinguishes disabled, stale, disconnected, partial probes, and connected observations", () => {
    const configs = ["disabled", "blocked", "stale", "offline", "partial", "healthy"].map((name) => ({ name, enabled: name !== "disabled", ...(name === "blocked" ? { attempt_backend_preflight: "ineligible_non_local_fs" } : {}) }));
    const live = [
      { name: "disabled", online: true },
      { name: "blocked", online: true },
      { name: "stale", online: true },
      { name: "offline", online: false },
      { name: "partial", online: true, error: "probe failed" },
      { name: "healthy", online: true },
    ];
    const rows = projectCompute(configs, live, new Map([["disabled", 1], ["blocked", 1], ["stale", 121], ["offline", 1], ["partial", 1], ["healthy", 1]]));
    expect(Object.fromEntries(rows.map((row) => [row.name, row.status]))).toEqual({ blocked: "已封鎖", disabled: "已停用", healthy: "已連線", offline: "已斷線", partial: "需要留意", stale: "狀態未知" });
    expect(rows.find((row) => row.name === "stale")?.stale).toBe(true);
  });
});
