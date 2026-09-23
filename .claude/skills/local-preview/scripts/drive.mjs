#!/usr/bin/env node
// Drive the local preview in headless Chromium and take screenshots.
//   node drive.mjs steps.json
// steps.json: { "base": "http://127.0.0.1:5173/static/studio/", "out": "<dir>", "steps": [
//   {"goto": "#/compute"},                       // relative to base (or absolute URL)
//   {"click": "role=button[name='＋ 新增運算資源']"},   // any Playwright selector
//   {"fill": "label=主機", "value": "127.0.0.1"},
//   {"wait": 500},                               // ms
//   {"expect": "text=第 1 步，共 4 步"},            // fails the run if not visible
//   {"shot": "01-compute.png", "full": true}
// ]}
// Uses the Playwright package cached by the Playwright MCP server (npx cache)
// so no extra install is needed; override with PLAYWRIGHT_PKG=<dir>.
import { createRequire } from "node:module";
import { readFileSync, readdirSync, existsSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

function findPlaywright() {
  if (process.env.PLAYWRIGHT_PKG) return process.env.PLAYWRIGHT_PKG;
  const npx = join(homedir(), ".npm", "_npx");
  if (existsSync(npx)) {
    for (const dir of readdirSync(npx)) {
      const candidate = join(npx, dir, "node_modules", "playwright");
      if (existsSync(join(candidate, "package.json"))) return candidate;
    }
  }
  throw new Error("playwright package not found; run `npx -y playwright@latest install chromium` or set PLAYWRIGHT_PKG");
}

const stepsPath = process.argv[2];
if (!stepsPath) { console.error("usage: node drive.mjs steps.json"); process.exit(2); }
const plan = JSON.parse(readFileSync(stepsPath, "utf8"));
const base = plan.base ?? "http://127.0.0.1:5173/static/studio/";
const out = plan.out ?? ".";
mkdirSync(out, { recursive: true });
const require = createRequire(import.meta.url);
const { chromium } = require(findPlaywright());

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
// `label=<accessible label>` → getByLabel; everything else is a Playwright selector.
const loc = (sel) => (sel.startsWith("label=") ? page.getByLabel(sel.slice(6)) : page.locator(sel));
page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
page.on("console", (msg) => { if (msg.type() === "error") errors.push(`console: ${msg.text()}`); });
try {
  for (const step of plan.steps ?? []) {
    if (step.goto !== undefined) {
      const url = /^https?:/.test(step.goto) ? step.goto : base + step.goto;
      await page.goto(url, { waitUntil: "networkidle" });
    } else if (step.click) {
      await loc(step.click).first().click();
    } else if (step.fill) {
      await loc(step.fill).first().fill(String(step.value ?? ""));
    } else if (step.select) {
      await loc(step.select).first().selectOption(String(step.value ?? ""));
    } else if (step.wait !== undefined) {
      await page.waitForTimeout(Number(step.wait));
    } else if (step.expect) {
      await loc(step.expect).first().waitFor({ state: "visible", timeout: step.timeout ?? 10000 });
    } else if (step.shot) {
      const file = join(out, step.shot);
      await page.screenshot({ path: file, fullPage: Boolean(step.full) });
      console.log(`shot ${file}`);
    } else {
      throw new Error(`unknown step ${JSON.stringify(step)}`);
    }
  }
  if (errors.length) { console.log("browser errors:"); for (const e of errors) console.log("  " + e); }
  console.log("done");
} catch (err) {
  await page.screenshot({ path: join(out, "failure.png"), fullPage: true }).catch(() => {});
  console.error(`FAILED: ${err.message} (see ${join(out, "failure.png")})`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
