import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HardwarePanel, devicesOf, physicalTemplates, shortDigest } from "./HardwarePanel";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const PROJECT_ID = "10000000-0000-0000-0000-000000000001";
const SHA = "a".repeat(64);
const IMAGE = {
  id: "20000000-0000-0000-0000-000000000002", project_id: PROJECT_ID, project_version_id: "v", build_plan_id: "p", job_id: 7,
  kind: "firmware", output_name: "firmware", relative_path: "build/firmware.bin", sha256: SHA, size_bytes: 4096,
  registered_at: "2026-09-03T00:00:00Z", known_good_marked_at: null, known_good_source: null,
};
const TEMPLATES = [
  { run_profile_id: "t-compute", name: "train", revision: 1, status: "approved", classification: "typed", head_spec: {} },
  { run_profile_id: "t-flash", name: "flash", revision: 1, status: "approved", classification: "typed", head_spec: { action_class: "program" } },
  { run_profile_id: "t-hil", name: "hil", revision: 1, status: "archived", classification: "typed", head_spec: { action_class: "hil_test" } },
];

function stubApi(calls: string[], overrides: { images?: unknown[]; receipts?: unknown[]; markStatus?: number } = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.includes("/api/v2/projects-matrix")) return jsonResponse(200, { projects: [{ id: PROJECT_ID, name: "demo", instances: {} }] });
      if (url.endsWith("/hardware-images")) return jsonResponse(200, { items: overrides.images ?? [IMAGE], next_cursor: null });
      if (url.endsWith("/hardware-receipts")) return jsonResponse(200, { items: overrides.receipts ?? [], next_cursor: null });
      if (url.includes("/run-templates")) return jsonResponse(200, { items: TEMPLATES, next_cursor: null });
      if (url.endsWith("/workspace")) return jsonResponse(200, { project: { id: PROJECT_ID, name: "demo" }, run_creation_options: { project_version_candidates: [{ id: "version-1" }], ssh_target_candidates: [{ server_name: "board-118", id: "rev-1", ready: true }] } });
      if (url.endsWith("/api/v2/servers")) return jsonResponse(200, [{ name: "board-118", online: true, devices: { "esp32-1": "present", "relay-1": "absent" } }]);
      if (url.endsWith("/known-good")) return jsonResponse(overrides.markStatus ?? 200, overrides.markStatus === 403 ? { error: { code: "forbidden", message: "Platform administration is required" } } : { image: { ...IMAGE, known_good_marked_at: "2026-09-03T01:00:00Z", known_good_source: "direct" }, marked_now: true });
      if (url.endsWith("/hardware-action-previews")) return jsonResponse(200, { plan_digest: "d".repeat(64), hardware: { action_class: "program", server_name: "board-118", device_id: "esp32-1", image_sha256: SHA } });
      if (url.endsWith("/hardware-action-requests")) return jsonResponse(202, { approval_id: 41, execution_plan_id: "plan", status: "pending" });
      if (url.endsWith("/api/v2/approvals/41")) return jsonResponse(200, { id: 41, kind: "hardware_action_v2", title: "硬體實體動作", summary: "燒錄 · board-118 · esp32-1", status: "pending", created_at: "2026-09-03T00:00:00Z", payload: { action_class: "program", device_id: "esp32-1" } });
      return jsonResponse(404, { detail: "not found" });
    }),
  );
}

function renderPanel() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <HardwarePanel project="demo" />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("hardware helpers", () => {
  it("keeps only approved physical templates and reads device presence per server", () => {
    expect(physicalTemplates(TEMPLATES).map((item) => item.name)).toEqual(["flash"]);
    expect(devicesOf([{ name: "board-118", devices: { "esp32-1": "present" } }], "board-118")).toEqual([{ id: "esp32-1", presence: "present" }]);
    expect(devicesOf([{ name: "board-118", devices: null }], "board-118")).toEqual([]);
    expect(shortDigest(SHA)).toBe("aaaaaaaaaaaa");
    expect(shortDigest(null)).toBe("—");
  });
});

describe("HardwarePanel (DG-HARDWARE-EXECUTION v1 P4)", () => {
  it("lists images with the known-good state and files a program card through preview → request", async () => {
    const calls: string[] = [];
    stubApi(calls);
    renderPanel();
    expect(await screen.findByText(/firmware · 4 KiB/)).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaaaaaa")).toBeInTheDocument();
    expect(screen.getByText("還沒有收據。實體動作的任務結束後，工作機寫的 hardware_receipt.json 會回收到這裡；沒有收據＝未知，不改任務結果。")).toBeInTheDocument();
    //: the composer shows only the physical template, the devices with presence, and needs an image first
    expect(await screen.findByText("flash（燒錄）")).toBeInTheDocument();
    expect(screen.queryByText(/train/)).not.toBeInTheDocument();
    expect(await screen.findByText("esp32-1：present")).toBeInTheDocument();
    expect(screen.getByText("relay-1：absent")).toBeInTheDocument();
    expect(screen.getByText("燒錄需要選一個已登記的映像")).toBeInTheDocument();
    const imageSelect = screen.getAllByRole("combobox").find((el) => (el as HTMLSelectElement).options[0]?.text.startsWith("選擇已登記的映像"))!;
    fireEvent.change(imageSelect, { target: { value: SHA } });
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));
    await screen.findByText(/燒錄 · board-118 · 裝置 esp32-1/);
    fireEvent.click(screen.getByRole("button", { name: "送出核准卡" }));
    await screen.findByText("硬體實體動作");
    const preview = calls.find((call) => call.endsWith("/hardware-action-previews"));
    const request = calls.find((call) => call.endsWith("/hardware-action-requests"));
    expect(preview).toBe(`POST /api/v2/projects/${PROJECT_ID}/hardware-action-previews`);
    expect(request).toBe(`POST /api/v2/projects/${PROJECT_ID}/hardware-action-requests`);
    //: the card is rendered for a second person — never confirmed immediately
    expect(screen.getByRole("button", { name: "核准" })).toBeInTheDocument();
    expect(screen.queryByText("已由你本人立即核准，完整留稽核。")).not.toBeInTheDocument();
  });

  it("offers the direct known-good mark and explains a non-admin refusal", async () => {
    const calls: string[] = [];
    stubApi(calls, { markStatus: 403 });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "標記 known-good" }));
    await screen.findByText("只有平台管理員可以直接標記；或在實機測試核准時勾選。");
    expect(calls).toContain(`POST /api/v2/projects/${PROJECT_ID}/hardware-images/${IMAGE.id}/known-good`);
  });

  it("renders receipts with their closed fields and the unknown state", async () => {
    stubApi([], {
      receipts: [
        { job_id: 9, approval_id: 41, action_class: "program", status: "collected", collected_at: "2026-09-03T02:00:00Z", receipt_json: JSON.stringify({ device_id: "esp32-1", tool: "esptool", verify: "verified", exit_code: 0, image_sha256: SHA }) },
        { job_id: 8, approval_id: 40, action_class: "power", status: "missing", collected_at: "2026-09-03T01:00:00Z", receipt_json: null },
      ],
    });
    renderPanel();
    expect(await screen.findByText(/esp32-1 · esptool · verify=verified · exit=0 · 映像 aaaaaaaaaaaa/)).toBeInTheDocument();
    expect(screen.getByText("無收據（未知）")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("電源")).toBeInTheDocument());
  });
});
