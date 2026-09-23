import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AddComputeWizard } from "./AddComputeWizard";

const response = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
function fill() { fireEvent.change(screen.getByLabelText("運算資源名稱（不可重複）"), { target: { value: "rental-a" } }); fireEvent.change(screen.getByLabelText("主機"), { target: { value: "gpu.test" } }); fireEvent.change(screen.getByLabelText("SSH 使用者"), { target: { value: "operator" } }); fireEvent.change(screen.getByLabelText("私鑰路徑參照"), { target: { value: "/keys/a" } }); }
async function reachTrust() { fireEvent.click(screen.getByRole("button", { name: "繼續" })); fill(); fireEvent.click(screen.getByRole("button", { name: "繼續" })); fireEvent.click(screen.getByRole("button", { name: "新增運算資源設定" })); await screen.findByText("已新增運算資源設定"); fireEvent.click(screen.getByRole("button", { name: "觀察目前主機金鑰" })); await screen.findByText(/SHA256:observed/); fireEvent.click(screen.getByRole("button", { name: "明確接受觀察到的金鑰（TOFU）" })); await screen.findByText("主機身分已信任"); fireEvent.click(screen.getByRole("button", { name: "繼續" })); }
afterEach(() => vi.unstubAllGlobals());

describe("AddComputeWizard", () => {
  it("orders add, observe, trust, connection test, and preflight", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => { const url = String(input); urls.push(url); if (url.endsWith("/server-configs")) return response(200, {}); if (url.endsWith("/observe")) return response(200, { algorithm: "ssh-ed25519", fingerprint_sha256: "SHA256:observed" }); if (url.endsWith("/actions/trust")) return response(200, { state: "tofu" }); if (url.endsWith("/test-ssh")) return response(200, { ok: true }); if (url.endsWith("/attempt-preflight")) return response(200, { status: "eligible", filesystem_type: "ext4" }); return response(404, {}); }));
    const added = vi.fn(); render(<AddComputeWizard existingNames={[]} onAdded={added} onCancel={vi.fn()} />);
    await reachTrust(); fireEvent.click(screen.getByRole("button", { name: "測試已信任的連線並執行預檢" }));
    expect(await screen.findByText("預檢通過")).toBeInTheDocument(); expect(screen.getByText(/ext4/)).toBeInTheDocument(); expect(added).toHaveBeenCalledOnce();
    expect(urls).toEqual(["/api/v2/server-configs", "/api/v2/server-configs/rental-a/host-identity/observe", "/api/v2/server-configs/rental-a/host-identity/actions/trust", "/api/v2/server-configs/test-ssh", "/api/v2/server-configs/rental-a/attempt-preflight"]);
  });

  it("retains the added and trusted Compute when the post-trust connection test fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => { const url = String(input); if (url.endsWith("/server-configs")) return response(200, {}); if (url.endsWith("/observe")) return response(200, { algorithm: "ssh-ed25519", fingerprint_sha256: "SHA256:observed" }); if (url.endsWith("/actions/trust")) return response(200, { state: "tofu" }); if (url.endsWith("/test-ssh")) return response(200, { ok: false, errors: ["SSH timed out"] }); return response(404, {}); }));
    const added = vi.fn(); render(<AddComputeWizard existingNames={[]} onAdded={added} onCancel={vi.fn()} />);
    await reachTrust(); fireEvent.click(screen.getByRole("button", { name: "測試已信任的連線並執行預檢" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("運算資源仍保持已新增與已信任狀態。SSH timed out"); expect(added).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole("button", { name: "測試已信任的連線並執行預檢" })).toBeEnabled());
  });
});

describe("AddComputeWizard kind selection", () => {
  it("shows which kind is selected and lets the user switch it", () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(404, {})));
    render(<AddComputeWizard existingNames={[]} onAdded={vi.fn()} onCancel={vi.fn()} />);
    const rental = screen.getByRole("button", { name: /租用 GPU/ });
    const owned = screen.getByRole("button", { name: /自有伺服器/ });
    expect(rental).toHaveAttribute("aria-pressed", "true");
    expect(within(rental).getByText("已選擇")).toBeInTheDocument();
    expect(screen.getByText(/目前選擇：/)).toHaveTextContent("租用 GPU");
    fireEvent.click(owned);
    expect(owned).toHaveAttribute("aria-pressed", "true");
    expect(rental).toHaveAttribute("aria-pressed", "false");
    expect(within(owned).getByText("已選擇")).toBeInTheDocument();
    expect(screen.getByText(/目前選擇：/)).toHaveTextContent("自有伺服器");
    expect(screen.getByRole("button", { name: "繼續" })).toBeEnabled();
  });
});
