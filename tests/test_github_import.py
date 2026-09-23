"""DG-PROJECT-GITHUB-IMPORT-v1 (V0.2 WP3) pins: projects are created only from a
GitHub repository with a non-empty README.md, through the governed
``project_github_import`` approval that clones on the worker.

Covers the closed URL contract and pure builders (G-1/G-2), request-time
validation, the approve flow (command sequence, README fail-closed with
bounded staging cleanup, clone failure without cleanup, response-loss
reconcile), the GitHub-only policy on scanned candidates and the legacy
direct-create route, and the API request route. No network, no real SSH.
"""

from __future__ import annotations

import asyncio

import pytest

from app.approval_presentation import KIND_TITLES
from app.approvals import approve, request_import_project_approval
from app.audit import read_audit
from app.config import AppConfig, ServerConfig
from app.db import VALID_APPROVAL_KINDS
from app.github_import import (
    KIND,
    InvalidGithubImportRequestError,
    build_clone_command,
    build_move_command,
    build_readme_check_command,
    build_staging_cleanup_command,
    has_readme,
    is_github_remote,
    is_github_source_reference,
    parse_github_repo_url,
    request_github_import_approval,
    staging_path,
)

REPO = "https://github.com/acme/demo-project"
ROOT = "/home/bgab141/Howard"


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class FakeSSH:
    """Worker-side fake for both request-time probes and approve-time steps."""

    def __init__(
        self,
        *,
        dest_exists: bool = False,
        dest_empty: bool = True,
        clone_ok: bool = True,
        readme_ok: bool = True,
        move_ok: bool = True,
        head: str = "deadbeef1234567890",
        branch: str = "main",
        raise_on: str | None = None,
    ):
        self.calls: list[str] = []
        self.dest_exists = dest_exists
        self.dest_empty = dest_empty
        self.clone_ok = clone_ok
        self.readme_ok = readme_ok
        self.move_ok = move_ok
        self.head = head
        self.branch = branch
        self.raise_on = raise_on

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        if self.raise_on and self.raise_on in command:
            raise OSError("simulated response loss")
        if command.startswith("test -e"):
            return _Result(stdout="" if self.dest_exists else "NOT_EXIST\n")
        if command.startswith("ls -A"):
            return _Result(stdout="" if self.dest_empty else "somefile\n")
        if command.startswith("mkdir -p"):
            return _Result()
        if command.startswith("git clone"):
            return _Result(exit_status=0 if self.clone_ok else 128, stderr="" if self.clone_ok else "fatal: repository not found")
        if command.startswith("test -s"):
            return _Result(stdout="README_OK\n" if self.readme_ok else "README_MISSING\n")
        if command.startswith("mv "):
            return _Result(exit_status=0 if self.move_ok else 1, stderr="" if self.move_ok else "mv failed")
        if command.startswith("rm -rf"):
            return _Result()
        if command.rstrip().endswith("rev-parse HEAD"):
            return _Result(stdout=f"{self.head}\n")
        if command.rstrip().endswith("branch --show-current"):
            return _Result(stdout=f"{self.branch}\n")
        return _Result()


class _FakeAppState:
    def __init__(self, config):
        self.config = config


def _target(name="server-b", enabled=True, roots=(ROOT,)) -> ServerConfig:
    return ServerConfig(name=name, host="10.0.0.6", user="train", key="~/.ssh/id_rsa", project_roots=list(roots), enabled=enabled)


def _request(db, audit_path, *, ssh=None, repo_url=REPO, project=None, target=None, dest_path=None, ref=None, server_configs=None):
    target = target or _target()
    return asyncio.run(
        request_github_import_approval(
            db,
            repo_url=repo_url,
            project=project,
            target_server=target.name,
            dest_path=dest_path,
            ref=ref,
            server_configs=server_configs if server_configs is not None else {target.name: target},
            ssh_run=ssh or FakeSSH(),
            audit_path=audit_path,
        )
    )


def _approve(db, audit_path, approval_id, ssh, tmp_path, target=None):
    target = target or _target()
    config = AppConfig(servers=[target], local_home_dir=str(tmp_path))
    return asyncio.run(
        approve(
            db,
            approval_id,
            ssh_run=ssh,
            audit_path=audit_path,
            server_configs={target.name: target},
            app_state=_FakeAppState(config),
        )
    )


