import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SessionView } from "./SessionView";

Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });

const mocks = vi.hoisted(() => ({
  events: [] as { seq: number; kind: string; payload: Record<string, unknown>; created_at: string }[],
  send: vi.fn(),
  sendPending: false,
  sessionError: false,
}));

function mutation(mutate = vi.fn()) {
  return { mutate, mutateAsync: vi.fn(), isPending: false, error: null, data: undefined };
}

vi.mock("@/api/hooks", () => ({
  promotableCheckpointTasks: () => [],
  useCheckpointTasks: () => ({ data: [], refetch: vi.fn() }),
  useSession: () => ({
    isLoading: false,
    isError: mocks.sessionError,
    data: {
      id: "session-1", project_id: "demo", status: "open", pending_permissions: [],
      runtime: { runner_id: "runner-1", runner_connected: true, task_state: "idle", sdk_session_id: "sdk-1", cost_usd: null, last_seq: 0, options: { model: "claude" } },
    },
  }),
  useSessionActions: () => ({
    start: mutation(), send: { ...mutation(mocks.send), isPending: mocks.sendPending }, interrupt: mutation(), close: mutation(), diff: mutation(), decide: mutation(), configure: mutation(), files: mutation(), checkpoint: mutation(),
  }),
}));

vi.mock("./useSessionStream", () => ({ useSessionStream: () => ({ events: mocks.events, connected: true }) }));

describe("SessionView composer", () => {
  beforeEach(() => {
    mocks.events = [];
    mocks.send.mockReset();
    mocks.sendPending = false;
    mocks.sessionError = false;
  });

  it("sends on Enter, keeps Shift+Enter multiline, and ignores composing Enter", () => {
    render(<SessionView sessionId="session-1" />);
    const composer = screen.getByRole("textbox");
    fireEvent.change(composer, { target: { value: "first line" } });
    fireEvent.keyDown(composer, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(composer, { key: "Enter", isComposing: true });
    expect(mocks.send).not.toHaveBeenCalled();
    fireEvent.keyDown(composer, { key: "Enter" });
    expect(mocks.send).toHaveBeenCalledWith({ text: "first line", attachments: undefined }, expect.any(Object));
  });

  it("preserves failed text and retries the same payload", () => {
    mocks.send.mockImplementationOnce((_payload, options) => options.onError(new Error("offline")));
    render(<SessionView sessionId="session-1" />);
    const composer = screen.getByRole("textbox");
    fireEvent.change(composer, { target: { value: "please inspect" } });
    fireEvent.click(screen.getByRole("button", { name: "送出" }));
    expect(composer).toHaveValue("please inspect");
    expect(screen.getByText("failed to send")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(mocks.send).toHaveBeenLastCalledWith({ text: "please inspect", attachments: undefined }, expect.any(Object));
  });

  it("shows explicit unavailable context details instead of inferred category totals", () => {
    mocks.events = [{ seq: 1, kind: "context", payload: { usage: { categories: [{ name: "input", tokens: 40 }] } }, created_at: "2026-09-21T00:00:00Z" }];
    render(<SessionView sessionId="session-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Context usage unavailable" }));
    expect(screen.getByText(/has not supplied a trustworthy/)).toBeInTheDocument();
    expect(screen.queryByText("40")).not.toBeInTheDocument();
  });

  it("shows validated estimated near-limit telemetry and only its valid details", () => {
    mocks.events = [{ seq: 1, kind: "context", payload: { usage: { total_tokens: 90, context_window: 100, estimated: true, categories: [{ name: "recent conversation", tokens: 25 }, { name: "opaque" }] } }, created_at: "2026-09-21T00:00:00Z" }];
    render(<SessionView sessionId="session-1" />);
    const context = screen.getByRole("button", { name: /Context 90%.*Estimated.*Near limit/ });
    fireEvent.click(context);
    expect(screen.getByRole("region", { name: "Context details" })).toHaveTextContent("recent conversation25");
    expect(screen.queryByText("opaque")).not.toBeInTheDocument();
  });

  it("makes sending and unavailable session states explicit", () => {
    mocks.sendPending = true;
    const { unmount } = render(<SessionView sessionId="session-1" />);
    expect(screen.getByText("sending")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeDisabled();
    unmount();

    mocks.sendPending = false;
    mocks.sessionError = true;
    render(<SessionView sessionId="session-1" />);
    expect(screen.getByText("session unavailable")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeDisabled();
  });

  it("recovers grounded analysis after reload and Continue sends through the same session action", () => {
    const planId = "33333333-3333-4333-8333-333333333333";
    mocks.events = [
      { seq: 1, kind: "user_text", payload: { text: `[dispatch:analyze-run:${planId}] analyze` }, created_at: "2026-09-21T00:00:00Z" },
      { seq: 2, kind: "tool_use", payload: { tool_use_id: "evidence-1", name: "mcp__dispatch__get_run", input: { plan_id: planId } }, created_at: "2026-09-21T00:00:01Z" },
      { seq: 3, kind: "tool_result", payload: { tool_use_id: "evidence-1", is_error: false, content: JSON.stringify({ plan_id: planId, state: "succeeded" }) }, created_at: "2026-09-21T00:00:02Z" },
      { seq: 4, kind: "assistant_text", payload: { text: "Loss improved. Recommendation: adjust one parameter." }, created_at: "2026-09-21T00:00:03Z" },
      { seq: 5, kind: "result", payload: { is_error: false, text: "done" }, created_at: "2026-09-21T00:00:04Z" },
    ];
    render(<SessionView sessionId="session-1" />);
    const resultCard = screen.getByTestId("result-analysis-card");
    expect(resultCard).toHaveTextContent(/get_run.*evidence-1/);
    expect(resultCard).toHaveTextContent("Loss improved");
    expect(resultCard).not.toHaveTextContent(planId);
    expect(screen.getByText("Analyze this Run")).toBeInTheDocument();
    expect(screen.queryByText(`[dispatch:analyze-run:${planId}] analyze`)).not.toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Advanced evidence references" }));
    expect(resultCard).toHaveTextContent(planId);
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(mocks.send).toHaveBeenCalledTimes(1);
    const payload = mocks.send.mock.calls[0][0];
    expect(payload.text).toContain(`[dispatch:continue-run:${planId}]`);
    expect(payload.text).toContain("same Project session");
    expect(payload.text).toContain("new governed request_run proposal and human approval");
  });

  it("shows a human label for a persisted generated Continue action", () => {
    const planId = "33333333-3333-4333-8333-333333333333";
    const raw = `[dispatch:continue-run:${planId}] Continue with internal context`;
    mocks.events = [{ seq: 1, kind: "user_text", payload: { text: raw }, created_at: "2026-09-21T00:00:00Z" }];
    render(<SessionView sessionId="session-1" />);
    expect(screen.getByText("Continue from this Run analysis")).toBeInTheDocument();
    expect(screen.getByText(raw)).not.toBeVisible();
    fireEvent.click(screen.getByText("Advanced action details"));
    expect(screen.getByText(raw)).toBeVisible();
  });
});
