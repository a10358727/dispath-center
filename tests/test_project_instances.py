"""PLAN.md 2026-07-11 版 §14 切片 2:instance reconciliation——唯讀探測 →
available/missing/dirty/diverged/unknown 狀態機(app/project_instances.py)。

邊界(對應 INV-PROJECT-3 草案/INV-SSH-7 精神):離線或探測失敗＝unknown,
不是 missing、不改 git 快照、不推進 last_seen;missing 列不刪;判定是
純函式,全部用假 ssh_run 測,不碰真機。async 主函式用 `asyncio.run()`
呼叫(同 tests/test_hub.py 的既有慣例,不依賴 pytest-asyncio)。"""

import asyncio
from dataclasses import dataclass

import pytest

from app.project_instances import (
    build_instance_probe_command,
    derive_instance_state,
    parse_instance_probe_output,
    reconcile_all_instances,
)


# ---------------------------------------------------------------------------
# 純函式:build / parse / derive
# ---------------------------------------------------------------------------


def test_build_instance_probe_command_quotes_path_and_is_read_only():
    cmd = build_instance_probe_command("/work/my proj")
    assert "'/work/my proj'" in cmd  # shlex.quote
    assert "EXISTS:" in cmd and "COMMIT:" in cmd and "DIRTY:" in cmd
    # 唯讀封閉指令集:不含任何寫入/刪除動詞(`2>/dev/null` 的 stderr 重導
    # 是容錯,不算寫入,不列入禁詞)。
    for forbidden in ("rm ", "git add", "git commit", "git push", "git pull", "touch "):
        assert forbidden not in cmd


def test_parse_probe_output_existing_clean_git_repo():
    text = "EXISTS:1\nBRANCH:main\nCOMMIT:abc123\nDIRTY:0\n"
    assert parse_instance_probe_output(text) == {
        "exists": True,
        "branch": "main",
        "commit": "abc123",
        "dirty": False,
    }


def test_parse_probe_output_missing_directory():
    probe = parse_instance_probe_output("EXISTS:0\n")
    assert probe["exists"] is False


def test_parse_probe_output_non_git_directory():
    """非 git 目錄:BRANCH/COMMIT 空值 → None,DIRTY:0。"""
    probe = parse_instance_probe_output("EXISTS:1\nBRANCH:\nCOMMIT:\nDIRTY:0\n")
    assert probe == {"exists": True, "branch": None, "commit": None, "dirty": False}


def test_parse_probe_output_without_marker_returns_none():
    """輸出裡完全沒有 EXISTS: 標記(指令沒跑成)→ None,呼叫端視同失敗。"""
    assert parse_instance_probe_output("") is None
    assert parse_instance_probe_output("garbage\n") is None


def test_derive_state_priority_order():
    # 探測失敗 → unknown
    assert derive_instance_state(None, hub_head="abc") == "unknown"
    # 不存在 → missing(優先於其他一切)
    assert (
        derive_instance_state(
            {"exists": False, "branch": None, "commit": None, "dirty": False},
            hub_head="abc",
        )
        == "missing"
    )
    # 髒 → dirty(優先於 diverged)
    assert (
        derive_instance_state(
            {"exists": True, "branch": "main", "commit": "xyz", "dirty": True},
            hub_head="abc",
        )
        == "dirty"
    )
    # 乾淨但 commit != hub HEAD → diverged
    assert (
        derive_instance_state(
            {"exists": True, "branch": "main", "commit": "xyz", "dirty": False},
            hub_head="abc",
        )
        == "diverged"
    )
    # commit 等於 hub HEAD → available
    assert (
        derive_instance_state(
            {"exists": True, "branch": "main", "commit": "abc", "dirty": False},
            hub_head="abc",
        )
        == "available"
    )


def test_derive_state_without_hub_or_git_never_diverged():
    # 專案沒有 hub:沒有比較基準,不宣稱漂移。
    probe = {"exists": True, "branch": "main", "commit": "xyz", "dirty": False}
    assert derive_instance_state(probe, hub_head=None) == "available"
    # instance 不是 git repo(commit None):是「未 git 化」,不是 diverged。
    non_git = {"exists": True, "branch": None, "commit": None, "dirty": False}
    assert derive_instance_state(non_git, hub_head="abc") == "available"


# ---------------------------------------------------------------------------
# reconcile_all_instances:假 ssh_run 端到端
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    stdout: str


def _make_fake_ssh(outputs: dict, *, fail_servers: frozenset = frozenset()):
    """依 server 名稱回固定 stdout;`fail_servers` 內的機器拋例外。"""

    async def ssh_run(server: str, command: str, timeout: int):
        if server in fail_servers:
            raise ConnectionError("unreachable")
        return _FakeResult(stdout=outputs[server])

    return ssh_run


