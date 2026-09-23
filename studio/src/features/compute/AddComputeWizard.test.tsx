import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AddComputeWizard } from "./AddComputeWizard";

const response = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
function fill() { fireEvent.change(screen.getByLabelText("Unique Compute name"), { target: { value: "rental-a" } }); fireEvent.change(screen.getByLabelText("Host"), { target: { value: "gpu.test" } }); fireEvent.change(screen.getByLabelText("SSH user"), { target: { value: "operator" } }); fireEvent.change(screen.getByLabelText("Private-key path reference"), { target: { value: "/keys/a" } }); }
async function reachTrust() { fireEvent.click(screen.getByRole("button", { name: "Continue" })); fill(); fireEvent.click(screen.getByRole("button", { name: "Continue" })); fireEvent.click(screen.getByRole("button", { name: "Add Compute configuration" })); await screen.findByText("Compute configuration added"); fireEvent.click(screen.getByRole("button", { name: "Observe current host key" })); await screen.findByText(/SHA256:observed/); fireEvent.click(screen.getByRole("button", { name: "Explicitly accept observed key (TOFU)" })); await screen.findByText("Host identity trusted"); fireEvent.click(screen.getByRole("button", { name: "Continue" })); }
afterEach(() => vi.unstubAllGlobals());

describe("AddComputeWizard", () => {
  it("orders add, observe, trust, connection test, and preflight", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => { const url = String(input); urls.push(url); if (url.endsWith("/server-configs")) return response(200, {}); if (url.endsWith("/observe")) return response(200, { algorithm: "ssh-ed25519", fingerprint_sha256: "SHA256:observed" }); if (url.endsWith("/actions/trust")) return response(200, { state: "tofu" }); if (url.endsWith("/test-ssh")) return response(200, { ok: true }); if (url.endsWith("/attempt-preflight")) return response(200, { status: "eligible", filesystem_type: "ext4" }); return response(404, {}); }));
    const added = vi.fn(); render(<AddComputeWizard existingNames={[]} onAdded={added} onCancel={vi.fn()} />);
    await reachTrust(); fireEvent.click(screen.getByRole("button", { name: "Test trusted connection & run preflight" }));
    expect(await screen.findByText("Preflight eligible")).toBeInTheDocument(); expect(screen.getByText(/ext4/)).toBeInTheDocument(); expect(added).toHaveBeenCalledOnce();
    expect(urls).toEqual(["/api/v2/server-configs", "/api/v2/server-configs/rental-a/host-identity/observe", "/api/v2/server-configs/rental-a/host-identity/actions/trust", "/api/v2/server-configs/test-ssh", "/api/v2/server-configs/rental-a/attempt-preflight"]);
  });

  it("retains the added and trusted Compute when the post-trust connection test fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => { const url = String(input); if (url.endsWith("/server-configs")) return response(200, {}); if (url.endsWith("/observe")) return response(200, { algorithm: "ssh-ed25519", fingerprint_sha256: "SHA256:observed" }); if (url.endsWith("/actions/trust")) return response(200, { state: "tofu" }); if (url.endsWith("/test-ssh")) return response(200, { ok: false, errors: ["SSH timed out"] }); return response(404, {}); }));
    const added = vi.fn(); render(<AddComputeWizard existingNames={[]} onAdded={added} onCancel={vi.fn()} />);
    await reachTrust(); fireEvent.click(screen.getByRole("button", { name: "Test trusted connection & run preflight" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Compute remains added and trusted. SSH timed out"); expect(added).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole("button", { name: "Test trusted connection & run preflight" })).toBeEnabled());
  });
});