# ---------------------------------------------------------------------------
# G-1: closed URL contract + pure builders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://github.com/acme/demo", "https://github.com/acme/demo.git", "https://github.com/acme/demo/", "https://github.com/a-b/c.d_e"],
)
def test_parse_github_repo_url_accepts_canonical_forms(url):
    owner, repo = parse_github_repo_url(url)
    assert owner in {"acme", "a-b"}
    assert repo in {"demo", "c.d_e"}


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/demo",
        "https://gitlab.com/acme/demo",
        "https://user:pw@github.com/acme/demo",
        "https://github.com/acme/demo?x=1",
        "https://github.com/acme/demo#frag",
        "https://github.com/acme",
        "https://github.com/acme/demo/tree/main",
        "git@github.com:acme/demo.git",
        " https://github.com/acme/demo",
        "https://github.com/acme/de mo",
        "",
    ],
)
def test_parse_github_repo_url_rejects_non_canonical_forms(url):
    with pytest.raises(InvalidGithubImportRequestError):
        parse_github_repo_url(url)


def test_remote_and_readme_predicates():
    assert is_github_remote("https://github.com/acme/demo.git")
    assert is_github_remote("git@github.com:acme/demo.git")
    assert is_github_remote("ssh://git@github.com/acme/demo")
    assert not is_github_remote("https://gitlab.com/acme/demo.git")
    assert not is_github_remote("")
    assert not is_github_remote(None)
    assert is_github_source_reference("repo", "https://github.com/acme/demo")
    assert not is_github_source_reference("local_path", "/srv/demo")
    assert has_readme("# Demo\nWhat it does")
    assert not has_readme("   \n")
    assert not has_readme(None)


def test_builders_quote_values_and_bound_cleanup():
    staging = staging_path("/home/x/demo", 7)
    assert staging == "/home/x/demo.dispatch-import-7"
    assert build_clone_command(REPO + ".git", None, staging) == f"git clone {REPO} {staging}"
    assert build_clone_command(REPO, "feature/x", "/home/x/it's") == f"git clone -b feature/x {REPO} '/home/x/it'\"'\"'s'"
    with pytest.raises(InvalidGithubImportRequestError):
        build_clone_command(REPO, "bad ref;rm", staging)
    assert build_readme_check_command(staging) == f"test -s {staging}/README.md && echo README_OK || echo README_MISSING"
    assert build_move_command(staging, "/home/x/demo") == f"mv {staging} /home/x/demo"
    assert build_staging_cleanup_command(staging, 7) == f"rm -rf {staging}"
    with pytest.raises(ValueError):
        build_staging_cleanup_command("/home/x/demo", 7)
    with pytest.raises(ValueError):
        build_staging_cleanup_command("/home/x/demo.dispatch-import-8", 7)


def test_kind_registered_and_titled():
    assert KIND == "project_github_import"
    assert KIND in VALID_APPROVAL_KINDS
    assert KIND_TITLES[KIND] == "從 GitHub 匯入專案"


# ---------------------------------------------------------------------------
# request time
# ---------------------------------------------------------------------------


def test_request_builds_card_with_defaults_and_audit(db, audit_path):
    approval = _request(db, audit_path)
    assert approval.kind == KIND
    assert approval.status == "pending"
    assert approval.payload == {
        "repo_url": REPO,
        "owner": "acme",
        "repo": "demo-project",
        "project": "demo-project",
        "target_server": "server-b",
        "dest_path": f"{ROOT}/demo-project",
        "ref": None,
    }
    requested = [r for r in read_audit(audit_path) if r["action"] == "approval_requested"]
    assert requested and requested[-1]["params"]["kind"] == KIND
    assert db.get_project("demo-project") is None  # nothing created at request time


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"server_configs": {}}, "未知或未啟用"),
        ({"target": _target(enabled=False)}, "未知或未啟用"),
        ({"dest_path": "relative/path"}, "絕對路徑"),
        ({"dest_path": "/home/x/../etc"}, ".."),
        ({"dest_path": "/"}, "禁止"),
        ({"target": _target(roots=())}, "project_roots"),
        ({"ssh": FakeSSH(dest_exists=True, dest_empty=False)}, "非空目錄"),
        ({"ref": "bad ref"}, "ref"),
        ({"repo_url": "https://gitlab.com/acme/demo"}, "GitHub"),
    ],
)
def test_request_validation_rejects(db, audit_path, kwargs, message):
    with pytest.raises(InvalidGithubImportRequestError, match=message):
        _request(db, audit_path, **kwargs)
    assert db.list_approvals() == [] if hasattr(db, "list_approvals") else True


