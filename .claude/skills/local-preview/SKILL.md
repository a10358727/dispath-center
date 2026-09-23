---
name: local-preview
description: Use when a Studio or API change should be seen, clicked or screenshotted in a real browser before merge or pilot deploy, when the user asks to 預覽／試玩／demo／本機看看 the app, or when reproducing a UI bug report; never for deployment or pilot operations.
---

# Local Preview

Isolated dispatch-center on loopback for fast UI/API iteration: backend on
`127.0.0.1:8001` with its own runtime dir, plus a Vite dev server on
`127.0.0.1:5173` (hot reload) whose proxy attaches a seeded **Preview Admin**
session, so the browser lands in the Studio with no OIDC login. Nothing
touches `/home/aied/pilot-run`, real workers, or credentials.

## Quick reference

| Command (from repo root) | Effect |
|---|---|
| `bash .claude/skills/local-preview/scripts/preview.sh start` | seed admin, start backend + Vite, print URL |
| `… preview.sh status` | pids, health, `/api/v2/me` check, URL |
| `… preview.sh logs` / `stop` / `restart` | tail logs / stop both / restart |
| `… preview.sh reset` | stop, wipe `.runtime/preview`, fresh DB, start |
| `… preview.sh cookie` | session cookie for `curl -H "Cookie: …" http://127.0.0.1:8001/api/v2/…` |
| `node .claude/skills/local-preview/scripts/drive.mjs steps.json` | headless click-through + screenshots |

Studio URL: `http://127.0.0.1:5173/static/studio/` (hash routes: `#/compute`,
`#/projects`, `#/overview` …). Studio edits hot-reload; backend edits need
`restart`. The user can open the same URL in a browser on this machine.

## Driving it (Claude)

Preview must be running (`status` shows both running; otherwise `start`).
Write a steps file, run `node .claude/skills/local-preview/scripts/drive.mjs steps.json`,
then `Read` the PNGs to look at them. `out` is any directory (created if
missing) — use the session scratchpad directory named in the system prompt.

Just look at a page:

```json
{ "out": "/tmp/preview-shots", "steps": [ {"goto": "#/overview"}, {"shot": "overview.png", "full": true} ] }
```

Click through a flow:

```json
{ "base": "http://127.0.0.1:5173/static/studio/", "out": "/tmp/preview-shots", "steps": [
  {"goto": "#/compute"},
  {"click": "role=button[name='＋ 新增運算資源']"},
  {"expect": "text=第 1 步，共 4 步"},
  {"fill": "label=主機", "value": "127.0.0.1"},
  {"shot": "wizard.png", "full": true}
]}
```

Ops: `goto` (relative to base or absolute), `click`, `fill`+`value`,
`select`+`value`, `wait` (ms), `expect` (waits visible, else fails and saves
`failure.png`), `shot` (+`full`). Selectors are Playwright selectors;
`label=` maps to `getByLabel`. Browser console/page errors are printed at the
end — read them. The runner uses the Playwright package cached by the
Playwright MCP; if it reports `Executable doesn't exist … chromium`, install
the matching Chromium once:

```bash
node "$(ls -d ~/.npm/_npx/*/node_modules/playwright | head -1)/cli.js" install chromium
```

The Playwright MCP tools themselves only work if the MCP config passes
`--browser chromium` (its default wants Google Chrome, which is not installed).

## What is isolated

`.runtime/preview/` (gitignored): `.env` (open-development: `AUTHORIZATION_MODE=off`,
OIDC/legacy token off, loopback only), `jobqueue.db`, `audit.jsonl`,
`servers.yaml`, `keys/preview_key` (throwaway ed25519 accepted by the
add-compute wizard's key-path rule), `home/` (AI-usage scan dir), logs, pids,
`session.cookie`. Delete the dir or run `reset` to start over.

## Limits

- Flows that SSH to a worker (host-identity trust, connection test, preflight,
  Runs, GitHub import clone) need a reachable host; `127.0.0.1` with
  `keys/preview_key` in the target's `authorized_keys` works for demos, real
  workers never.
- Preview is not evidence for release: merge → `/release-gate` →
  `/updating-pilot-site` stays the only path to the pilot.
- Ports 8001/5173 are fixed (`PREVIEW_API_PORT`/`PREVIEW_VITE_PORT` override).

## Common mistakes

| Symptom | Fix |
|---|---|
| Vite shows the login page / API 401 | run `start` again (cookie is injected by the proxy; a stale `session.cookie` after `reset` needs a restart) |
| `backend did not become healthy` | `preview.sh logs`; port busy → `PREVIEW_API_PORT=8002 … start` |
| `Executable doesn't exist … chromium_headless_shell` | run the one-line Chromium install above |
| `drive.mjs` fails with `Unknown engine` | use Playwright selectors (`role=…`, `text=…`) or the `label=` shorthand |
| Shell hangs after `start` | always run the script itself; do not background it with `&` from another wrapper |
