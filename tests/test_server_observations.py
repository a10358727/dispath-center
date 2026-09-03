"""Goal 2 Slice 1（docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：容量觀測持久化。

涵蓋：additive schema 的 insert/list/prune 純 DB 行為、config flag 預設值與
env 覆寫、唯讀 API shape/404/clamping、以及 monitor tick 在 DB 寫入失敗時
仍然更新 in-memory `server_states`（state-reconciliation 邊界）。
"""

from datetime import datetime, timedelta, timezone

from app.config import AppConfig, ServerConfig, load_app_config
from app.db import Database
from app.monitor import GpuReading, ServerState


# ---------------------------------------------------------------------------
# insert / list / limit / ordering
# ---------------------------------------------------------------------------


def test_insert_and_list_returns_newest_first(db):
    for i in range(5):
        db.insert_server_observation(
            server_name="worker-a",
            online=True,
            probe_ok=True,
            gpu_count=1,
            gpu_util_max=float(i),
            gpu_mem_used_mb=100.0,
            gpu_mem_total_mb=8192.0,
            load1=0.1,
            mem_total_bytes=1024,
            mem_available_bytes=512,
            disk_avail_bytes=2048,
        )
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = db.list_server_observations("worker-a", since_iso=since, limit=100)
    assert len(rows) == 5
    # 最新在前：最後插入的那一筆 gpu_util_max 最大（4.0）應該排第一。
    assert rows[0].gpu_util_max == 4.0
    assert rows[-1].gpu_util_max == 0.0


def test_list_limit_caps_result_count(db):
    for _ in range(10):
        db.insert_server_observation(server_name="worker-a", online=True, probe_ok=True)
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = db.list_server_observations("worker-a", since_iso=since, limit=3)
    assert len(rows) == 3


def test_list_is_scoped_to_server_name(db):
    db.insert_server_observation(server_name="worker-a", online=True, probe_ok=True)
    db.insert_server_observation(server_name="worker-b", online=True, probe_ok=True)
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = db.list_server_observations("worker-a", since_iso=since, limit=100)
    assert len(rows) == 1
    assert rows[0].server_name == "worker-a"


# ---------------------------------------------------------------------------
# since 過濾（用 raw SQL 控制 observed_at，insert_server_observation 只會用
# 現在時間，測「since」邊界需要能自由指定歷史時間戳）。
# ---------------------------------------------------------------------------


