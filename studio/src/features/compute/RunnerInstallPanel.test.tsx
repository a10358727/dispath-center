import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunnerClaudeTokenDialog, RunnerInstallPanel } from "./RunnerInstallPanel";

const response = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function wrap(node: React.ReactElement) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>);
}

afterEach(() => vi.unstubAllGlobals());

describe("RunnerInstallPanel", () => {
  it("creates an enrol card with the install spec and shows the card", async () => {
    const bodies: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/agent-runners/enroll-requests")) {
        bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return response(202, { approval: { id: 7, kind: "agent_runner_enroll", title: "登錄 runner agent", status: "pending", payload: { server: "5090", install: { workspace_root: "~/ws", launch_mode: "platform_tmux" } } } });
      }
      return response(200, {});
    }));
    wrap(<RunnerInstallPanel server="5090" onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "安裝 runner agent" }));
    fireEvent.change(screen.getByLabelText("工作區根目錄"), { target: { value: "~/ws" } });
    fireEvent.click(screen.getByRole("button", { name: "建立安裝核准卡" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ server: "5090", install: { workspace_root: "~/ws" } });
    expect(await screen.findByTestId("approval-7")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /核准/ })).toBeInTheDocument();
  });

  it("shows the server's refusal when the host identity is not trusted", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(400, { error: { code: "invalid_agent_runner_request", message: "5090 的主機身分尚未信任，無法由平台安裝 runner" } })));
    wrap(<RunnerInstallPanel server="5090" onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "安裝 runner agent" }));
    fireEvent.click(screen.getByRole("button", { name: "建立安裝核准卡" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("主機身分尚未信任");
  });
});

describe("RunnerClaudeTokenDialog", () => {
  const runner = { id: "r-1", server: "5090", status: "enrolled", active: true, connected: false, managed: true };

  it("posts the token once, never renders it, and reports the outcome", async () => {
    const bodies: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/v2/agent-runners/r-1/claude-token");
      bodies.push(String(init?.body));
      return response(200, { ok: true, step: "launch", detail: "Claude token 已寫入，runner 已重新啟動" });
    }));
    const onDone = vi.fn();
    wrap(<RunnerClaudeTokenDialog runner={runner} onDone={onDone} />);
    fireEvent.click(screen.getByRole("button", { name: "設定 Claude token" }));
    const field = screen.getByLabelText("Claude token") as HTMLInputElement;
    expect(field.type).toBe("password");
    fireEvent.change(field, { target: { value: "sk-ant-oat01-SECRET" } });
    fireEvent.click(screen.getByRole("button", { name: "寫入並重啟 runner" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    expect(bodies).toEqual([JSON.stringify({ token: "sk-ant-oat01-SECRET" })]);
    expect(field.value).toBe("");
    expect(screen.getByRole("status")).toHaveTextContent("已設定");
    expect(document.body.textContent).not.toContain("sk-ant-oat01-SECRET");
  });

  it("surfaces a refused request as an alert", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(409, { error: { code: "runner_not_managed", message: "此 runner 不是由平台安裝" } })));
    wrap(<RunnerClaudeTokenDialog runner={runner} onDone={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "設定 Claude token" }));
    fireEvent.change(screen.getByLabelText("Claude token"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "寫入並重啟 runner" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("不是由平台安裝");
  });
});