def test_request_rejects_existing_project(db, audit_path):
    db.insert_project("demo-project", "https://github.com/acme/demo-project")
    with pytest.raises(InvalidGithubImportRequestError, match="已存在"):
        _request(db, audit_path)


# ---------------------------------------------------------------------------
# approve time
# ---------------------------------------------------------------------------


def test_approve_clones_checks_readme_moves_and_registers_project(db, audit_path, tmp_path):
    approval = _request(db, audit_path)
    ssh = FakeSSH()
    result = _approve(db, audit_path, approval.id, ssh, tmp_path)

    approved = result["approval"]
    assert approved.status == "approved"
    assert REPO in approved.note and "deadbeef" in approved.note
    calls = ssh.calls
    staging = staging_path(f"{ROOT}/demo-project", approval.id)
    order = [
        next(i for i, c in enumerate(calls) if c.startswith("mkdir -p")),
        next(i for i, c in enumerate(calls) if c.startswith("git clone")),
        next(i for i, c in enumerate(calls) if c.startswith("test -s")),
        next(i for i, c in enumerate(calls) if c.startswith("mv ")),
        next(i for i, c in enumerate(calls) if c.rstrip().endswith("rev-parse HEAD")),
    ]
    assert order == sorted(order)
    assert f"git clone {REPO} {staging}" in calls
    assert f"mv {staging} {ROOT}/demo-project" in calls
    assert not any(c.startswith("rm -rf") for c in calls)

    project = db.get_project("demo-project")
    assert project is not None and project.repo_or_path == REPO
    instance = next(i for i in db.list_project_instances("demo-project") if i.server == "server-b")
    assert instance.path == f"{ROOT}/demo-project"
    assert instance.git_remote == REPO
    assert instance.git_branch == "main"
    assert instance.git_commit == "deadbeef1234567890"
    versions = db._conn.execute(
        "SELECT COUNT(*) FROM project_versions WHERE git_commit = ?", ("deadbeef1234567890",)
    ).fetchone()[0]
    assert versions == 1
    durable = [
        (row[0], row[1])
        for row in db._conn.execute(
            "SELECT action, result FROM audit_events WHERE approval_id = ? ORDER BY id", (approval.id,)
        ).fetchall()
    ]
    assert ("project_github_import_intent", "intent") in durable
    assert ("project_github_import_outcome", "applied") in durable


def test_approve_rejects_missing_readme_and_removes_only_staging(db, audit_path, tmp_path):
    approval = _request(db, audit_path)
    ssh = FakeSSH(readme_ok=False)
    result = _approve(db, audit_path, approval.id, ssh, tmp_path)

    rejected = result["approval"]
    assert rejected.status == "rejected"
    assert "readme_required" in rejected.note
    staging = staging_path(f"{ROOT}/demo-project", approval.id)
    assert f"rm -rf {staging}" in ssh.calls
    assert not any(c.startswith("mv ") for c in ssh.calls)
    assert db.get_project("demo-project") is None
    assert db.list_project_instances("demo-project") == []
    durable = db._conn.execute(
        "SELECT params_json, result FROM audit_events WHERE approval_id = ? AND action = 'project_github_import_outcome'",
        (approval.id,),
    ).fetchall()
    assert durable and durable[0][1] == "rejected" and "readme_required" in durable[0][0]


def test_approve_clone_failure_rejects_without_cleanup(db, audit_path, tmp_path):
    approval = _request(db, audit_path)
    ssh = FakeSSH(clone_ok=False)
    result = _approve(db, audit_path, approval.id, ssh, tmp_path)
    rejected = result["approval"]
    assert rejected.status == "rejected"
    assert "git clone" in rejected.note and "系統不會自動清理" in rejected.note
    assert not any(c.startswith("rm -rf") for c in ssh.calls)
    assert not any(c.startswith("test -s") for c in ssh.calls)
    assert db.get_project("demo-project") is None


