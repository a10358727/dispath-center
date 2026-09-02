# Coverage baseline

Status: active quality gate  
Established: 2026-08-03

CI enforces 35% branch coverage across `app`, `agent`, and `dispatch_center`.
The current stable measurement is 35.08% from 1177 pure-core tests covering authorization,
execution attempts and plans, Product Run state projection, Node protocol/daemon behavior,
dataset snapshots, server publication, migrations, identity/OIDC, typed configuration,
scheduling, and the SSH state-machine helpers.

The coverage command is `python scripts/coverage_gate.py`. It selects
`tests/ -m "not untraced"`: `tests/conftest.py` marks a test `untraced` when it
uses the `api_client` fixture or its module references `TestClient(` /
`asyncio.to_thread` (整頓 C3) — there is no hand-kept module list. The full
offline suite (`make test`, parallel via pytest-xdist) remains a separate CI
step and is not replaced by the coverage subset.

## Temporary tracing constraint

Coverage tracing deadlocks two legacy threaded seams in this worktree:

- FastAPI `TestClient` lifespan startup through the monolithic `app.main`;
- the mailer fake that uses `asyncio.to_thread`.

Those tests pass without tracing and remain in the full regression suite. The
coverage gate rejects adding a selected module that references `TestClient`,
`api_client`, or `asyncio.to_thread`, so CI fails quickly instead of hanging.
API router extraction and worker separation must retire this constraint rather
than treating it as permanent architecture.

The threshold may increase after coverage grows. It must never be lowered or
the measured source narrowed merely to land a change.
