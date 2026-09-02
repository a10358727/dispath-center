# Development entry points (整頓 C3, DG-CONSOLIDATION-v1). The pilot deploy
# loop treats every commit as a deploy candidate, so `make test` (full,
# parallel) is the commit gate; `make test-serial` is the fallback when a
# failure needs deterministic ordering.
PY ?= .venv/bin/python
JOBS ?= auto

.PHONY: test test-serial test-affected check gate studio openapi-update mirrors-write

test:
	$(PY) -m pytest tests/ -q -n $(JOBS)

test-serial:
	$(PY) -m pytest tests/ -q

# Inner loop only: tests for the files changed since $(BASE) (default: uncommitted).
BASE ?= HEAD
test-affected:
	$(PY) scripts/test_affected.py --base $(BASE) --run

openapi-update:
	$(PY) scripts/openapi_snapshot.py --update

check:
	$(PY) -m ruff check app agent dispatch_center scripts tests
	$(PY) -m mypy app agent dispatch_center scripts
	bash .claude/skills/release-gate/scripts/static_checks.sh
	$(PY) scripts/audit_adoption_gate.py
	$(PY) scripts/sync_mirrors.py --check

# Edit the canonical side (app/mcp_bridge.py, dispatch_agent/protocol.py), then:
mirrors-write:
	$(PY) scripts/sync_mirrors.py --write

gate: check
	$(PY) scripts/coverage_gate.py
	$(MAKE) test

studio:
	npm ci --prefix studio --no-audit --no-fund
	npm run build --prefix studio
	npm test --prefix studio