def test_approve_response_loss_reconciles_read_only(db, audit_path, tmp_path):
    approval = _request(db, audit_path)
    with pytest.raises(OSError):
        _approve(db, audit_path, approval.id, FakeSSH(raise_on="git clone"), tmp_path)
    pending = db.get_approval(approval.id)
    assert pending.status == "pending"
    assert pending.materialization_started_at is not None
    assert "未知" in (pending.note or "")

    second = FakeSSH(head="0123456789abcdef0123")
    result = _approve(db, audit_path, approval.id, second, tmp_path)
    assert result["approval"].status == "approved"
    assert "reconcile" in result["approval"].note
    assert not any(c.startswith("git clone") for c in second.calls)
    instance = next(i for i in db.list_project_instances("demo-project") if i.server == "server-b")
    assert instance.git_commit == "0123456789abcdef0123"


def test_approve_precheck_existing_project_rejects_before_any_effect(db, audit_path, tmp_path):
    approval = _request(db, audit_path)
    db.insert_project("demo-project", "https://github.com/acme/demo-project")
    ssh = FakeSSH()
    result = _approve(db, audit_path, approval.id, ssh, tmp_path)
    assert result["approval"].status == "rejected"
    assert ssh.calls == []


# ---------------------------------------------------------------------------
# GitHub-only policy on scanned candidates (G-1)
# ---------------------------------------------------------------------------


def _candidate(db, *, remote, readme):
    return db.upsert_project_candidate(
        server="server-c", path="/home/bgab141/Howard/cand", name_guess="cand",
        git_remote=remote, readme_excerpt=readme,
    )


def test_candidate_import_requires_github_origin_and_readme(db, audit_path):
    cid = _candidate(db, remote="https://gitlab.com/acme/cand.git", readme="# cand")
    with pytest.raises(ValueError, match="github_origin_required"):
        request_import_project_approval(db, cid, {}, audit_path=audit_path, github_only=True)
    cid2 = db.upsert_project_candidate(server="server-c", path="/home/bgab141/Howard/cand2", name_guess="cand2", git_remote="git@github.com:acme/cand2.git", readme_excerpt="  ")
    with pytest.raises(ValueError, match="readme_required"):
        request_import_project_approval(db, cid2, {}, audit_path=audit_path, github_only=True)
    cid3 = db.upsert_project_candidate(server="server-c", path="/home/bgab141/Howard/cand3", name_guess="cand3", git_remote="https://github.com/acme/cand3", readme_excerpt="# cand3\nA project")
    approval = request_import_project_approval(db, cid3, {}, audit_path=audit_path, github_only=True)
    assert approval.kind == "import_project"
    # policy off: the same non-GitHub candidate is accepted (legacy behaviour retained)
    legacy = request_import_project_approval(db, cid, {}, audit_path=audit_path, github_only=False)
    assert legacy.kind == "import_project"


def test_candidate_import_is_rejected_at_decision_time_when_policy_on(db, audit_path, tmp_path):
    cid = _candidate(db, remote="https://gitlab.com/acme/cand.git", readme="# cand")
    approval = request_import_project_approval(db, cid, {}, audit_path=audit_path, github_only=False)
    config = AppConfig(servers=[], local_home_dir=str(tmp_path))
    assert config.project_github_only_enabled is True
    result = asyncio.run(approve(db, approval.id, audit_path=audit_path, app_state=_FakeAppState(config)))
    assert result["approval"].status == "rejected"
    assert "github_origin_required" in result["approval"].note
    assert db.get_project("cand") is None


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


def _enable_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True


def test_legacy_direct_create_is_refused_while_github_only(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    body = {"name": "direct", "repo_or_path": "/srv/direct"}
    refused = client.post("/api/v2/legacy-projects", json=body)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "github_project_required"
    main_module.app_state.config.project_github_only_enabled = False
    try:
        allowed = client.post("/api/v2/legacy-projects", json=body)
        assert allowed.status_code == 200
    finally:
        main_module.app_state.config.project_github_only_enabled = True


def test_github_import_request_route(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    target = _target()
    main_module.app_state.server_configs = {target.name: target}
    main_module.app_state.ssh_run = FakeSSH()

    bad = client.post("/api/v2/projects/github-import-requests", json={"repo_url": "https://gitlab.com/a/b", "target_server": "server-b"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_github_import_request"

    ok = client.post("/api/v2/projects/github-import-requests", json={"repo_url": REPO, "target_server": "server-b"})
    assert ok.status_code == 200, ok.text
    card = ok.json()
    assert card["kind"] == KIND
    assert card["status"] == "pending"
    assert card["payload"]["dest_path"] == f"{ROOT}/demo-project"
    assert ok.headers["Cache-Control"] == "no-store"
