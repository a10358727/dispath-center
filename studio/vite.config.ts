import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Studio is served by the control plane under the already-public `/static/`
// prefix (`/static/studio/`), so the build never needs a new auth exemption.
// The build output is not committed; CI and the pilot deploy step build it.
export default defineConfig({
  base: "/static/studio/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  build: { outDir: "../static/studio", emptyOutDir: true, sourcemap: false },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", ws: true },
      "/auth": "http://127.0.0.1:8000",
      "/approve": "http://127.0.0.1:8000",
      "/reject": "http://127.0.0.1:8000",
    },
  },
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], globals: true },
});
