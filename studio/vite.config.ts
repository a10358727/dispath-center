import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Studio is served by the control plane under the already-public `/static/`
// prefix (`/static/studio/`), so the build never needs a new auth exemption.
// The build output is not committed; CI and the pilot deploy step build it.
const previewEnv = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};
const previewApiTarget = previewEnv.PREVIEW_API_TARGET || "http://127.0.0.1:8000";
const previewSessionCookie = previewEnv.PREVIEW_SESSION_COOKIE || "";

export default defineConfig({
  base: "/static/studio/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  build: { outDir: "../static/studio", emptyOutDir: true, sourcemap: false },
  server: {
    port: 5173,
    // Local preview (.claude/skills/local-preview): PREVIEW_API_TARGET points the
    // proxy at an isolated backend and PREVIEW_SESSION_COOKIE lets the proxy
    // attach a seeded preview-admin session so the browser needs no OIDC login.
    // Both are dev-server-only; the production build never reads them.
    proxy: Object.fromEntries(
      ["/api", "/auth", "/approve", "/reject"].map((path) => [
        path,
        {
          target: previewApiTarget,
          ws: path === "/api",
          ...(previewSessionCookie ? { headers: { Cookie: previewSessionCookie } } : {}),
        },
      ]),
    ),
  },
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], globals: true },
});
