import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AddComputeWizard } from "./AddComputeWizard";

function response(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function fillConnection(port = "2222") {
  fireEvent.change(screen.getByLabelText("Unique Compute name"), { target: { value: "rental-a" } });
  fireEvent.change(screen.getByLabelText("Host"), { target: { value: "gpu.example.test" } });
  fireEvent.change(screen.getByLabelText("SSH port"), { target: { value: port } });
  fireEvent.change(screen.getByLabelText("SSH user"), { target: { value: "operator" } });
  fireEvent.change(screen.getByLabelText("Private-key path reference"), { target: { value: "/keys/rental-a" } });
  fireEvent.change(screen.getByLabelText("Additional tags (comma-separated)"), { target: { value: "team-a,gpu" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("AddComputeWizard", () => {
  it("tests the exact draft, adds through ServerConfig, then reports stored preflight", async () => {
    const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
      requests.push({ url: String(input), body });
      if (String(input).endsWith("/test-ssh")) return response(200, { ok: true, results: { hostname: "gpu-a", whoami: "operator", tmux: "tmux 3.4", gpu: "0, Test GPU" }, warnings: [], errors: [] });
      if (String(input).endsWith("/server-configs")) return response(200, { name: "rental-a" });
      if (String(input).endsWith("/rental-a/attempt-preflight")) return response(200, { ok: true, status: "eligible", filesystem_type: "ext4" });
      return response(404, { detail: "not found" });
    }));
    const added = vi.fn();
    render(<AddComputeWizard existingNames={[]} onAdded={added} onCancel={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fillConnection();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(screen.getByRole("button", { name: "Test Connection" }));
    expect(await screen.findByText("Connection succeeded")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByText("operator@gpu.example.test:2222")).toBeInTheDocument();
    expect(screen.getByText("Configured (rental-a)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add Compute" }));

    expect(await screen.findByText("Preflight eligible")).toBeInTheDocument();
    expect(screen.getByText(/Local attempt filesystem detected \(ext4\)/)).toBeInTheDocument();
    expect(added).toHaveBeenCalledOnce();
    expect(requests.map((item) => item.url)).toEqual([
      "/api/v2/server-configs/test-ssh",
      "/api/v2/server-configs",
      "/api/v2/server-configs/rental-a/attempt-preflight",
    ]);
    expect(requests[0].body).toMatchObject({ name: "rental-a", host: "gpu.example.test", port: 2222, user: "operator", key: "/keys/rental-a", gpu: true, tags: ["rental", "gpu", "team-a"] });
    expect(requests[1].body).toEqual(requests[0].body);
  });

  it("invalidates a successful test when connection details change", async () => {
    const fetchMock = vi.fn(async () => response(200, { ok: true, results: { hostname: "gpu-a", whoami: "operator" }, warnings: [], errors: [] }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AddComputeWizard existingNames={[]} onAdded={vi.fn()} onCancel={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fillConnection();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(screen.getByRole("button", { name: "Test Connection" }));
    expect(await screen.findByText("Connection succeeded")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.change(screen.getByLabelText("SSH port"), { target: { value: "2200" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(screen.getByText("Connection details changed. Test this configuration again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("keeps creation blocked and gives actionable guidance when the SSH test fails", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => response(200, { ok: false, results: {}, warnings: [], errors: ["SSH connection timed out"] }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AddComputeWizard existingNames={[]} onAdded={vi.fn()} onCancel={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fillConnection();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(screen.getByRole("button", { name: "Test Connection" }));

    expect(await screen.findByText(/SSH connection timed out/)).toHaveTextContent("Check the host, port, network reachability, SSH user, and credential reference, then retry.");
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/v2/server-configs/test-ssh");
  });

  it("keeps an added Compute when post-add preflight fails and reports the preflight error", async () => {
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      if (call === 1) return response(200, { ok: true, results: { hostname: "gpu-a", whoami: "operator" } });
      if (call === 2) return response(200, { name: "rental-a" });
      return response(409, { error: { code: "server_no_active_revision", message: "server has no active approved revision" } });
    }));
    const added = vi.fn();
    render(<AddComputeWizard existingNames={[]} onAdded={added} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fillConnection();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(screen.getByRole("button", { name: "Test Connection" }));
    await screen.findByText("Connection succeeded");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(screen.getByRole("button", { name: "Add Compute" }));
    expect(await screen.findByText("Preflight error")).toBeInTheDocument();
    expect(screen.getByText(/Compute was added, but its stored filesystem preflight could not be completed/)).toBeInTheDocument();
    expect(added).toHaveBeenCalledOnce();
  });
});
