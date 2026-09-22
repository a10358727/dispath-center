import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EventsPage } from "./EventsPage";

afterEach(() => vi.unstubAllGlobals());

describe("EventsPage", () => {
  it("presents human activity while retaining the complete record in Advanced details", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([{
      event_id: "event-1",
      ts: "2026-09-21T00:00:00Z",
      action: "dispatch",
      result: "ok",
      actor: { id: "actor-internal-7", kind: "human", display_name: "Ada" },
      source: "audit.jsonl",
      durability: "transactional",
      params: { project: "demo", server: "gpu1" },
    }]), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><EventsPage /></QueryClientProvider>);

    await screen.findByText(/派工 · demo · gpu1/);
    const table = screen.getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "活動" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "操作者" })).toBeInTheDocument();
    expect(within(table).queryByRole("columnheader", { name: /action|actor|來源|source/i })).not.toBeInTheDocument();
    expect(within(table).getByText(/派工 · demo · gpu1/)).toBeInTheDocument();
    expect(within(table).queryByText("audit.jsonl")).not.toBeInTheDocument();

    fireEvent.click(within(table).getByRole("button", { name: "Advanced audit details" }));
    expect(within(table).getByText(/audit.jsonl/)).toBeInTheDocument();
    expect(within(table).getByText(/actor-internal-7/)).toBeInTheDocument();
  });
});
