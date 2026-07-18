"""Goal 2 Slice 2（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：確定性閒置摘要。

`app/capacity.py` 純函式表格測試 + `GET /servers/idle-summary` API shape 測試。
"""

from datetime import datetime, timedelta, timezone

from app.capacity import IdleSummary, percentile, summarize_observations
from app.config import ServerConfig
from app.db import ServerObservation


# ---------------------------------------------------------------------------
# percentile()
# ---------------------------------------------------------------------------


def test_percentile_empty_list_is_none():
    assert percentile([], 50) is None


def test_percentile_single_value():
    assert percentile([42.0], 50) == 42.0
    assert percentile([42.0], 95) == 42.0


def test_percentile_two_values_median_is_midpoint():
    assert percentile([0.0, 10.0], 50) == 5.0


def test_percentile_interpolation():
    # 排序後 [0, 10, 20, 30]，p50 的 rank = 0.5*3 = 1.5 -> 10 + 0.5*(20-10) = 15
    values = [0.0, 10.0, 20.0, 30.0]
    assert percentile(values, 50) == 15.0


def test_percentile_out_of_order_input_is_sorted_internally():
    values = [30.0, 0.0, 20.0, 10.0]
    assert percentile(values, 50) == 15.0
    assert percentile(values, 0) == 0.0
    assert percentile(values, 100) == 30.0


# ---------------------------------------------------------------------------
# summarize_observations()：以輔助函式組出「最新在前」的觀測列
# ---------------------------------------------------------------------------


def _obs(
    *,
    id_,
    observed_at,
    online=True,
    probe_ok=True,
    gpu_util_max=None,
    load1=None,
) -> ServerObservation:
    return ServerObservation(
        id=id_,
        server_name="worker-a",
        observed_at=observed_at,
        online=online,
        probe_ok=probe_ok,
        gpu_count=1 if gpu_util_max is not None else None,
        gpu_util_max=gpu_util_max,
        gpu_mem_used_mb=None,
        gpu_mem_total_mb=None,
        load1=load1,
        mem_total_bytes=None,
        mem_available_bytes=None,
        disk_avail_bytes=None,
    )


_NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: int) -> str:
    return (_NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_summarize_no_samples_is_unknown_with_all_none_fields():
    summary = summarize_observations(
        "worker-a",
        [],
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    assert summary.status == "unknown"
    assert summary.sample_count == 0
    assert summary.online_ratio is None
    assert summary.gpu_util_p50 is None
    assert summary.gpu_util_p95 is None
    assert summary.load1_p50 is None
    assert summary.load1_p95 is None
    assert summary.continuous_idle_seconds is None
    assert summary.freshness_seconds is None


def test_summarize_single_idle_sample_has_zero_length_streak():
    observations = [_obs(id_=1, observed_at=_iso(0), gpu_util_max=5.0)]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    assert summary.status == "ok"
    assert summary.sample_count == 1
    assert summary.continuous_idle_seconds == 0
    assert summary.freshness_seconds == 0


def test_summarize_consecutive_idle_streak_duration():
    # 最新在前：0 分鐘前、10 分鐘前、20 分鐘前，全部閒置。
    observations = [
        _obs(id_=3, observed_at=_iso(0), gpu_util_max=5.0),
        _obs(id_=2, observed_at=_iso(10), gpu_util_max=6.0),
        _obs(id_=1, observed_at=_iso(20), gpu_util_max=7.0),
    ]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    assert summary.continuous_idle_seconds == 20 * 60


def test_summarize_streak_broken_by_offline_sample():
    observations = [
        _obs(id_=3, observed_at=_iso(0), gpu_util_max=5.0),
        _obs(id_=2, observed_at=_iso(10), gpu_util_max=6.0, online=False),
        _obs(id_=1, observed_at=_iso(20), gpu_util_max=7.0),
    ]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    # 只有最新那筆連續閒置（往回第二筆離線就中斷），streak 長度 1 -> 0 秒。
    assert summary.continuous_idle_seconds == 0


def test_summarize_streak_broken_by_none_gpu_util_on_gpu_server():
    observations = [
        _obs(id_=3, observed_at=_iso(0), gpu_util_max=5.0),
        _obs(id_=2, observed_at=_iso(10), gpu_util_max=None),
        _obs(id_=1, observed_at=_iso(20), gpu_util_max=7.0),
    ]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    assert summary.continuous_idle_seconds == 0


def test_summarize_newest_sample_not_idle_like_gives_zero_streak():
    observations = [
        _obs(id_=2, observed_at=_iso(0), gpu_util_max=90.0),
        _obs(id_=1, observed_at=_iso(10), gpu_util_max=5.0),
    ]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    assert summary.continuous_idle_seconds == 0


def test_summarize_cpu_server_uses_load1_threshold():
    observations = [
        _obs(id_=2, observed_at=_iso(0), load1=0.5),
        _obs(id_=1, observed_at=_iso(15), load1=0.8),
    ]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=False,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    assert summary.continuous_idle_seconds == 15 * 60
    assert summary.load1_p50 is not None


def test_summarize_online_ratio_is_mixed_fraction():
    observations = [
        _obs(id_=4, observed_at=_iso(0), online=True),
        _obs(id_=3, observed_at=_iso(5), online=False),
        _obs(id_=2, observed_at=_iso(10), online=True),
        _obs(id_=1, observed_at=_iso(15), online=True),
    ]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    assert summary.online_ratio == 0.75


def test_summarize_percentiles_skip_none_values():
    observations = [
        _obs(id_=3, observed_at=_iso(0), gpu_util_max=None),
        _obs(id_=2, observed_at=_iso(5), gpu_util_max=10.0),
        _obs(id_=1, observed_at=_iso(10), gpu_util_max=20.0),
    ]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    assert summary.gpu_util_p50 == 15.0
    assert summary.sample_count == 3


def test_summarize_malformed_observed_at_row_breaks_streak_and_freshness():
    observations = [
        _obs(id_=2, observed_at="not-a-timestamp", gpu_util_max=5.0),
        _obs(id_=1, observed_at=_iso(10), gpu_util_max=6.0),
    ]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    # 最新樣本時間戳不可信：freshness 跟 streak 都無法確認，回傳 None。
    assert summary.freshness_seconds is None
    assert summary.continuous_idle_seconds is None
    # 其他統計（樣本數、百分位）不受時間戳影響，照常計算。
    assert summary.sample_count == 2


def test_summarize_freshness_seconds_matches_fixed_now_iso():
    observations = [_obs(id_=1, observed_at=_iso(30), gpu_util_max=5.0)]
    summary = summarize_observations(
        "worker-a",
        observations,
        window_hours=24,
        now_iso=_NOW.isoformat(),
        gpu_server=True,
        idle_gpu_util=15.0,
        idle_load=2.0,
    )
    assert summary.freshness_seconds == 30 * 60


# ---------------------------------------------------------------------------
# API: GET /servers/idle-summary
# ---------------------------------------------------------------------------


def _server_config(name: str, **overrides) -> ServerConfig:
    values = {
        "name": name,
        "host": "127.0.0.1",
        "user": "test",
        "key": "~/.ssh/id_rsa",
        "gpu": True,
        "idle_gpu_util": 15.0,
        "idle_load": 2.0,
    }
    values.update(overrides)
    return ServerConfig(**values)


def test_get_idle_summary_shape_and_unknown_status(api_client):
    client, main_module = api_client
    app_state = main_module.app_state
    app_state.server_configs["worker-a"] = _server_config("worker-a")
    app_state.server_configs["worker-b"] = _server_config("worker-b", gpu=False)

    app_state.db.insert_server_observation(
        server_name="worker-a", online=True, probe_ok=True, gpu_util_max=5.0
    )
    # worker-b 沒有任何觀測列 -> unknown。

    resp = client.get("/servers/idle-summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_hours"] == 24
    servers = {s["server_name"]: s for s in body["servers"]}
    assert set(servers) == {"worker-a", "worker-b"}
    assert servers["worker-a"]["status"] == "ok"
    assert servers["worker-a"]["sample_count"] == 1
    assert servers["worker-b"]["status"] == "unknown"
    assert servers["worker-b"]["sample_count"] == 0
    assert set(servers["worker-a"].keys()) == {
        "server_name",
        "window_hours",
        "sample_count",
        "online_ratio",
        "gpu_util_p50",
        "gpu_util_p95",
        "load1_p50",
        "load1_p95",
        "continuous_idle_seconds",
        "freshness_seconds",
        "status",
    }


def test_get_idle_summary_clamps_hours(api_client):
    client, main_module = api_client
    app_state = main_module.app_state
    app_state.server_configs["worker-a"] = _server_config("worker-a")

    resp = client.get("/servers/idle-summary", params={"hours": 0})
    assert resp.status_code == 200
    assert resp.json()["window_hours"] == 1

    resp2 = client.get("/servers/idle-summary", params={"hours": 999999})
    assert resp2.status_code == 200
    assert resp2.json()["window_hours"] == 24 * 30
