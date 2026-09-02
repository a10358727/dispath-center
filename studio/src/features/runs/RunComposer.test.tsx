import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunComposer } from "./RunComposer";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const workspace = {
  project: { id: "p1", name: "demo" },
  environment: { id: "env-1", name: "default", revision_id: "rev-1", revision: 1 },
  run_template: { id: "rp-1", name: "train", revision: 1, spec_digest: "a".repeat(64), parameters: [{ name: "lr", type: "number", required: true }, { name: "epochs", type: "integer", required: true }] },
  defaults: { revision_id: "def-1" },
  run_creation_options: {
    project_version_candidates: [{ id: "v1", state: "promoted", created_at: "2026-09-01" }],
    ssh_target_candidates: [
      { id: "rev-a", server_name: "server-a", ready: true },
      { id: "rev-b", server_name: "server-b", ready: true },
    ],
  },
};

function stub(calls: { url: string; body?: Record<string, unknown> }[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url: `${init?.method ?? "GET"} ${url}`, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/workspace")) return jsonResponse(200, workspace);
      if (url.includes("/servers")) return jsonResponse(200, []);
      if (url.includes("/server-configs")) return jsonResponse(200, []);
      if (url.endsWith("/experiment-previews")) return jsonResponse(200, { run_count: 2, plan_digests: ["d1", "d2"], members: [{ target_server: "server-a", parameter_values: { lr: "0.1" }, plan_digest: "d1" }, { target_server: "server-b", parameter_values: { lr: "0.01" }, plan_digest: "d2" }] });
      if (url.endsWith("/experiment-requests")) return jsonResponse(202, { approval_id: 21 });
      if (url.endsWith("/run-previews")) return jsonResponse(200, { plan_digest: "p".repeat(64) });
      if (url.endsWith("/run-requests")) return jsonResponse(202, { approval_id: 22 });
      if (url.endsWith("/api/v2/approvals/21")) return jsonResponse(200, { id: 21, kind: "experiment_create_v2", title: "建立實驗", summary: "demo · 2 個 run", status: "pending", created_at: "2026-09-02T00:00:00Z", payload: {} });
      if (url.endsWith("/api/v2/approvals/22")) return jsonResponse(200, { id: 22, kind: "execution_plan_v2", title: "執行一個 Run", summary: "demo · server-a", status: "pending", created_at: "2026-09-02T00:00:00Z", payload: {} });
      if (url.endsWith("/api/v2/dispatch-requests")) return jsonResponse(200, { approval: { id: 30, kind: "enqueue", status: "approved", created_at: "2026-09-02T00:00:00Z" }, auto_approved: true, job: { id: 77 } });
      return jsonResponse(404, { detail: "not found" });
    }),
  );
}

function renderComposer() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RunComposer projectId="p1" projectName="demo" onCreated={() => undefined} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("RunComposer (整頓 U6)", () => {
  it("expands a list value into an experiment (preview → card)", async () => {
    const calls: { url: string; body?: Record<string, unknown> }[] = [];
    stub(calls);
    renderComposer();
    const lr = await screen.findByPlaceholderText("例：0.1 或 0.1, 0.01");
    fireEvent.change(lr, { target: { value: "0.1, 0.01" } });
    expect(screen.getByText("展開 2 個 run")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /server-a/ }));
    fireEvent.click(screen.getByRole("button", { name: /server-b/ }));
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));
    expect(await screen.findByText("預覽：2 個 run")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "建立核准卡" }));
    expect(await screen.findByText("建立實驗")).toBeInTheDocument();
    const request = calls.find((call) => call.url.endsWith("/experiment-requests"));
    expect(request?.body).toMatchObject({ project_version_id: "v1", template_selection: { kind: "project_defaults", project_defaults_revision_id: "def-1" }, expected_plan_digests: ["d1", "d2"] });
    expect((request?.body as { matrix: { axes: unknown[] } }).matrix.axes).toEqual([{ name: "lr", values: ["0.1", "0.01"] }]);
  });

  it("sends single values as one run pinned to one target", async () => {
    const calls: { url: string; body?: Record<string, unknown> }[] = [];
    stub(calls);
    renderComposer();
    const lr = await screen.findByPlaceholderText("例：0.1 或 0.1, 0.01");
    fireEvent.change(lr, { target: { value: "0.1" } });
    fireEvent.click(screen.getByRole("button", { name: /server-a/ }));
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));
    expect(await screen.findByText("預覽通過，可以建立核准卡。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "建立核准卡" }));
    expect(await screen.findByText("執行一個 Run")).toBeInTheDocument();
    const request = calls.find((call) => call.url.endsWith("/run-requests"));
    expect(request?.body).toMatchObject({ target_selection: { kind: "server_config_revision", server_config_revision_id: "rev-a" }, parameter_overrides: { lr: "0.1" }, expected_plan_digest: "p".repeat(64) });
    expect(screen.queryByText(/digest/)).not.toBeInTheDocument();
  });

  it("runs a quick command through the web enqueue path", async () => {
    const calls: { url: string; body?: Record<string, unknown> }[] = [];
    stub(calls);
    renderComposer();
    await screen.findByText("模板 Run／實驗");
    fireEvent.click(screen.getByRole("button", { name: "快速指令" }));
    fireEvent.change(screen.getByPlaceholderText("python train.py --lr 0.1"), { target: { value: "nvidia-smi" } });
    fireEvent.click(screen.getByRole("button", { name: "立即執行" }));
    expect(await screen.findByText("已排入任務 #77")).toBeInTheDocument();
    const request = calls.find((call) => call.url.endsWith("/api/v2/dispatch-requests"));
    expect(request?.body).toMatchObject({ command: "nvidia-smi", project: "demo", source: "web", type: "adhoc", pin_server: "server-a" });
  });
});
