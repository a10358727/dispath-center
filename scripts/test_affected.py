"""Pick the tests worth running for the files changed since a git base (整頓 C4).

An inner-loop convenience, not a commit gate: `make test` (the full parallel
suite) stays mandatory before every commit because a commit is a pilot deploy
candidate and the monolith has no module boundary a mapping could trust.

    python scripts/test_affected.py            # print the pytest command
    python scripts/test_affected.py --run      # run it
    python scripts/test_affected.py --base main

Rules (first match wins per changed file; "full" wins overall):
  tests/test_X.py                     -> itself
  app/main.py, app/db.py, app/settings/*, tests/conftest.py,
  pyproject.toml, requirements*       -> full suite
  app/foo.py, dispatch_center/.../foo.py
                                      -> tests whose source mentions the
                                         module (`app.foo` / `foo.py`) plus
                                         tests/test_foo*.py; none found -> full
  docs/**, .claude/**, README.md, CLAUDE.md, AGENTS.md, CONTRIBUTING.md
                                      -> test_document_authority.py,
                                         test_capability_ledger.py
  studio/**                           -> npm test (printed) + test_studio_static.py
  .github/**                          -> test_ci_release_gate.py
  scripts/x.py                        -> tests/test_x*.py, else full
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS = REPOSITORY_ROOT / "tests"
FULL = ("tests/",)
FULL_TRIGGERS = ("app/main.py", "app/db.py", "tests/conftest.py", "pyproject.toml")
DOC_TESTS = ("tests/test_document_authority.py", "tests/test_capability_ledger.py")


def changed_files(base: str) -> list[str]:
    diff = subprocess.run(
        ["git", "diff", "--name-only", base, "--"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(set(diff) | set(untracked))


def tests_mentioning(module_path: Path) -> list[str]:
    stem = module_path.stem
    dotted = ".".join(module_path.with_suffix("").parts)
    found: set[str] = set()
    for test_file in sorted(TESTS.glob("test_*.py")):
        source = test_file.read_text(encoding="utf-8", errors="ignore")
        if dotted in source or f"{stem}.py" in source or f"from {module_path.parts[0]} import {stem}" in source:
            found.add(str(test_file.relative_to(REPOSITORY_ROOT)))
    for sibling in sorted(TESTS.glob(f"test_{stem}*.py")):
        found.add(str(sibling.relative_to(REPOSITORY_ROOT)))
    return sorted(found)


def select(files: list[str]) -> tuple[list[str], list[str]]:
    """Return (pytest targets, extra shell commands). A single full-suite
    trigger collapses the selection to `tests/`."""

    targets: set[str] = set()
    extras: list[str] = []
    for name in files:
        path = Path(name)
        if name.startswith(FULL_TRIGGERS) or name.startswith("app/settings/") or name.startswith("requirements"):
            return list(FULL), extras
        if name.startswith("tests/") and path.name.startswith("test_") and path.suffix == ".py":
            targets.add(name)
        elif name.startswith(("docs/", ".claude/")) or name in {"README.md", "CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md"}:
            targets.update(DOC_TESTS)
        elif name.startswith("studio/"):
            extras.append("npm test --prefix studio")
            targets.add("tests/test_studio_static.py")
        elif name.startswith(".github/"):
            targets.add("tests/test_ci_release_gate.py")
        elif name.startswith(("app/", "dispatch_center/", "agent/", "dispatch_agent/", "scripts/")) and path.suffix == ".py":
            mentioned = tests_mentioning(path)
            if not mentioned:
                return list(FULL), extras
            targets.update(mentioned)
        # anything else (data files, examples, images) selects nothing
    existing = sorted(target for target in targets if (REPOSITORY_ROOT / target).exists())
    return existing, sorted(set(extras))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="HEAD", help="git base to diff against (default HEAD = uncommitted changes)")
    parser.add_argument("--run", action="store_true", help="run the selected tests instead of printing the command")
    args = parser.parse_args(argv)
    files = changed_files(args.base)
    targets, extras = select(files)
    python = sys.executable
    if not targets and not extras:
        print("test_affected: no test-relevant changes")
        return 0
    command = [python, "-m", "pytest", "-q", *targets]
    if targets == list(FULL):
        command += ["-n", "auto"]
    for extra in extras:
        print(extra)
    print(" ".join(command))
    if not args.run:
        return 0
    status = 0
    for extra in extras:
        # `extras` holds fixed literal commands from `select()`, never user input.
        status |= subprocess.run(extra.split(), cwd=REPOSITORY_ROOT).returncode
    if targets:
        status |= subprocess.run(command, cwd=REPOSITORY_ROOT).returncode
    return status


if __name__ == "__main__":
    raise SystemExit(main())
