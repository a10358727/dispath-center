"""Integration evidence for the Engineering Task v2 final-Git path policy.

These tests intentionally cross the request, approval, deterministic Runner
wrapper, and Server-A result-acceptance boundaries.  The smaller matching and
Git parser cases live in ``test_engineering_path_policy.py``; this module proves
that the independently approved policy actually reaches every enforcement
point without changing already-pending v1 tasks.
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


from app.approvals import (
    approve,
)
from app.config import AppConfig, ServerConfig
from app.db import Database


FAKE_COMMIT = "a" * 40
TASK_ID = "11111111-1111-4111-8111-111111111111"


@dataclass
class _CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0


class _HubInspection:
    def __init__(self, commit: str):
        self.commit = commit
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, command: str, timeout: int):
        self.calls.append((command, timeout))
        if "rev-parse --verify" in command:
            return _CommandResult(stdout=f"{self.commit}\n")
        if "ls-tree -r --name-only" in command:
            return _CommandResult(stdout="allowed.txt\nseed.txt\npyproject.toml\n")
        return _CommandResult()


class _RunnerProbe:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def __call__(self, server: str, command: str, timeout: int):
        self.calls.append((server, command, timeout))
        if "command -v codex" in command:
            return _CommandResult(stdout="codex-cli 0.144.3\nAUTH_OK\n")
        return _CommandResult()


class _Writes:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, server: str, path: str, content: str):
        self.calls.append((server, path, content))


def _server() -> ServerConfig:
    return ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="~/.ssh/id_rsa",
        port=32221,
        enabled=True,
    )


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        servers=[_server()],
        local_home_dir=str(tmp_path),
        codex_runner_server="server-a",
        codex_workspace_root="~/codex_workspaces",
        engineering_task_backend_v1=True,
        engineering_task_backend_v1_accept_unsandboxed_finalization=True,
    )


def _structured_request(**overrides) -> dict:
    request = {
        "objective": "Enforce the approved final Git path policy",
        "background": "The final result must be independently reviewable",
        "expected_changes": ["Change only the approved scope"],
        "non_goals": [],
        "allowed_paths": ["approved-scope/"],
        "prohibited_paths": ["approved-scope/private/"],
        "prohibited_changes": ["Do not alter deployment behavior"],
        "acceptance_criteria": ["Reject every out-of-scope final tree"],
        "validation": {
            "tests_lint": False,
            "build_smoke": False,
            "continue_fixing_failures": False,
            "worker_validation_target": None,
        },
        "permissions": {
            "modify_project_files": True,
            "install_dependencies": False,
            "external_network": False,
            "environment_references": [],
            "secret_references": [],
        },
    }
    request.update(overrides)
    return request


def _insert_fake_project(db: Database, tmp_path: Path):
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    version = db.get_or_create_project_version("proj1", FAKE_COMMIT, git_ref="main")
    (tmp_path / "git" / "proj1.git").mkdir(parents=True)
    return version




def _approve(
    db: Database,
    tmp_path: Path,
    approval_id: int,
    local: _HubInspection,
):
    writes = _Writes()
    runner = _RunnerProbe()
    result = asyncio.run(
        approve(
            db,
            approval_id,
            ssh_run=runner,
            server_configs={"server-a": _server()},
            app_state=SimpleNamespace(
                config=_config(tmp_path),
                ssh_write_file=writes,
            ),
            local_run=local,
        )
    )
    return result, writes, runner












def _git(repo: Path, *arguments: str, capture: bool = False) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip() if capture else ""


def _make_source_repo(root: Path) -> tuple[Path, str]:
    source = root / "source"
    source.mkdir(parents=True)
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "dispatch@example.invalid")
    _git(source, "config", "user.name", "Dispatch Test")
    (source / "seed.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "seed.txt")
    _git(source, "commit", "-qm", "base")
    return source, _git(source, "rev-parse", "HEAD", capture=True)


def _write_fake_codex(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            from pathlib import Path
            import subprocess
            import sys

            args = sys.argv[1:]
            if args == ["--version"]:
                print("codex-cli 0.144.3")
                raise SystemExit(0)
            if args == ["login", "status"]:
                raise SystemExit(0)
            if not args or args[0] != "exec":
                raise SystemExit(2)

            repo = Path(args[args.index("--cd") + 1])
            output = Path(args[args.index("-o") + 1])
            mode = os.environ["FAKE_CODEX_MODE"]
            if mode == "already-committed":
                (repo / "allowed.txt").write_text("allowed result\\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(repo), "add", "allowed.txt"], check=True)
                subprocess.run(
                    [
                        "git", "-C", str(repo),
                        "-c", "user.name=Fake Agent",
                        "-c", "user.email=fake@example.invalid",
                        "commit", "-qm", "agent-created commit",
                    ],
                    check=True,
                )
            elif mode == "outside-newline":
                (repo / "outside\\nfile.txt").write_text("outside\\n", encoding="utf-8")
            elif mode == "detached":
                subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
                (repo / "allowed.txt").write_text("detached\\n", encoding="utf-8")
            else:
                raise SystemExit(3)
            output.write_text("fake final response\\n", encoding="utf-8")
            print('{"type":"turn.completed"}')
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)











