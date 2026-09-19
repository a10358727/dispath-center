---
name: updating-pilot-site
description: Manual pilot deployment or rollback for dispatch-center. Invoke only with /updating-pilot-site after the user explicitly requests a pilot deploy, rollback, or dispatch-center-web restart; never auto-trigger from ordinary code/UI work.
disable-model-invocation: true
---

# Updating the Pilot Site

This is an explicit operational skill. It is not part of normal coding,
testing, review, or UI implementation.

Runtime layout:

- development repo: `/home/aied/dispath-center`
- runtime worktree: `/home/aied/pilot-run`
- service: user systemd unit `dispatch-center-web`
- runtime data such as `.env`, `servers.yaml`, `jobqueue.db`,
  `audit.jsonl`, and `results/` must remain untouched by checkout/deploy.

## Deploy preconditions

A normal deploy requires an existing commit SHA that has passed
`/release-gate`.

Require:

1. the user explicitly requested deployment;
2. an exact `VERIFIED_SHA` is identified;
3. `/release-gate` reported PASS for that same SHA;
4. development repo HEAD still equals that SHA;
5. no deployment step creates or amends a commit.

If exact release-gate evidence is unavailable or the SHA differs, stop with:

`PRECONDITION FAILED: run /release-gate for the exact commit to deploy.`

Do not silently rerun a weaker substitute gate.

## Deploy

For `<VERIFIED_SHA>`:

```bash
git -C /home/aied/pilot-run checkout <VERIFIED_SHA>
```

If `requirements.lock` changed for this release, install only from the pinned
lockfile using the repository's reviewed command:

```bash
/home/aied/dispath-center/.venv/bin/python -m pip install --require-hashes -r /home/aied/pilot-run/requirements.lock
```

If `studio/` changed, build the exact verified source and sync only the
generated Studio assets:

```bash
cd /home/aied/dispath-center/studio
npm ci --no-audit --no-fund
npm run build
rsync -a --delete /home/aied/dispath-center/static/studio/ /home/aied/pilot-run/static/studio/
```

Then restart and health-check:

```bash
systemctl --user restart dispatch-center-web
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
```

Expected health result: HTTP 200.

## Rollback

Rollback is explicit and only to a known previously deployed/known-good commit.

```bash
git -C /home/aied/pilot-run checkout <known-good-sha>
systemctl --user restart dispatch-center-web
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
```

Do not invent a rollback target.

## Hard boundaries

- Never deploy as a side effect of coding, testing, review, or merging.
- Never `git add`, commit, amend, merge, or push from this skill.
- Never edit code directly in `pilot-run`.
- Never delete or rewrite `jobqueue.db`, `audit.jsonl`, `results/`,
  `servers.yaml`, or runtime credentials.
- Do not claim restart safety from memory; preserve current sentinel/reconcile
  semantics and stop if runtime evidence contradicts them.
- Use `journalctl --user -u dispatch-center-web` for service diagnostics when
  explicitly needed.

After deployment report the deployed SHA, health result, dependency/Studio build
actions performed, and any rollback risk.
