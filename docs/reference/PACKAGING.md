# Packaging and Installation

Dispatch Center publishes two Python distributions from this repository. The
Control Plane and Node Agent deliberately do not contain one another:

| Distribution | Source project | Runtime dependencies | Commands |
| --- | --- | --- | --- |
| `dispatch-center` | repository root | FastAPI, SSH, OIDC, HTTP, YAML, typed settings | `dispatch`, `dispatch-api`, `dispatch-scheduler`, `dispatch-worker` |
| `dispatch-node-agent` | `agent/` | none (Python standard library only) | `dispatch-node-agent` |

The source-compatible launchers `python -m app.main` and `python -m agent`
remain supported during the architectural transition.

Before an application rollout, use the explicit SQLite migration preflight:
`dispatch db check`, `dispatch db backup`, and `dispatch db upgrade` (see
[`MIGRATIONS.md`](MIGRATIONS.md)).

## Build both distributions

Use Python 3.10 or newer with the locked development toolchain:

```bash
python -m pip install --require-hashes -r requirements.lock
python -m pip install --require-hashes -r requirements-dev.lock
python -m build --outdir dist .
python -m build --outdir dist agent
python scripts/check_wheel_boundaries.py dist
```

Each `python -m build` invocation creates an sdist and a wheel. Building the
two projects into the same `dist/` directory makes the installation smoke test
explicit:

The GitHub CI workflow publishes the two review wheels as the
`dispatch-wheels-<commit-sha>` artifact for 14 days after the package smoke
gate passes. This artifact is tied to the exact reviewed commit and can be
used to prepare a non-production canary installation; artifact availability is
not evidence that a worker has installed it or that a canary passed.

```bash
python -m venv /tmp/dispatch-package-smoke
/tmp/dispatch-package-smoke/bin/python -m pip install \
  --require-hashes -r requirements.lock
/tmp/dispatch-package-smoke/bin/python -m pip install --no-deps dist/*.whl
/tmp/dispatch-package-smoke/bin/python -m pip check
/tmp/dispatch-package-smoke/bin/dispatch --help
/tmp/dispatch-package-smoke/bin/dispatch-api --help
/tmp/dispatch-package-smoke/bin/dispatch-worker --help
DISPATCH_CONTROL_PLANE_URL=http://127.0.0.1:8888 \
DISPATCH_NODE_TOKEN=local-check-placeholder \
DISPATCH_NODE_WORKDIR=/tmp/dispatch-node-check \
  /tmp/dispatch-package-smoke/bin/dispatch-node-agent --check
```

The placeholder token is used only by the offline self-check and must never be
used to enroll a node. No assignment flag is enabled by building or installing
either distribution.

## Dependency groups

`requirements.txt` and `requirements.lock` contain the Control Plane's core
runtime. Wheel extras select optional capabilities:

```bash
python -m pip install 'dispatch-center[llm]'
python -m pip install 'dispatch-center[mcp]'
```

The `test` extra includes the optional integrations because the full suite
exercises them with fake clients. The `dev` extra contains build, lint, type,
and coverage tooling. `requirements-dev.lock` is the exact, hash-checked
aggregate used by CI; production does not install it.

## Rollback

Installing these wheels does not migrate data or enable a backend. Roll back
the Control Plane by reinstalling the previously retained wheel and restarting
the same process role. `dispatch-worker` is a template-only durable outbox
role until its separate deployment gate is approved. Roll back a Node Agent by disabling its user service and
restoring that node's `execution_backend: ssh`; no data migration is required.
The existing SSH execution backend remains the default and must not be removed.
