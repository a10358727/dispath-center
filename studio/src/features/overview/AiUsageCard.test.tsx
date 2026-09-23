import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AiProviderQuota } from "@/api/types";
import { AiUsageCard } from "./AiUsageCard";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function renderCard() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AiUsageCard />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const NOW = new Date("2026-09-23T10:00:00Z").getTime();

function availablePayload(): AiProviderQuota {
  return {
    schema: "ai-provider-quota-v1",
    generated_at: "2026-09-23T10:00:00Z",
    today: { date: "2026-09-23", timezone: "CST", starts_at: "2026-09-23T00:00:00+08:00" },
    providers: {
      claude_code: {
        provider: "claude_code",
        label: "Claude Code",
        account_quota: { availability: "unavailable", reason: "requires_credentialed_api", source: null, plan_type: null, limit_id: null, observed_at: null, windows: [] },
        local_usage: {
          availability: "available",
          reason: null,
          source: "claude_code_project_jsonl",
          today: { input_tokens: 300_000, output_tokens: 31_000, total_tokens: 331_000 },
          sessions_today: 3,
          events_today: 10,
          newest_event_at: "2026-09-23T09:00:00Z",
          models_today: {},
          scan: { files_considered: 5, files_scanned: 5, files_skipped: 0, bytes_read: 100, truncated: false, scanned_at: "2026-09-23T09:00:00Z" },
        },
        context_usage: { availability: "partial", reason: null, model: "claude-sonnet-5", used_tokens: 12_000, context_window: null, used_percent: null, observed_at: "2026-09-23T09:00:00Z" },
        estimated_cost: { availability: "unavailable", reason: "no_pricing_source", currency: null, today_usd: null },
      },
      codex: {
        provider: "codex",
        label: "Codex",
        account_quota: {
          availability: "available",
          reason: null,
          source: "codex_session_jsonl",
          plan_type: "plus",
          limit_id: "codex",
          observed_at: "2026-09-23T09:59:00Z",
          windows: [
            { id: "primary", label: "5h", window_minutes: 300, used_percent: 95, resets_at: "2026-09-23T11:26:00Z", state: "current" },
            { id: "secondary", label: "weekly", window_minutes: 10080, used_percent: 100, resets_at: "2026-09-20T00:00:00Z", state: "expired" },
          ],
        },
        local_usage: {
          availability: "available",
          reason: null,
          source: "codex_session_jsonl",
          today: { input_tokens: 150_000, output_tokens: 5_000, total_tokens: 155_000 },
          sessions_today: 2,
          events_today: 8,
          newest_event_at: "2026-09-23T09:00:00Z",
          models_today: {},
          scan: { files_considered: 3, files_scanned: 3, files_skipped: 0, bytes_read: 50, truncated: false, scanned_at: "2026-09-23T09:00:00Z" },
        },
        context_usage: { availability: "available", reason: null, model: "gpt-5-codex", used_tokens: 155_000, context_window: 256_000, used_percent: (155_000 / 256_000) * 100, observed_at: "2026-09-23T09:00:00Z" },
        estimated_cost: { availability: "unavailable", reason: "no_pricing_source", currency: null, today_usd: null },
      },
    },
  };
}

