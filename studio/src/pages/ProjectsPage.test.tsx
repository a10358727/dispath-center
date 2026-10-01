import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectsPage } from "./ProjectsPage";

function Location() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}{location.search}</div>;
}

function renderPage() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const body = String(input).includes("projects-matrix")
      ? { projects: [{ name: "demo", repo_or_path: "https://github.com/acme/demo", instances: [{ server: "5090", git_branch: "main", git_commit: "abcdef1234" }] }] }
      : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/projects"]}>
        <Location />
        <Routes>
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="*" element={<div>elsewhere</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("ProjectsPage cards", () => {
  it("opens the project when any part of the card is clicked", async () => {
    renderPage();
    const card = await screen.findByRole("link", { name: "開啟專案 demo" });
    fireEvent.click(screen.getByText("https://github.com/acme/demo"));
    expect(screen.getByTestId("location")).toHaveTextContent("/projects/demo");
    expect(card).not.toBeInTheDocument();
  });

  it("keeps the Run link separate and supports the keyboard", async () => {
    renderPage();
    const card = await screen.findByRole("link", { name: "開啟專案 demo" });
    fireEvent.click(screen.getByRole("link", { name: "實驗與 Run →" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/runs?project=demo");
    expect(card).not.toBeInTheDocument();
  });

  it("opens the project with Enter on the focused card", async () => {
    renderPage();
    const card = await screen.findByRole("link", { name: "開啟專案 demo" });
    fireEvent.keyDown(card, { key: "Enter" });
    expect(screen.getByTestId("location")).toHaveTextContent("/projects/demo");
  });
});