def _raw_insert_observation(db: Database, server_name: str, observed_at: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO server_observations
                (server_name, observed_at, online, probe_ok)
            VALUES (?, ?, 1, 1)
            """,
            (server_name, observed_at),
        )


def test_since_filter_excludes_older_rows(db):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=10)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()
    _raw_insert_observation(db, "worker-a", old)
    _raw_insert_observation(db, "worker-a", recent)

    since = (now - timedelta(days=1)).isoformat()
    rows = db.list_server_observations("worker-a", since_iso=since, limit=100)
    assert len(rows) == 1
    assert rows[0].observed_at == recent


# ---------------------------------------------------------------------------
# prune 邊界：早於 cutoff 的刪掉，等於/晚於 cutoff 的留著。
# ---------------------------------------------------------------------------


def test_prune_deletes_older_rows_keeps_newer_and_returns_count(db):
    now = datetime.now(timezone.utc)
    older = (now - timedelta(days=20)).isoformat()
    newer = (now - timedelta(days=1)).isoformat()
    _raw_insert_observation(db, "worker-a", older)
    _raw_insert_observation(db, "worker-a", newer)

    cutoff = (now - timedelta(days=14)).isoformat()
    deleted = db.prune_server_observations(before_iso=cutoff)
    assert deleted == 1

    remaining = db.list_server_observations(
        "worker-a", since_iso=(now - timedelta(days=30)).isoformat(), limit=100
    )
    assert len(remaining) == 1
    assert remaining[0].observed_at == newer


# ---------------------------------------------------------------------------
# 舊 DB reopen：additive CREATE TABLE IF NOT EXISTS 自動補表，不需要
# ALTER TABLE 遷移。
# ---------------------------------------------------------------------------


def test_legacy_db_reopen_creates_server_observations_table(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    first = Database(db_path)
    first.close()

    reopened = Database(db_path)
    try:
        tables = {
            row[0]
            for row in reopened._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "server_observations" in tables
        # 表可用：插入/查詢不出錯。
        reopened.insert_server_observation(
            server_name="worker-a", online=True, probe_ok=True
        )
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        rows = reopened.list_server_observations(
            "worker-a", since_iso=since, limit=10
        )
        assert len(rows) == 1
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# config flag 預設值 + env 覆寫
# ---------------------------------------------------------------------------


def test_server_observations_config_defaults():
    config = AppConfig(servers=[])
    assert config.server_observations_enabled is True
    assert config.server_observation_retention_days == 14


def test_load_app_config_reads_server_observations_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVER_OBSERVATIONS_ENABLED", "false")
    monkeypatch.setenv("SERVER_OBSERVATION_RETENTION_DAYS", "30")

    config = load_app_config(
        servers_yaml_path=str(tmp_path / "servers.yaml"),
        dotenv_path=str(tmp_path / ".env"),
    )
    assert config.server_observations_enabled is False
    assert config.server_observation_retention_days == 30


# ---------------------------------------------------------------------------
# API shape / 404 / hours-limit clamping
# ---------------------------------------------------------------------------


def test_get_observations_returns_newest_first_shape(api_client):
    client, main_module = api_client
    main_module.app_state.config.server_observations_enabled = True
    #: The route only needs the server to be known; a state in
    #: `server_states` would also be persisted by the observation tick on a
    #: slow machine and add a third row (CI flake), so register a config.
    main_module.app_state.server_configs["worker-a"] = ServerConfig(
        name="worker-a", host="192.0.2.10", user="train", key="/dispatch-test/nonexistent-key", port=22, enabled=True
    )
    main_module.app_state.db.insert_server_observation(
        server_name="worker-a", online=True, probe_ok=True, load1=0.2
    )
    main_module.app_state.db.insert_server_observation(
        server_name="worker-a", online=True, probe_ok=True, load1=0.4
    )

    resp = client.get("/servers/worker-a/observations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["server"] == "worker-a"
    assert len(body["observations"]) == 2
    assert body["observations"][0]["load1"] == 0.4
    assert set(body["observations"][0].keys()) == {
        "id",
        "server_name",
        "observed_at",
        "online",
        "probe_ok",
        "gpu_count",
        "gpu_util_max",
        "gpu_mem_used_mb",
        "gpu_mem_total_mb",
        "load1",
        "mem_total_bytes",
        "mem_available_bytes",
        "disk_avail_bytes",
    }


def test_get_observations_404_for_unknown_server(api_client):
    client, main_module = api_client
    resp = client.get("/servers/does-not-exist/observations")
    assert resp.status_code == 404


def test_get_observations_clamps_hours_and_limit(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs["worker-a"] = ServerConfig(
        name="worker-a", host="192.0.2.10", user="train", key="/dispatch-test/nonexistent-key", port=22, enabled=True
    )
    main_module.app_state.db.insert_server_observation(
        server_name="worker-a", online=True, probe_ok=True
    )

    # hours=0 應夾到 1，limit 超過上限應夾到 2000；兩者都不應該 500。
    resp = client.get(
        "/servers/worker-a/observations", params={"hours": 0, "limit": 999999}
    )
    assert resp.status_code == 200
    resp2 = client.get(
        "/servers/worker-a/observations", params={"hours": 999999, "limit": -5}
    )
    assert resp2.status_code == 200
    assert len(resp2.json()["observations"]) == 1


# ---------------------------------------------------------------------------
# monitor tick：DB 寫入失敗容錯——server_states 已經先更新，寫入例外不能
# 影響它（state-reconciliation 邊界）。
# ---------------------------------------------------------------------------


def test_persist_server_observations_failure_does_not_raise(api_client, monkeypatch):
    """`_persist_server_observations` 對每台伺服器單獨 try/except，DB 寫入
    丟例外時方法本身不能往外傳例外（monitor_loop 呼叫端會先做完
    `server_states` 更新才呼叫這個方法，方法內部失敗不能讓呼叫端整輪迴圈
    掛掉）。"""
    client, main_module = api_client
    app_state = main_module.app_state

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(app_state.db, "insert_server_observation", _raise)

    state = ServerState(name="worker-a", online=True, load1=0.1)
    # server_states 的更新（monitor_loop 既有順序：先寫 dict 再呼叫落地方法）
    app_state.server_states["worker-a"] = state

    # 呼叫落地方法不應該丟例外，且呼叫後 server_states 仍然是同一個物件。
    app_state._persist_server_observations({"worker-a": state})
    assert app_state.server_states["worker-a"] is state


def test_persist_server_observations_sums_gpu_memory_across_gpus(api_client):
    client, main_module = api_client
    app_state = main_module.app_state
    state = ServerState(
        name="worker-a",
        online=True,
        gpus=[
            GpuReading(util_percent=10.0, mem_used_mb=100.0, mem_total_mb=8192.0),
            GpuReading(util_percent=20.0, mem_used_mb=200.0, mem_total_mb=8192.0),
        ],
    )
    #: persist the state explicitly; leaving it out of `server_states` keeps the
    #: observation tick from writing a second identical row (CI flake).
    app_state._persist_server_observations({"worker-a": state})

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = app_state.db.list_server_observations("worker-a", since_iso=since, limit=10)
    assert len(rows) == 1
    assert rows[0].gpu_count == 2
    assert rows[0].gpu_util_max == 20.0
    assert rows[0].gpu_mem_used_mb == 300.0
    assert rows[0].gpu_mem_total_mb == 16384.0
