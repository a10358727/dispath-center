import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SetupPanel } from "./SetupPanel";
import { describeReason } from "./readiness";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const PROJECT_ID = "10000000-0000-0000-0000-000000000001";

function stubApi(overrides: { workspace?: unknown; workspaceStatus?: number; versions?: unknown[]; environments?: unknown[] }, calls: string[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.includes("/api/v2/projects-matrix")) return jsonResponse(200, { projects: [{ id: PROJECT_ID, name: "demo", instances: { "server-a": { path: "/srv/demo", git_commit: "abcdef1234", dirty: false } } }] });
      if (url.endsWith("/versions")) return jsonResponse(200, overrides.versions ?? []);
      if (url.endsWith("/workspace")) return jsonResponse(overrides.workspaceStatus ?? 200, overrides.workspace ?? {});
      if (url.endsWith("/environments")) return jsonResponse(200, { items: overrides.environments ?? [] });
      if (url.includes("/server-configs")) return jsonResponse(200, [{ name: "server-a", tags: ["gpu"] }]);
      if (url.endsWith("/hub-sync")) return jsonResponse(200, { synced: true });
      if (url.endsWith("/environment-change-requests")) return jsonResponse(202, { approval_id: 9, replayed: false, status: "pending" });
      if (url.endsWith("/api/v2/approvals/9")) return jsonResponse(200, { id: 9, kind: "environment_change_v2", title: "環境設定變更", summary: "create · default", status: "pending", created_at: "2026-09-02T00:00:00Z", payload: {} });
      return jsonResponse(404, { detail: "not found" });
    }),
  );
}

function renderPanel() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SetupPanel project="demo" />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("SetupPanel (整頓 U4a)", () => {
  it("offers version registration and the environment form when nothing is set up", async () => {
    const calls: string[] = [];
    stubApi({ workspace: { project: { id: PROJECT_ID, name: "demo" }, environment: null, run_template: null, defaults: null, run_creation_options: { ssh_target_candidates: [] } } }, calls);
    renderPanel();
    expect(await screen.findByText("還沒有任何版本。從一台已有專案副本的機器登記目前的 commit（直接執行、留稽核）：")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "登記目前版本" }));
    await screen.findByText("1. 登記版本");
    expect(calls).toContain("POST /api/v2/legacy-projects/demo/hub-sync");

    fireEvent.change(screen.getByPlaceholderText("pip install -r requirements.txt"), { target: { value: "pip install -e ." } });
    fireEvent.click(screen.getByRole("button", { name: "建立環境（一張核准卡）" }));
    expect(await screen.findByText("環境設定變更")).toBeInTheDocument();
    expect(calls).toContain(`POST /api/v2/projects/${PROJECT_ID}/environment-change-requests`);
  });

  it("shows the environment as done with readiness reasons in Chinese", async () => {
    stubApi({
      versions: [{ id: "v1", project_id: PROJECT_ID, git_commit: "abcdef1234567890", created_at: "2026-09-01", promotion_state: "promoted" }],
      workspace: {
        project: { id: PROJECT_ID, name: "demo" },
        environment: { id: "env-1", name: "default", revision: 1, required_server_tags: ["gpu"] },
        run_template: null,
        defaults: null,
        run_creation_options: { ssh_target_candidates: [{ server_name: "server-a", ready: false, readiness_reasons: ["no_matching_promoted_version"] }] },
      },
      environments: [{ environment_id: "env-1", name: "default", status: "approved", readiness: { state: "not_ready", reasons: ["host_observation_stale"] } }],
    });
    renderPanel();
    expect(await screen.findByText(/default · 第 1 版/)).toBeInTheDocument();
    // Labels sit next to their hint inside the same list item; match loosely and wait for the environments query.
    expect(await screen.findByText(new RegExp(describeReason("host_observation_stale").label))).toBeInTheDocument();
    expect(await screen.findByText(new RegExp(describeReason("no_matching_promoted_version").label))).toBeInTheDocument();
    expect(screen.getByText(/已登記 1 個版本，其中 1 個已晉升/)).toBeInTheDocument();
    expect(screen.queryByText("host_observation_stale")).not.toBeInTheDocument();
  });

  it("compiles the command into a template card, then offers typed defaults (U4b)", async () => {
    const calls: string[] = [];
    const bodies: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push(`${init?.method ?? "GET"} ${url}`);
        if (init?.body) bodies.push(JSON.parse(String(init.body)));
        if (url.includes("/api/v2/projects-matrix")) return jsonResponse(200, { projects: [{ id: PROJECT_ID, name: "demo", instances: {} }] });
        if (url.endsWith("/versions")) return jsonResponse(200, []);
        if (url.endsWith("/workspace"))
          return jsonResponse(200, {
            project: { id: PROJECT_ID, name: "demo" },
            environment: { id: "env-1", name: "default", revision_id: "rev-1", revision: 1, required_server_tags: ["gpu"] },
            run_template: null,
            defaults: null,
            run_creation_options: { ssh_target_candidates: [] },
          });
        if (url.endsWith("/environments")) return jsonResponse(200, { items: [] });
        if (url.includes("/server-configs")) return jsonResponse(200, []);
        if (url.endsWith("/run-template-change-requests")) return jsonResponse(202, { approval_id: 11, replayed: false, status: "pending" });
        if (url.endsWith("/api/v2/approvals/11")) return jsonResponse(200, { id: 11, kind: "run_template_change_v2", title: "執行模板變更", summary: "create · train", status: "pending", created_at: "2026-09-02T00:00:00Z", payload: {} });
        return jsonResponse(404, { detail: "not found" });
      }),
    );
    renderPanel();
    const command = await screen.findByPlaceholderText("python train.py --lr {lr:number} --epochs {epochs:integer}");
    fireEvent.change(command, { target: { value: "python train.py --lr={lr}" } });
    expect(await screen.findByText(/參數必須是獨立的引數/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "建立模板（一張核准卡）" })).toBeDisabled();
    fireEvent.change(command, { target: { value: "python train.py --lr {lr:number} --epochs {epochs:integer}" } });
    expect(await screen.findByText(/lr（number）、epochs（integer）/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "建立模板（一張核准卡）" }));
    expect(await screen.findByText("執行模板變更")).toBeInTheDocument();
    const body = bodies.find((item) => item.operation === "create" && "template" in item) as { template: { argv_template: unknown[]; parameter_schema: unknown[]; resource_requirements: { required_tags: string[]; exclusive_worker: boolean } }; expected_environment_head_revision_id: string };
    expect(body.expected_environment_head_revision_id).toBe("rev-1");
    expect(body.template.argv_template[0]).toEqual({ kind: "literal", value: "python" });
    expect(body.template.parameter_schema).toHaveLength(2);
    expect(body.template.resource_requirements.required_tags).toEqual(["gpu"]);
    expect(body.template.resource_requirements.exclusive_worker).toBe(true);
    expect(calls).toContain(`POST /api/v2/projects/${PROJECT_ID}/run-template-change-requests`);
  });

  it("explains when the workspace route is gated off", async () => {
    stubApi({ workspaceStatus: 404, workspace: { error: { code: "not_found", message: "Resource not found" } } });
    renderPanel();
    expect(await screen.findByText(/執行設定功能未啟用/)).toBeInTheDocument();
  });
});
