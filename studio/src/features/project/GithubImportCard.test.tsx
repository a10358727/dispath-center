import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GithubImportCard, candidateMeetsGithubRule, isGithubRepoUrl, repoNameFromUrl } from "./GithubImportCard";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function renderCard(onDone = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <GithubImportCard onDone={onDone} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("GithubImportCard helpers", () => {
  it("accepts only canonical GitHub repository URLs", () => {
    expect(isGithubRepoUrl("https://github.com/acme/demo")).toBe(true);
    expect(isGithubRepoUrl("https://github.com/acme/demo.git")).toBe(true);
    expect(isGithubRepoUrl("https://gitlab.com/acme/demo")).toBe(false);
    expect(isGithubRepoUrl("https://github.com/acme/demo/tree/main")).toBe(false);
    expect(isGithubRepoUrl("git@github.com:acme/demo.git")).toBe(false);
    expect(repoNameFromUrl("https://github.com/acme/demo.git")).toBe("demo");
  });

  it("applies the GitHub-only rule to scanned candidates", () => {
    expect(candidateMeetsGithubRule({ git_remote: "git@github.com:acme/demo.git", readme_excerpt: "# demo" })).toBe(true);
    expect(candidateMeetsGithubRule({ git_remote: "https://gitlab.com/acme/demo.git", readme_excerpt: "# demo" })).toBe(false);
    expect(candidateMeetsGithubRule({ git_remote: "https://github.com/acme/demo", readme_excerpt: "  " })).toBe(false);
    expect(candidateMeetsGithubRule({})).toBe(false);
  });
});

describe("GithubImportCard", () => {
  it("states the rule, validates the URL, and posts a project_github_import request", async () => {
    const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("server-configs")) return json(200, [{ name: "server-b", enabled: true }, { name: "off", enabled: false }]);
      if (url.endsWith("/projects/github-import-requests")) {
        requests.push({ url, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
        return json(200, { id: 12, kind: "project_github_import", status: "pending", payload: { repo_url: "https://github.com/acme/demo", target_server: "server-b" } });
      }
      // The approval card's follow-up calls (decision / detail) are not under test here;
      // answer them so no unhandled rejection escapes the test run.
      return json(200, { id: 12, kind: "project_github_import", status: "pending", approval: { id: 12, status: "approved" } });
    }));
    renderCard();

    expect(screen.getByText(/README\.md 必須存在且非空/)).toBeInTheDocument();
    const url = screen.getByLabelText("GitHub 網址");
    fireEvent.change(url, { target: { value: "https://gitlab.com/acme/demo" } });
    expect(screen.getByRole("alert")).toHaveTextContent("只接受");
    expect(screen.getByRole("button", { name: "確認並匯入" })).toBeDisabled();

    fireEvent.change(url, { target: { value: "https://github.com/acme/demo" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "確認並匯入" })).toBeEnabled());
    expect(screen.getByText(/預設 demo/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "確認並匯入" }));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].body).toEqual({ repo_url: "https://github.com/acme/demo", target_server: "server-b" });
    expect(await screen.findByText(/從 GitHub 匯入專案/)).toBeInTheDocument();
    expect(document.querySelector("a[href^='http']")).toBeNull();
  });

  it("shows the server's rejection reason and the README guide", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("server-configs")) return json(200, [{ name: "server-b", enabled: true }]);
      if (url.endsWith("/projects/github-import-requests")) return json(400, { error: { code: "invalid_github_import_request", message: "專案 demo 已存在" } });
      return json(404, {});
    }));
    renderCard();
    fireEvent.change(screen.getByLabelText("GitHub 網址"), { target: { value: "https://github.com/acme/demo" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "建立匯入卡" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "建立匯入卡" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("專案 demo 已存在");

    fireEvent.click(screen.getByRole("button", { name: "還沒有 GitHub 專案？" }));
    expect(screen.getByText(/勾選「Add a README file」/)).toBeInTheDocument();
    expect(screen.getByText(/## 目的/)).toBeInTheDocument();
  });
});