describe("AiUsageCard", () => {
  it("shows a loading state before data arrives", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    renderCard();
    expect(screen.getByText("載入中…")).toBeInTheDocument();
  });

  describe("with fake timers pinned to a fixed now", () => {
    beforeEach(() => {
      // shouldAdvanceTime lets fake setTimeout still fire in real time, so
      // RTL's async queries (findBy*/waitFor) keep working while Date.now()
      // stays pinned via setSystemTime.
      vi.useFakeTimers({ shouldAdvanceTime: true });
      vi.setSystemTime(NOW);
    });

    it("renders both providers' account quota, local usage, context usage, and cost without mixing categories", async () => {
      vi.stubGlobal("fetch", vi.fn(async () => json(200, availablePayload())));
      renderCard();

      expect(await screen.findByRole("heading", { name: "Claude Code" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Codex" })).toBeInTheDocument();

      // Claude Code: account quota unavailable (requires_credentialed_api).
      expect(screen.getByText(/需要帳戶憑證 API，本版本未啟用/)).toBeInTheDocument();
      // Claude Code: today tokens 331,000 and context partial.
      expect(screen.getByText(/上次回合 12,000 tokens（視窗未知）/)).toBeInTheDocument();

      // Codex: primary window 95% current, countdown 1h26m ahead.
      const primaryBar = screen.getByRole("progressbar", { name: "Codex 5 小時 使用率" });
      expect(primaryBar).toHaveAttribute("aria-valuenow", "95");
      expect(screen.getByText(/1 小時 26 分/)).toBeInTheDocument();

      // Codex: secondary window expired.
      const secondaryBar = screen.getByRole("progressbar", { name: "Codex 每週 使用率" });
      expect(secondaryBar).toHaveAttribute("aria-valuenow", "100");
      expect(screen.getByText(/視窗已重置/)).toBeInTheDocument();

      // Codex: plan badge.
      expect(screen.getByText("方案 plus")).toBeInTheDocument();

      // Codex: today tokens 155,000 with 2 sessions.
      expect(screen.getByText("155.0k")).toBeInTheDocument();
      expect(screen.getByText(/2 個 session/)).toBeInTheDocument();

      // Codex: context 155000/256000 -> 60.5%.
      expect(screen.getByText(/60\.5%/)).toBeInTheDocument();

      // Both estimated costs unavailable.
      expect(screen.getAllByText("未提供（無定價來源）")).toHaveLength(2);
    });
  });

  it("renders a stale badge and last-observed time for a stale account quota", async () => {
    const payload = availablePayload();
    payload.providers.codex.account_quota = {
      availability: "stale",
      reason: null,
      source: "codex_session_jsonl",
      plan_type: "plus",
      limit_id: "codex",
      observed_at: "2026-09-23T08:00:00Z",
      windows: [{ id: "primary", label: "5h", window_minutes: 300, used_percent: 40, resets_at: "2026-09-23T11:00:00Z", state: "stale" }],
    };
    vi.stubGlobal("fetch", vi.fn(async () => json(200, payload)));
    renderCard();

    expect(await screen.findByText("舊觀測")).toBeInTheDocument();
    expect(screen.getByText(/最後觀測/)).toBeInTheDocument();
  });

  it("renders reason-mapped text for unavailable local usage and account quota", async () => {
    const payload = availablePayload();
    payload.providers.claude_code.local_usage = {
      availability: "unavailable",
      reason: "home_missing",
      source: null,
      today: null,
      sessions_today: 0,
      events_today: 0,
      newest_event_at: null,
      models_today: {},
      scan: { files_considered: 0, files_scanned: 0, files_skipped: 0, bytes_read: 0, truncated: false, scanned_at: "2026-09-23T09:00:00Z" },
    };
    payload.providers.codex.account_quota = {
      availability: "unavailable",
      reason: "no_rate_limit_events",
      source: null,
      plan_type: null,
      limit_id: null,
      observed_at: null,
      windows: [],
    };
    vi.stubGlobal("fetch", vi.fn(async () => json(200, payload)));
    renderCard();

    expect(await screen.findByText(/找不到本機資料目錄/)).toBeInTheDocument();
    expect(screen.getByText(/session 檔案沒有額度事件/)).toBeInTheDocument();
  });

  it("shows an error alert with a working retry button, and a distinct 403 message", async () => {
    const fetchMock = vi.fn(async () => json(500, { detail: "boom" }));
    vi.stubGlobal("fetch", fetchMock);
    renderCard();

    expect(await screen.findByRole("alert")).toHaveTextContent("AI 使用量無法取得");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "重試" }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows a distinct message on 403 without permission", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(403, { detail: "forbidden" })));
    renderCard();
    expect(await screen.findByText("沒有權限查看 AI 使用量。")).toBeInTheDocument();
  });

  it("has no provider-switching controls and no buttons besides retry", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, availablePayload())));
    renderCard();

    expect(await screen.findByRole("heading", { name: "AI 使用量" })).toBeInTheDocument();
    // Data loaded successfully, so there is no error state and thus no 重試
    // button; no other buttons or provider-switching controls (select/combobox) exist.
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });
});
