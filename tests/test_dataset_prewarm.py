"""Goal 3 Phase B4：新機 dataset 預熱（DG-B4，docs/DECISIONS.md 2026-07-25）。

涵蓋草案 `docs/DG_B4_DATASET_PREWARM_DRAFT.md` 的 Verification 清單：

- 純政策：只有空 cache 機器觸發、非空 cache 不觸發、data-gravity 排序、
  同分 size 排序、磁碟不足跳過、fail-closed（探測不到空間＝不提案）、
  確定性（同輸入同輸出）；
- 提案：冷卻期內不重複、pending 去重、旗標關閉時 fail-closed、
  資料集消失/已快取時不提案；
- 核准：走既有 sync 路徑，指令字串與手動派工附帶的 sync 任務**逐位元
  一致**（golden test，比照 C1 的驗證方式）；核准當下重新驗證。
- 邊界：`dataset_prewarm` 永遠不被 `maybe_auto_approve()` 自動核准。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.approvals import (
    DatasetPrewarmDisabledError,
    approve,
    maybe_auto_approve,
    request_dataset_prewarm_approval,
)
from app.config import AppConfig, ServerConfig
from app.dataset_prewarm import PrewarmCandidate, evaluate_prewarm_candidates
from app.datasets import SPACE_SAFETY_FACTOR, build_sync_script, dataset_remote_dir
from app.db import Database, Dataset, DatasetCacheEntry, VALID_APPROVAL_KINDS


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _dataset(name: str, version: str, size_bytes: int) -> Dataset:
    return Dataset(
        name=name,
        version=version,
        size_bytes=size_bytes,
        source_path=f"/data/{name}/{version}",
        manifest={},
    )


def _insert(db, name: str, version: str, size_bytes: int) -> Dataset:
    """註冊一個資料集版本並回傳對應的 Dataset（測試要用它的 source_path
    組 golden 指令）。"""
    dataset = _dataset(name, version, size_bytes)
    db.insert_dataset(
        dataset.name,
        dataset.version,
        dataset.size_bytes,
        dataset.source_path,
        dataset.manifest,
    )
    return dataset


def _cache(server: str, dataset: str, version: str) -> DatasetCacheEntry:
    return DatasetCacheEntry(server=server, dataset=dataset, version=version)


def _plenty(*servers: str) -> dict:
    return {name: 10**15 for name in servers}


def _server_cfg(name: str, **kw) -> ServerConfig:
    return ServerConfig(
        name=name,
        host=kw.get("host", f"{name}.example"),
        user=kw.get("user", "worker"),
        key=kw.get("key", "~/.ssh/id_test"),
        port=kw.get("port", 22),
        enabled=kw.get("enabled", True),
    )


def _config(**kw) -> AppConfig:
    return AppConfig(
        servers=[],
        dataset_prewarm_v1_enabled=kw.get("enabled", True),
        dataset_prewarm_cooldown_sec=kw.get("cooldown", 3600),
        dataset_prewarm_kill_switch=kw.get("kill_switch", False),
        audit_path=kw.get("audit_path", "/dev/null"),
    )


# ---------------------------------------------------------------------------
# 1. 純政策函式
# ---------------------------------------------------------------------------


def test_kind_is_registered():
    assert "dataset_prewarm" in VALID_APPROVAL_KINDS


def test_empty_cache_server_gets_a_candidate():
    candidates = evaluate_prewarm_candidates(
        enabled_server_names=["new", "old"],
        cache_entries=[_cache("old", "mnist", "v1")],
        datasets_by_key={("mnist", "v1"): _dataset("mnist", "v1", 100)},
        disk_avail_by_server=_plenty("new", "old"),
    )
    assert [(c.server_name, c.dataset_name, c.dataset_version) for c in candidates] == [
        ("new", "mnist", "v1")
    ]
    assert candidates[0].cached_on_count == 1
    assert candidates[0].size_bytes == 100


def test_server_with_any_cache_is_never_proposed():
    """非空 cache 的機器不觸發——即使它缺了別台有的資料集。"""
    candidates = evaluate_prewarm_candidates(
        enabled_server_names=["a", "b"],
        cache_entries=[_cache("a", "mnist", "v1"), _cache("b", "cifar", "v1")],
        datasets_by_key={
            ("mnist", "v1"): _dataset("mnist", "v1", 100),
            ("cifar", "v1"): _dataset("cifar", "v1", 100),
        },
        disk_avail_by_server=_plenty("a", "b"),
    )
    assert candidates == []


def test_data_gravity_prefers_most_widely_cached_dataset():
    """被最多台快取的資料集優先，即使它比較大。"""
    candidates = evaluate_prewarm_candidates(
        enabled_server_names=["new", "x", "y", "z"],
        cache_entries=[
            _cache("x", "popular", "v1"),
            _cache("y", "popular", "v1"),
            _cache("z", "popular", "v1"),
            _cache("x", "rare", "v1"),
        ],
        datasets_by_key={
            ("popular", "v1"): _dataset("popular", "v1", 9_000),
            ("rare", "v1"): _dataset("rare", "v1", 10),
        },
        disk_avail_by_server=_plenty("new", "x", "y", "z"),
    )
    assert len(candidates) == 1
    assert candidates[0].dataset_name == "popular"
    assert candidates[0].cached_on_count == 3


def test_tie_on_gravity_breaks_to_smaller_dataset():
    candidates = evaluate_prewarm_candidates(
        enabled_server_names=["new", "x"],
        cache_entries=[_cache("x", "big", "v1"), _cache("x", "small", "v1")],
        datasets_by_key={
            ("big", "v1"): _dataset("big", "v1", 5_000),
            ("small", "v1"): _dataset("small", "v1", 5),
        },
        disk_avail_by_server=_plenty("new", "x"),
    )
    assert [c.dataset_name for c in candidates] == ["small"]


def test_tie_on_gravity_and_size_breaks_deterministically_by_name():
    """同 gravity 同 size：字典序決定，保證同輸入同輸出。"""
    entries = [_cache("x", "bbb", "v1"), _cache("x", "aaa", "v1")]
    datasets = {
        ("bbb", "v1"): _dataset("bbb", "v1", 100),
        ("aaa", "v1"): _dataset("aaa", "v1", 100),
    }
    results = {
        evaluate_prewarm_candidates(
            enabled_server_names=["new", "x"],
            cache_entries=list(entries),
            datasets_by_key=dict(datasets),
            disk_avail_by_server=_plenty("new", "x"),
        )[0].dataset_name
        for _ in range(20)
    }
    assert results == {"aaa"}


def test_insufficient_disk_skips_that_dataset():
    size = 1_000
    candidates = evaluate_prewarm_candidates(
        enabled_server_names=["new", "x"],
        cache_entries=[_cache("x", "mnist", "v1")],
        datasets_by_key={("mnist", "v1"): _dataset("mnist", "v1", size)},
        # 剛好差一個 byte 不到 size × factor。
        disk_avail_by_server={"new": int(size * SPACE_SAFETY_FACTOR) - 1, "x": 10**15},
    )
    assert candidates == []


def test_unknown_disk_is_fail_closed():
    """探測不到空間（離線／還沒探測）一律不提案。"""
    candidates = evaluate_prewarm_candidates(
        enabled_server_names=["new", "x"],
        cache_entries=[_cache("x", "mnist", "v1")],
        datasets_by_key={("mnist", "v1"): _dataset("mnist", "v1", 1)},
        disk_avail_by_server={"new": None, "x": 10**15},
    )
    assert candidates == []
    # 連 key 都沒有時同樣不提案。
    assert (
        evaluate_prewarm_candidates(
            enabled_server_names=["new", "x"],
            cache_entries=[_cache("x", "mnist", "v1")],
            datasets_by_key={("mnist", "v1"): _dataset("mnist", "v1", 1)},
            disk_avail_by_server={"x": 10**15},
        )
        == []
    )


def test_disabled_servers_are_neither_targets_nor_gravity_evidence():
    """只有 enabled 機器算 data gravity；停用機器不會成為提案目標。"""
    candidates = evaluate_prewarm_candidates(
        enabled_server_names=["new"],  # "off" 不在 enabled 名單
        cache_entries=[_cache("off", "mnist", "v1")],
        datasets_by_key={("mnist", "v1"): _dataset("mnist", "v1", 1)},
        disk_avail_by_server=_plenty("new", "off"),
    )
    assert candidates == []


def test_dataset_missing_from_registry_is_skipped():
    """快取表指向已刪除的版本時不提案（不臆造大小/來源）。"""
    candidates = evaluate_prewarm_candidates(
        enabled_server_names=["new", "x"],
        cache_entries=[_cache("x", "ghost", "v1")],
        datasets_by_key={},
        disk_avail_by_server=_plenty("new", "x"),
    )
    assert candidates == []


def test_at_most_one_candidate_per_server_per_tick():
    candidates = evaluate_prewarm_candidates(
        enabled_server_names=["new", "x"],
        cache_entries=[
            _cache("x", "a", "v1"),
            _cache("x", "b", "v1"),
            _cache("x", "c", "v1"),
        ],
        datasets_by_key={
            ("a", "v1"): _dataset("a", "v1", 10),
            ("b", "v1"): _dataset("b", "v1", 20),
            ("c", "v1"): _dataset("c", "v1", 30),
        },
        disk_avail_by_server=_plenty("new", "x"),
    )
    assert len(candidates) == 1
    assert [c.server_name for c in candidates] == ["new"]


# ---------------------------------------------------------------------------
# 2. 提案建立（去重、冷卻、旗標）
# ---------------------------------------------------------------------------


def _candidate(server="new", name="mnist", version="v1", size=100, count=1):
    return PrewarmCandidate(
        server_name=server,
        dataset_name=name,
        dataset_version=version,
        size_bytes=size,
        cached_on_count=count,
    )


def test_request_creates_pending_approval_with_evidence(db):
    _insert(db, "mnist", "v1", 100)
    approval = request_dataset_prewarm_approval(
        db, candidate=_candidate(count=3), config=_config()
    )
    assert approval is not None
    assert approval.kind == "dataset_prewarm"
    assert approval.status == "pending"
    assert approval.payload == {
        "server": "new",
        "dataset": "mnist",
        "version": "v1",
        "size_bytes": 100,
        "cached_on_count": 3,
    }


def test_request_is_fail_closed_when_flag_disabled(db):
    _insert(db, "mnist", "v1", 100)
    with pytest.raises(DatasetPrewarmDisabledError):
        request_dataset_prewarm_approval(
            db, candidate=_candidate(), config=_config(enabled=False)
        )
    assert db.list_approvals(kind="dataset_prewarm") == []


def test_request_skips_when_dataset_no_longer_exists(db):
    assert (
        request_dataset_prewarm_approval(db, candidate=_candidate(), config=_config())
        is None
    )
    assert db.list_approvals(kind="dataset_prewarm") == []


def test_request_skips_when_already_cached(db):
    _insert(db, "mnist", "v1", 100)
    db.upsert_dataset_cache("new", "mnist", "v1")
    assert (
        request_dataset_prewarm_approval(db, candidate=_candidate(), config=_config())
        is None
    )


def test_request_dedupes_existing_pending(db):
    _insert(db, "mnist", "v1", 100)
    first = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=_config()
    )
    second = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=_config()
    )
    assert first is not None and second is None
    assert len(db.list_approvals(kind="dataset_prewarm")) == 1


def test_request_respects_cooldown_after_rejection(db):
    """使用者拒絕之後，冷卻期內不重提；冷卻過了才會再提一次。"""
    _insert(db, "mnist", "v1", 100)
    first = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=_config()
    )
    db.update_approval(first.id, status="rejected", decided_at="2026-01-01T00:00:00")

    assert (
        request_dataset_prewarm_approval(db, candidate=_candidate(), config=_config())
        is None
    )

    # 把建立時間往回推到冷卻視窗之外。
    old = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    with db.cursor() as cur:
        cur.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (old, first.id))
    assert (
        request_dataset_prewarm_approval(db, candidate=_candidate(), config=_config())
        is not None
    )


def test_request_cooldown_is_scoped_to_the_triple(db):
    """不同 (server, dataset, version) 互不影響冷卻。"""
    _insert(db, "mnist", "v1", 100)
    _insert(db, "cifar", "v1", 100)
    assert request_dataset_prewarm_approval(
        db, candidate=_candidate(name="mnist"), config=_config()
    )
    assert request_dataset_prewarm_approval(
        db, candidate=_candidate(name="cifar"), config=_config()
    )
    assert request_dataset_prewarm_approval(
        db, candidate=_candidate(server="other", name="mnist"), config=_config()
    )


# ---------------------------------------------------------------------------
# 3. 核准：走既有 sync 路徑（golden test）
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, config):
        self.config = config


def _approve(db, approval_id, server_configs, config):
    return asyncio.run(
        approve(
            db,
            approval_id,
            server_configs=server_configs,
            app_state=_State(config),
            audit_path="/dev/null",
        )
    )


def test_approve_creates_sync_job_byte_identical_to_manual_path(db):
    """核准產生的 sync 指令必須與手動派工附帶的 sync 任務逐位元一致——
    重用同一組 dataset_remote_dir()/build_sync_script()，不另造邏輯。"""
    dataset = _insert(db, "mnist", "v1", 100)
    target = _server_cfg("new", host="10.0.0.9", user="ml", key="~/.ssh/k", port=2222)
    config = _config()

    approval = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=config
    )
    result = _approve(db, approval.id, {"new": target}, config)

    assert result["approval"].status == "approved"
    job = result["job"]
    assert job.type == "sync"
    assert job.pin_server == "_local"
    assert job.target_server == "new"
    assert job.dataset_name == "mnist"
    assert job.dataset_version == "v1"

    expected = build_sync_script(
        dataset.source_path,
        target.user,
        target.host,
        dataset_remote_dir("mnist", "v1"),
        target.key_path,
        port=target.port,
    )
    assert job.command == expected


def test_approve_is_fail_closed_when_flag_disabled(db):
    _insert(db, "mnist", "v1", 100)
    approval = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=_config()
    )
    with pytest.raises(DatasetPrewarmDisabledError):
        _approve(db, approval.id, {"new": _server_cfg("new")}, _config(enabled=False))
    assert db.get_approval(approval.id).status == "pending"
    assert db.list_jobs() == []


def test_approve_revalidates_dataset_still_exists(db):
    _insert(db, "mnist", "v1", 100)
    config = _config()
    approval = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=config
    )
    with db.cursor() as cur:
        cur.execute("DELETE FROM datasets WHERE name = 'mnist'")

    result = _approve(db, approval.id, {"new": _server_cfg("new")}, config)
    assert result["approval"].status == "rejected"
    assert "已不存在" in result["approval"].note
    assert db.list_jobs() == []


def test_approve_revalidates_target_still_enabled(db):
    _insert(db, "mnist", "v1", 100)
    config = _config()
    approval = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=config
    )
    result = _approve(
        db, approval.id, {"new": _server_cfg("new", enabled=False)}, config
    )
    assert result["approval"].status == "rejected"
    assert "已停用" in result["approval"].note
    assert db.list_jobs() == []


def test_approve_rejects_when_target_config_vanished(db):
    _insert(db, "mnist", "v1", 100)
    config = _config()
    approval = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=config
    )
    result = _approve(db, approval.id, {}, config)
    assert result["approval"].status == "rejected"
    assert db.list_jobs() == []


def test_approve_rejects_when_already_synced_meanwhile(db):
    """提案排隊期間資料已經到位——核准變成 no-op 拒絕，不建重複 sync。"""
    _insert(db, "mnist", "v1", 100)
    config = _config()
    approval = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=config
    )
    db.upsert_dataset_cache("new", "mnist", "v1")
    result = _approve(db, approval.id, {"new": _server_cfg("new")}, config)
    assert result["approval"].status == "rejected"
    assert "不需要預熱" in result["approval"].note
    assert db.list_jobs() == []


# ---------------------------------------------------------------------------
# 4. 邊界：永不自動核准
# ---------------------------------------------------------------------------


def test_dataset_prewarm_is_never_auto_approved(db):
    """INV-APPROVAL-4：自動核准白名單恰好 enqueue|stop，不因 B4 擴大。"""
    _insert(db, "mnist", "v1", 100)
    approval = request_dataset_prewarm_approval(
        db, candidate=_candidate(), config=_config()
    )
    decided = asyncio.run(
        maybe_auto_approve(
            db,
            approval,
            #: 刻意餵一條「什麼都放行」的規則——即使規則本身無條件同意，
            #: kind 閘門仍必須擋下 dataset_prewarm。
            source="api",
            rules=[{"kind": "any", "action": "approve"}],
            server_configs={"new": _server_cfg("new")},
            app_state=_State(_config()),
            audit_path="/dev/null",
        )
    )
    assert decided is None
    assert db.get_approval(approval.id).status == "pending"
    assert db.list_jobs() == []


# ---------------------------------------------------------------------------
# 5. AppState tick / loop gating
# ---------------------------------------------------------------------------


def _wire_state(app_state, *, enabled=True, kill_switch=False):
    """把 app_state 佈置成「worker-a 有 mnist、worker-new 空的」的現況。"""
    from app.monitor import ServerState

    app_state.config.dataset_prewarm_v1_enabled = enabled
    app_state.config.dataset_prewarm_kill_switch = kill_switch
    app_state.config.dataset_prewarm_interval_sec = 0

    _insert(app_state.db, "mnist", "v1", 100)
    app_state.db.upsert_dataset_cache("worker-a", "mnist", "v1")
    for name in ("worker-a", "worker-new"):
        app_state.server_configs[name] = _server_cfg(name)
        app_state.server_states[name] = ServerState(
            name=name, online=True, disk_avail_bytes=10**15
        )
    return app_state


def test_prewarm_tick_creates_pending_approval(api_client):
    _client, main_module = api_client
    app_state = _wire_state(main_module.app_state)

    app_state._dataset_prewarm_tick()

    pending = app_state.db.list_approvals(status="pending", kind="dataset_prewarm")
    assert len(pending) == 1
    assert pending[0].payload["server"] == "worker-new"
    assert pending[0].payload["dataset"] == "mnist"
    #: 提案 ≠ 執行：這一輪不建立任何 job。
    assert app_state.db.list_jobs() == []


def test_prewarm_tick_is_idempotent_across_ticks(api_client):
    _client, main_module = api_client
    app_state = _wire_state(main_module.app_state)

    app_state._dataset_prewarm_tick()
    app_state._dataset_prewarm_tick()
    app_state._dataset_prewarm_tick()

    assert len(app_state.db.list_approvals(kind="dataset_prewarm")) == 1


def test_prewarm_tick_never_ssh(api_client, monkeypatch):
    """提案這一輪完全不連線（磁碟餘量取 monitor 已探測到的快取值）。"""
    _client, main_module = api_client
    app_state = _wire_state(main_module.app_state)

    async def _boom(*args, **kwargs):
        raise AssertionError("dataset_prewarm tick must not SSH")

    monkeypatch.setattr(app_state, "ssh_run", _boom)
    monkeypatch.setattr(app_state, "ssh_write_file", _boom)

    app_state._dataset_prewarm_tick()
    assert len(app_state.db.list_approvals(kind="dataset_prewarm")) == 1


def test_prewarm_tick_skips_machine_with_unknown_disk(api_client):
    _client, main_module = api_client
    app_state = _wire_state(main_module.app_state)
    app_state.server_states["worker-new"].disk_avail_bytes = None

    app_state._dataset_prewarm_tick()
    assert app_state.db.list_approvals(kind="dataset_prewarm") == []


def test_prewarm_tick_single_candidate_failure_does_not_stop_others(
    api_client, monkeypatch
):
    """單一候選提案失敗只記警告，不擋其他候選（best-effort 慣例）。"""
    _client, main_module = api_client
    app_state = _wire_state(main_module.app_state)
    from app.monitor import ServerState

    for name in ("worker-new2",):
        app_state.server_configs[name] = _server_cfg(name)
        app_state.server_states[name] = ServerState(
            name=name, online=True, disk_avail_bytes=10**15
        )

    calls = {"n": 0}
    real = main_module.request_dataset_prewarm_approval

    def _flaky(db, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated proposal failure")
        return real(db, **kwargs)

    monkeypatch.setattr(main_module, "request_dataset_prewarm_approval", _flaky)

    app_state._dataset_prewarm_tick()

    assert calls["n"] == 2
    assert len(app_state.db.list_approvals(kind="dataset_prewarm")) == 1


@pytest.mark.parametrize(
    "enabled,kill_switch",
    [(False, False), (True, True), (False, True)],
    ids=["flag-off", "kill-switch-on", "both-off"],
)
def test_prewarm_loop_is_noop_unless_both_brakes_released(
    api_client, enabled, kill_switch
):
    """兩把煞車任一沒撥開，整輪都不得建立任何提案。"""
    _client, main_module = api_client
    app_state = _wire_state(
        main_module.app_state, enabled=enabled, kill_switch=kill_switch
    )

    async def _one_pass():
        task = asyncio.ensure_future(app_state.dataset_prewarm_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_one_pass())
    assert app_state.db.list_approvals(kind="dataset_prewarm") == []


def test_prewarm_loop_proposes_when_both_brakes_released(api_client):
    _client, main_module = api_client
    app_state = _wire_state(main_module.app_state, enabled=True, kill_switch=False)

    async def _one_pass():
        task = asyncio.ensure_future(app_state.dataset_prewarm_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_one_pass())
    assert len(app_state.db.list_approvals(kind="dataset_prewarm")) == 1


def test_prewarm_loop_survives_tick_failure(api_client, monkeypatch):
    """單輪失敗只記警告，迴圈不死（不影響 monitor/scheduler）。"""
    _client, main_module = api_client
    app_state = _wire_state(main_module.app_state)

    def _raise():
        raise RuntimeError("simulated tick failure")

    monkeypatch.setattr(app_state, "_dataset_prewarm_tick", _raise)

    async def _one_pass():
        task = asyncio.ensure_future(app_state.dataset_prewarm_loop())
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive

    assert asyncio.run(_one_pass()) is True
