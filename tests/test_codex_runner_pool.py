"""Goal 3 Phase D-1 Codex Runner pool tests（docs/GOAL_3_FUTURE_WORK_PLAN.md）。

契約：`CODEX_RUNNER_SERVERS` pool 與既有單 `CODEX_RUNNER_SERVER` 配置
**完全相容**（單 Runner 行為逐位元不變）；`pick_job()` 的 Runner 身分由
名稱相等一般化為 pool 成員；每台 Runner 各自承擔 `codex_max_concurrency`；
`pick_codex_runner()` 決定性選擇（最少 active、平手取 pool 順序）；既有
reservation 語意適用於每個 pool 成員。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.approvals import select_codex_runner
from app.config import AppConfig, apply_codex_config_rules
from app.db import Job
from app.scheduler import pick_codex_runner, pick_job


def _job(
    job_id: int,
    *,
    type: str = "adhoc",
    pin_server=None,
    require_tag=None,
    priority: str = "normal",
    created_at: str = "2026-07-19T00:00:00+00:00",
) -> Job:
    return Job(
        id=job_id,
        command="echo hi",
        type=type,
        project=None,
        require_tag=require_tag,
        pin_server=pin_server,
        depends_on=[],
        gpus_needed=None,
        priority=priority,
        status="queued",
        server=None,
        created_at=created_at,
        started_at=None,
        finished_at=None,
    )


# ---------------------------------------------------------------------------
# config 正規化
# ---------------------------------------------------------------------------


def _cfg(**kwargs) -> AppConfig:
    return AppConfig(servers=[], **kwargs)


def test_single_server_config_normalizes_to_singleton_pool():
    config = _cfg(codex_runner_server="runner-a")
    warnings = apply_codex_config_rules(config, {"runner-a": True})
    assert warnings == []
    assert config.codex_runner_servers == ("runner-a",)
    assert config.codex_runner_server == "runner-a"


def test_pool_only_config_derives_primary_from_first_member():
    config = _cfg(codex_runner_servers=("runner-b", "runner-a", "runner-b"))
    apply_codex_config_rules(config, {"runner-a": True, "runner-b": True})
    assert config.codex_runner_servers == ("runner-b", "runner-a")  # 去重保序
    assert config.codex_runner_server == "runner-b"


def test_both_set_requires_primary_membership():
    config = _cfg(
        codex_runner_server="runner-a",
        codex_runner_servers=("runner-a", "runner-b"),
    )
    apply_codex_config_rules(config, {"runner-a": True, "runner-b": True})
    assert config.codex_runner_server == "runner-a"

    config = _cfg(
        codex_runner_server="runner-x",
        codex_runner_servers=("runner-a", "runner-b"),
    )
    with pytest.raises(ValueError):
        apply_codex_config_rules(config, {"runner-a": True, "runner-b": True})


def test_every_pool_member_must_exist_and_be_enabled():
    config = _cfg(codex_runner_servers=("runner-a", "ghost"))
    with pytest.raises(ValueError):
        apply_codex_config_rules(config, {"runner-a": True})

    config = _cfg(codex_runner_servers=("runner-a", "runner-b"))
    with pytest.raises(ValueError):
        apply_codex_config_rules(config, {"runner-a": True, "runner-b": False})


def test_unset_everything_keeps_codex_disabled():
    config = _cfg()
    assert apply_codex_config_rules(config, {}) == []
    assert config.codex_runner_server is None
    assert config.codex_runner_servers == ()


# ---------------------------------------------------------------------------
# pick_job：pool 成員資格
# ---------------------------------------------------------------------------


def test_coding_job_eligible_on_any_pool_member():
    pool = ("runner-a", "runner-b")
    coding = _job(1, type="coding", pin_server="runner-b")
    picked = pick_job(
        "runner-b", [], [coding],
        codex_runner_server="runner-a",
        codex_runner_servers=pool,
    )
    assert picked is coding
    # 非 pool 成員永遠不合格。
    assert pick_job(
        "worker-c", [], [coding],
        codex_runner_server="runner-a",
        codex_runner_servers=pool,
    ) is None


def test_single_runner_behavior_unchanged_without_pool_argument():
    coding = _job(1, type="coding")
    assert pick_job("runner-a", [], [coding], codex_runner_server="runner-a") is coding
    assert pick_job("worker-b", [], [coding], codex_runner_server="runner-a") is None
    assert pick_job("runner-a", [], [coding]) is None  # Codex 停用


def test_per_runner_concurrency_is_enforced_per_server():
    pool = ("runner-a", "runner-b")
    coding = _job(1, type="coding", pin_server="runner-b")
    # runner-b 這台已達上限 → 不合格；上限是逐台的，不是全域的。
    assert pick_job(
        "runner-b", [], [coding],
        codex_runner_servers=pool,
        codex_max_concurrency=1,
        running_coding_count=1,
    ) is None
    assert pick_job(
        "runner-b", [], [coding],
        codex_runner_servers=pool,
        codex_max_concurrency=1,
        running_coding_count=0,
    ) is coding


def test_reservation_applies_to_every_pool_member():
    pool = ("runner-a", "runner-b")
    ordinary = _job(2)
    for member in pool:
        assert pick_job(
            member, [], [ordinary],
            codex_runner_servers=pool,
            codex_runner_reserve=True,
        ) is None
    # 明確 pin 到成員的普通任務仍可派。
    pinned = _job(3, pin_server="runner-b")
    assert pick_job(
        "runner-b", [], [pinned],
        codex_runner_servers=pool,
        codex_runner_reserve=True,
    ) is pinned
    # 非成員機器完全不受 reservation 影響。
    assert pick_job(
        "worker-c", [], [ordinary],
        codex_runner_servers=pool,
        codex_runner_reserve=True,
    ) is ordinary


# ---------------------------------------------------------------------------
# pick_codex_runner / select_codex_runner
# ---------------------------------------------------------------------------


def test_pick_codex_runner_is_deterministic_least_loaded():
    pool = ("runner-a", "runner-b", "runner-c")
    assert pick_codex_runner(pool, {}) == "runner-a"  # 全空 → pool 順序
    assert pick_codex_runner(pool, {"runner-a": 2, "runner-b": 1}) == "runner-c"
    assert pick_codex_runner(pool, {"runner-a": 1, "runner-b": 1, "runner-c": 1}) == "runner-a"
    assert pick_codex_runner((), {}) is None


def test_select_codex_runner_single_config_returns_primary_without_db():
    config = SimpleNamespace(codex_runner_server="runner-a", codex_runner_servers=())
    assert select_codex_runner(None, config) == "runner-a"
    config = SimpleNamespace(
        codex_runner_server="runner-a", codex_runner_servers=("runner-a",)
    )
    assert select_codex_runner(None, config) == "runner-a"
    config = SimpleNamespace(codex_runner_server=None, codex_runner_servers=())
    assert select_codex_runner(None, config) is None


def test_select_codex_runner_multi_pool_uses_active_counts(db):
    # runner-a：一個 queued（pin）＋一個 running；runner-b：一個 running。
    db.insert_job("echo 1", type="coding", pin_server="runner-a")
    running_a = db.insert_job("echo 2", type="coding", pin_server="runner-a")
    db.update_job(running_a, status="running", server="runner-a")
    running_b = db.insert_job("echo 3", type="coding", pin_server="runner-b")
    db.update_job(running_b, status="running", server="runner-b")
    done = db.insert_job("echo 4", type="coding", pin_server="runner-b")
    db.update_job(done, status="done", server="runner-b")  # 終態不計入

    assert db.count_active_coding_jobs_by_server() == {"runner-a": 2, "runner-b": 1}

    config = SimpleNamespace(
        codex_runner_server="runner-a",
        codex_runner_servers=("runner-a", "runner-b"),
    )
    assert select_codex_runner(db, config) == "runner-b"