async def _hub_head_abc(project_name: str):
    return "abc"


async def _hub_head_none(project_name: str):
    return None


def test_reconcile_updates_states_and_reports_changes(db):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project_instance(project_name="proj1", server="s-ok", path="/w/p1")
    db.insert_project_instance(project_name="proj1", server="s-gone", path="/w/p1")

    outputs = {
        "s-ok": "EXISTS:1\nBRANCH:main\nCOMMIT:abc\nDIRTY:0\n",
        "s-gone": "EXISTS:0\n",
    }
    changes = asyncio.run(
        reconcile_all_instances(
            db, _make_fake_ssh(outputs), lambda s: True, _hub_head_abc
        )
    )

    by_server = {i.server: i for i in db.list_project_instances("proj1")}
    assert by_server["s-ok"].state == "available"
    assert by_server["s-ok"].git_commit == "abc"
    assert by_server["s-gone"].state == "missing"
    # missing 列不刪(還在 DB 裡)。
    assert len(by_server) == 2
    # 兩台都從 unknown 轉出去 → 兩筆變化。
    assert {(c["server"], c["to"]) for c in changes} == {
        ("s-ok", "available"),
        ("s-gone", "missing"),
    }


def test_reconcile_offline_server_is_unknown_and_preserves_snapshot(db):
    """離線機器:不發 SSH、state=unknown,git 快照與 last_seen 保留
    「最後一次確實觀察到」的值(INV-SSH-7:連不上不下判定)。"""
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project_instance(
        project_name="proj1",
        server="s-off",
        path="/w/p1",
        git_commit="oldcommit",
        git_branch="main",
    )
    before = db.list_project_instances("proj1")[0]

    called = []

    async def ssh_run(server, command, timeout):
        called.append(server)
        raise AssertionError("離線機器不該發 SSH")

    changes = asyncio.run(
        reconcile_all_instances(db, ssh_run, lambda s: False, _hub_head_abc)
    )

    after = db.list_project_instances("proj1")[0]
    assert called == []
    assert after.state == "unknown"
    assert after.git_commit == "oldcommit"
    assert after.last_seen == before.last_seen
    assert changes == []  # unknown → unknown,沒有變化


def test_reconcile_ssh_failure_is_unknown_not_missing(db):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project_instance(
        project_name="proj1", server="s-flaky", path="/w/p1", git_commit="keep"
    )

    changes = asyncio.run(
        reconcile_all_instances(
            db,
            _make_fake_ssh({}, fail_servers=frozenset({"s-flaky"})),
            lambda s: True,
            _hub_head_abc,
        )
    )
    inst = db.list_project_instances("proj1")[0]
    assert inst.state == "unknown"
    assert inst.git_commit == "keep"
    assert changes == []


def test_reconcile_dirty_and_diverged_and_last_seen_advances(db):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project_instance(project_name="proj1", server="s-dirty", path="/w/a")
    db.insert_project_instance(project_name="proj1", server="s-div", path="/w/b")

    outputs = {
        "s-dirty": "EXISTS:1\nBRANCH:main\nCOMMIT:abc\nDIRTY:1\n",
        "s-div": "EXISTS:1\nBRANCH:main\nCOMMIT:other\nDIRTY:0\n",
    }
    asyncio.run(
        reconcile_all_instances(
            db, _make_fake_ssh(outputs), lambda s: True, _hub_head_abc
        )
    )

    by_server = {i.server: i for i in db.list_project_instances("proj1")}
    assert by_server["s-dirty"].state == "dirty"
    assert by_server["s-dirty"].dirty is True
    assert by_server["s-div"].state == "diverged"
    assert by_server["s-dirty"].last_seen  # 觀察到了,last_seen 有值


def test_reconcile_single_instance_failure_does_not_block_others(db):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project_instance(project_name="proj1", server="s-bad", path="/w/a")
    db.insert_project_instance(project_name="proj1", server="s-good", path="/w/b")

    outputs = {"s-good": "EXISTS:1\nBRANCH:main\nCOMMIT:abc\nDIRTY:0\n"}
    asyncio.run(
        reconcile_all_instances(
            db,
            _make_fake_ssh(outputs, fail_servers=frozenset({"s-bad"})),
            lambda s: True,
            _hub_head_none,
        )
    )
    by_server = {i.server: i for i in db.list_project_instances("proj1")}
    assert by_server["s-bad"].state == "unknown"
    assert by_server["s-good"].state == "available"


def test_update_instance_reconcile_rejects_invalid_state(db):
    db.insert_project("proj1", "/repo/proj1")
    iid = db.insert_project_instance(project_name="proj1", server="s1", path="/w/p1")
    with pytest.raises(ValueError):
        db.update_instance_reconcile(iid, state="totally-bogus")
