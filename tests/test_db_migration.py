"""階段 4：既有（舊 schema）SQLite 檔案要能透過 ALTER TABLE 遷移補上
log_size / log_size_changed_at / stalled_suspect / stall_notified 四個新
欄位，不需要使用者手動處理，也不會讓既有資料遺失。"""

import sqlite3

import pytest

from app.db import Database


_OLD_JOBS_SCHEMA = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'adhoc',
    project TEXT,
    command TEXT NOT NULL,
    require_tag TEXT,
    pin_server TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',
    gpus_needed INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',
    server TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    log_tail TEXT,
    target_server TEXT,
    dataset_name TEXT,
    dataset_version TEXT
);
"""


def test_opening_legacy_db_migrates_in_new_stall_columns(tmp_path):
    db_path = tmp_path / "legacy.db"

    # 先用「階段 4 之前」的 schema 建一個舊資料庫，塞一筆既有資料。
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(_OLD_JOBS_SCHEMA)
    raw_conn.execute(
        "INSERT INTO jobs (type, command, created_at) VALUES ('adhoc', 'sleep 60', '2026-01-01T00:00:00')"
    )
    raw_conn.commit()
    raw_conn.close()

    # 用新版 Database 開啟，應該自動 ALTER TABLE 補欄位，不會拋例外，
    # 既有那筆資料也還在。
    db = Database(str(db_path))
    try:
        cur = db._conn.execute("PRAGMA table_info(jobs)")
        cols = {row[1] for row in cur.fetchall()}
        for expected in ("log_size", "log_size_changed_at", "stalled_suspect", "stall_notified"):
            assert expected in cols

        jobs = db.list_jobs()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.command == "sleep 60"
        # 新欄位預設值：沒有大小資訊、旗標都是 0（未卡住、未通知過）。
        assert job.log_size is None
        assert job.log_size_changed_at is None
        assert job.stalled_suspect == 0
        assert job.stall_notified == 0
    finally:
        db.close()


def test_fresh_db_has_stall_columns_too(tmp_path):
    """全新資料庫（CREATE TABLE IF NOT EXISTS 就已經包含新欄位）走同一條
    遷移程式碼路徑應該是 no-op，不會出錯。"""
    db = Database(str(tmp_path / "fresh.db"))
    try:
        cur = db._conn.execute("PRAGMA table_info(jobs)")
        cols = {row[1] for row in cur.fetchall()}
        for expected in ("log_size", "log_size_changed_at", "stalled_suspect", "stall_notified"):
            assert expected in cols
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 階段 13（PLAN.md N.6）：coding_runs 新表；jobs 加 source_coding_run_id
# ---------------------------------------------------------------------------


def test_opening_legacy_db_migrates_in_coding_runs_table_and_job_column(tmp_path):
    """既有（階段 13 之前）的舊 DB 開啟後，coding_runs 表要存在，jobs 也要
    透過 ALTER TABLE 補上 source_coding_run_id（同階段 4 的既有遷移慣例），
    既有資料不受影響。"""
    db_path = tmp_path / "legacy.db"

    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(_OLD_JOBS_SCHEMA)
    raw_conn.execute(
        "INSERT INTO jobs (type, command, created_at) VALUES ('adhoc', 'sleep 60', '2026-01-01T00:00:00')"
    )
    raw_conn.commit()
    raw_conn.close()

    db = Database(str(db_path))
    try:
        cur = db._conn.execute("PRAGMA table_info(jobs)")
        cols = {row[1] for row in cur.fetchall()}
        assert "source_coding_run_id" in cols

        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='coding_runs'"
        )
        assert cur.fetchone() is not None

        jobs = db.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].source_coding_run_id is None
    finally:
        db.close()


def test_fresh_db_has_coding_runs_table_and_source_coding_run_id_column(tmp_path):
    db = Database(str(tmp_path / "fresh.db"))
    try:
        cur = db._conn.execute("PRAGMA table_info(jobs)")
        cols = {row[1] for row in cur.fetchall()}
        assert "source_coding_run_id" in cols

        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='coding_runs'"
        )
        assert cur.fetchone() is not None
    finally:
        db.close()


def test_coding_runs_crud_basic_flow(db):
    run_id = db.insert_coding_run(
        approval_id=1,
        project="proj",
        runner_server="server-c",
        instruction="add a test",
    )
    run = db.get_coding_run(run_id)
    assert run is not None
    assert run.approval_id == 1
    assert run.project == "proj"
    assert run.runner_server == "server-c"
    assert run.instruction == "add a test"
    assert run.status == "queued"
    assert run.job_id is None

    db.update_coding_run(run_id, status="running", started_at="2026-01-01T00:00:00")
    updated = db.get_coding_run(run_id)
    assert updated.status == "running"
    assert updated.started_at == "2026-01-01T00:00:00"

    other_id = db.insert_coding_run(
        approval_id=2,
        project="other",
        runner_server="server-c",
        instruction="fix bug",
        status="done",
    )
    all_runs = db.list_coding_runs()
    assert [r.id for r in all_runs] == [other_id, run_id]  # 新到舊

    done_runs = db.list_coding_runs(status="done")
    assert [r.id for r in done_runs] == [other_id]

    proj_runs = db.list_coding_runs(project="proj")
    assert [r.id for r in proj_runs] == [run_id]

    limited = db.list_coding_runs(limit=1)
    assert len(limited) == 1
    assert limited[0].id == other_id


# ---------------------------------------------------------------------------
# 階段 16（PLAN.md Q 節）：datasets 表加 card（JSON，nullable）／sync_mode
# （TEXT NOT NULL DEFAULT 'packed'）
# ---------------------------------------------------------------------------

_OLD_DATASETS_SCHEMA = """
CREATE TABLE datasets (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    manifest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
);
"""


def test_opening_legacy_db_migrates_in_dataset_card_and_sync_mode_columns(tmp_path):
    """既有（階段 16 之前）的舊 DB 開啟後，datasets 表要透過 ALTER TABLE 補上
    card（NULL）／sync_mode（預設 'packed'），既有資料不受影響——階段 16
    之前建立的資料集版本一律沒有資料卡，查詢時 card 為 None（見
    GET /datasets/{name}/{version}/card 的 note 欄位）。"""
    db_path = tmp_path / "legacy_datasets.db"

    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(_OLD_DATASETS_SCHEMA)
    raw_conn.execute(
        """
        INSERT INTO datasets (name, version, size_bytes, source_path, manifest, created_at)
        VALUES ('defect', 'v1', 3072, '/data/defect', '{"file_count": 2, "total_size": 3072}',
                '2026-01-01T00:00:00')
        """
    )
    raw_conn.commit()
    raw_conn.close()

    db = Database(str(db_path))
    try:
        cur = db._conn.execute("PRAGMA table_info(datasets)")
        cols = {row[1] for row in cur.fetchall()}
        assert "card" in cols
        assert "sync_mode" in cols

        datasets = db.list_datasets()
        assert len(datasets) == 1
        dataset = datasets[0]
        assert dataset.name == "defect"
        assert dataset.version == "v1"
        assert dataset.card is None
        assert dataset.sync_mode == "packed"
    finally:
        db.close()


def test_fresh_db_has_dataset_card_and_sync_mode_columns_too(tmp_path):
    db = Database(str(tmp_path / "fresh_datasets.db"))
    try:
        cur = db._conn.execute("PRAGMA table_info(datasets)")
        cols = {row[1] for row in cur.fetchall()}
        assert "card" in cols
        assert "sync_mode" in cols
    finally:
        db.close()


def test_insert_dataset_with_and_without_card(db):
    db.insert_dataset(
        name="defect",
        version="v1",
        size_bytes=100,
        source_path="/data/defect",
        manifest={"file_count": 1, "total_size": 100},
    )
    no_card = db.get_dataset("defect", "v1")
    assert no_card.card is None
    assert no_card.sync_mode == "packed"

    card = {
        "description": "缺陷偵測資料集第一版",
        "method": "人工標註",
        "derived_from": None,
        "counts_custom": {"train": 80, "val": 20},
        "created_at": "2026-07-11T00:00:00",
        "updated_at": "2026-07-11T00:00:00",
    }
    db.insert_dataset(
        name="defect",
        version="v2",
        size_bytes=200,
        source_path="/data/defect2",
        manifest={"file_count": 2, "total_size": 200},
        card=card,
    )
    with_card = db.get_dataset("defect", "v2")
    assert with_card.card == card


def test_update_dataset_card(db):
    db.insert_dataset(
        name="defect",
        version="v1",
        size_bytes=100,
        source_path="/data/defect",
        manifest={"file_count": 1, "total_size": 100},
    )
    new_card = {
        "description": "補登的描述",
        "method": "補登的製作方式",
        "derived_from": None,
        "counts_custom": None,
        "created_at": "2026-07-11T00:00:00",
        "updated_at": "2026-07-11T01:00:00",
    }
    db.update_dataset_card("defect", "v1", new_card)
    updated = db.get_dataset("defect", "v1")
    assert updated.card == new_card


def test_update_coding_run_rejects_field_outside_whitelist(db):
    run_id = db.insert_coding_run(
        approval_id=1, project="proj", runner_server="server-c", instruction="x"
    )
    with pytest.raises(ValueError):
        db.update_coding_run(run_id, project="other-proj")
    with pytest.raises(ValueError):
        db.update_coding_run(run_id, instruction="rewritten")


def test_insert_job_with_source_coding_run_id(db):
    run_id = db.insert_coding_run(
        approval_id=1, project="proj", runner_server="server-c", instruction="x"
    )
    job_id = db.insert_job(command="echo hi", type="train", source_coding_run_id=run_id)
    job = db.get_job(job_id)
    assert job.source_coding_run_id == run_id

    plain_job_id = db.insert_job(command="echo hi")
    assert db.get_job(plain_job_id).source_coding_run_id is None


# ---------------------------------------------------------------------------
# 專案詳情頁計畫（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、
# 目標／方法／進度管理」）第 1 節：projects 表加 goal/optimization_notes/
# progress 三欄＋新表 experiment_records。
# ---------------------------------------------------------------------------

#: 這批功能之前（階段 8 之後）的 projects schema——已經有 summary／
#: dataset_mode（階段 8 第一批加的），但還沒有 goal／optimization_notes／
#: progress 三欄。
_OLD_PROJECTS_SCHEMA = """
CREATE TABLE projects (
    name TEXT PRIMARY KEY,
    repo_or_path TEXT NOT NULL,
    dataset_name TEXT,
    dataset_version TEXT,
    default_command TEXT,
    require_tag TEXT,
    setup_cmd TEXT,
    created_at TEXT NOT NULL,
    summary TEXT,
    dataset_mode TEXT NOT NULL DEFAULT 'none'
);
"""


def test_opening_legacy_db_migrates_in_project_doc_columns(tmp_path):
    """既有（這批功能之前）的舊 DB 開啟後，projects 表要透過 ALTER TABLE
    補上 goal／optimization_notes／progress（一律 NULL＝尚未填寫），既有
    資料不受影響；experiment_records 新表也要自動建立（`CREATE TABLE IF
    NOT EXISTS` 本來就會處理，這裡一併確認）。"""
    db_path = tmp_path / "legacy_projects.db"

    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(_OLD_PROJECTS_SCHEMA)
    raw_conn.execute(
        """
        INSERT INTO projects (name, repo_or_path, created_at, summary, dataset_mode)
        VALUES ('legacy-proj', '/repo/legacy', '2026-01-01T00:00:00', 'existing summary', 'none')
        """
    )
    raw_conn.commit()
    raw_conn.close()

    db = Database(str(db_path))
    try:
        cur = db._conn.execute("PRAGMA table_info(projects)")
        cols = {row[1] for row in cur.fetchall()}
        for expected in ("goal", "optimization_notes", "progress"):
            assert expected in cols

        project = db.get_project("legacy-proj")
        assert project is not None
        assert project.summary == "existing summary"  # 既有資料不受影響
        assert project.goal is None
        assert project.optimization_notes is None
        assert project.progress is None

        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='experiment_records'"
        )
        assert cur.fetchone() is not None
    finally:
        db.close()


def test_fresh_db_has_project_doc_columns_and_experiment_records_table(tmp_path):
    db = Database(str(tmp_path / "fresh_projects.db"))
    try:
        cur = db._conn.execute("PRAGMA table_info(projects)")
        cols = {row[1] for row in cur.fetchall()}
        for expected in ("goal", "optimization_notes", "progress"):
            assert expected in cols

        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='experiment_records'"
        )
        assert cur.fetchone() is not None
    finally:
        db.close()


def test_update_project_can_set_new_doc_columns(db):
    db.insert_project("proj1", "/repo/proj1")
    db.update_project("proj1", goal="把準確率衝到 95%", optimization_notes="換 optimizer", progress="第 3 輪訓練中")
    project = db.get_project("proj1")
    assert project.goal == "把準確率衝到 95%"
    assert project.optimization_notes == "換 optimizer"
    assert project.progress == "第 3 輪訓練中"


# ---------------------------------------------------------------------------
# PLAN.md 2026-07-11 版 §14 切片 1：Project identity migration——projects 補
# id（UUID,逐列 backfill）、project_instances 補 project_id（雙寫）與 state
# （預設 'unknown'）。
# ---------------------------------------------------------------------------

#: 切片 1 之前的 projects schema（已含專案詳情頁三欄,還沒有 id）。
_PRE_UUID_PROJECTS_SCHEMA = """
CREATE TABLE projects (
    name TEXT PRIMARY KEY,
    repo_or_path TEXT NOT NULL,
    dataset_name TEXT,
    dataset_version TEXT,
    default_command TEXT,
    require_tag TEXT,
    setup_cmd TEXT,
    created_at TEXT NOT NULL,
    summary TEXT,
    dataset_mode TEXT NOT NULL DEFAULT 'none',
    goal TEXT,
    optimization_notes TEXT,
    progress TEXT
);
CREATE TABLE project_instances (
    id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    git_remote TEXT,
    git_branch TEXT,
    git_commit TEXT,
    dirty INTEGER NOT NULL DEFAULT 0,
    embedded_data_paths TEXT NOT NULL DEFAULT '[]',
    last_seen TEXT
);
"""


def _make_pre_uuid_db(db_path, *, with_orphan_instance: bool = False):
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(_PRE_UUID_PROJECTS_SCHEMA)
    raw_conn.execute(
        """
        INSERT INTO projects (name, repo_or_path, created_at)
        VALUES ('proj-a', '/repo/a', '2026-01-01T00:00:00'),
               ('proj-b', '/repo/b', '2026-01-01T00:00:00')
        """
    )
    raw_conn.execute(
        """
        INSERT INTO project_instances (id, project_name, server, path, last_seen)
        VALUES ('iid-a', 'proj-a', 'server-b', '/work/proj-a', '2026-01-01T00:00:00')
        """
    )
    if with_orphan_instance:
        raw_conn.execute(
            """
            INSERT INTO project_instances (id, project_name, server, path, last_seen)
            VALUES ('iid-gone', 'proj-gone', 'server-b', '/work/gone', '2026-01-01T00:00:00')
            """
        )
    raw_conn.commit()
    raw_conn.close()


def test_opening_legacy_db_backfills_project_uuid_and_instance_columns(tmp_path):
    """既有（切片 1 之前）DB 開啟後:每個 project 拿到互不相同的非 NULL
    UUID;instance 補上 project_id（指向所屬 project 的新 UUID）與
    state='unknown';孤兒 instance（project 已不存在）的 project_id 維持
    NULL,不腦補。"""
    db_path = tmp_path / "legacy_uuid.db"
    _make_pre_uuid_db(db_path, with_orphan_instance=True)

    db = Database(str(db_path))
    try:
        proj_a = db.get_project("proj-a")
        proj_b = db.get_project("proj-b")
        assert proj_a.id and proj_b.id
        assert proj_a.id != proj_b.id

        instances = db.list_project_instances("proj-a")
        assert len(instances) == 1
        assert instances[0].project_id == proj_a.id
        assert instances[0].state == "unknown"

        orphans = db.list_project_instances("proj-gone")
        assert len(orphans) == 1
        assert orphans[0].project_id is None
        assert orphans[0].state == "unknown"
    finally:
        db.close()


def test_project_uuid_is_stable_across_reopen(tmp_path):
    """backfill 只補 NULL:第二次開啟同一個 DB 不得改變已產生的 UUID
    （身分一經建立永不變）。"""
    db_path = tmp_path / "legacy_uuid_stable.db"
    _make_pre_uuid_db(db_path)

    db = Database(str(db_path))
    first_id = db.get_project("proj-a").id
    db.close()

    db = Database(str(db_path))
    try:
        assert db.get_project("proj-a").id == first_id
    finally:
        db.close()


def test_fresh_db_insert_project_generates_uuid_and_get_accepts_it(db):
    """全新 DB:insert_project 產生並回傳 UUID;get_project 雙讀 adapter
    ——名稱與 UUID 都查得到同一筆,名稱精確命中優先。"""
    returned_id = db.insert_project("proj1", "/repo/proj1")
    by_name = db.get_project("proj1")
    assert by_name.id == returned_id

    by_id = db.get_project(returned_id)
    assert by_id is not None
    assert by_id.name == "proj1"

    assert db.get_project("no-such-project-or-id") is None


def test_instance_insert_populates_project_id(db):
    db.insert_project("proj1", "/repo/proj1")
    project_id = db.get_project("proj1").id

    db.insert_project_instance(
        project_name="proj1", server="server-b", path="/work/proj1"
    )
    instances = db.list_project_instances("proj1")
    assert instances[0].project_id == project_id
    assert instances[0].state == "unknown"

    # 專案不存在的 instance(理論上不會發生,防禦):project_id 留 NULL。
    db.insert_project_instance(
        project_name="ghost", server="server-b", path="/work/ghost"
    )
    ghost = db.list_project_instances("ghost")
    assert ghost[0].project_id is None


# ---------------------------------------------------------------------------
# PLAN.md 2026-07-11 版 §14 切片 4:project_versions 新表(canonical version
# service)。全新表,不需要 ALTER TABLE 遷移(同 coding_runs/experiment_
# records 的既有慣例),既有(切片 4 之前)DB 開啟後直接就有這張表。
# ---------------------------------------------------------------------------


def test_opening_legacy_db_creates_project_versions_table(tmp_path):
    db_path = tmp_path / "legacy_pre_versions.db"
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(_OLD_PROJECTS_SCHEMA)
    raw_conn.execute(
        "INSERT INTO projects (name, repo_or_path, created_at) VALUES"
        " ('proj-a', '/repo/a', '2026-01-01T00:00:00')"
    )
    raw_conn.commit()
    raw_conn.close()

    db = Database(str(db_path))
    try:
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_versions'"
        )
        assert cur.fetchone() is not None

        version = db.get_or_create_project_version("proj-a", "abc123full")
        assert version.git_commit == "abc123full"
    finally:
        db.close()


def test_fresh_db_has_project_versions_table(tmp_path):
    db = Database(str(tmp_path / "fresh_versions.db"))
    try:
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_versions'"
        )
        assert cur.fetchone() is not None
    finally:
        db.close()


def test_get_or_create_project_version_is_idempotent_by_commit(db):
    """同一個 (project, commit) 重複呼叫回同一筆——commit 身分不可變,
    不因為第二次帶了不同的 git_ref/source_instance_id 就改寫既有列
    （INV-DATA-1 草案「不可變」的同一精神）。"""
    db.insert_project("proj1", "/repo/proj1")

    v1 = db.get_or_create_project_version(
        "proj1", "commitabc", git_ref="main", source_instance_id="inst-1"
    )
    v2 = db.get_or_create_project_version(
        "proj1", "commitabc", git_ref="other-branch", source_instance_id="inst-2"
    )

    assert v1.id == v2.id
    assert v2.git_ref == "main"  # 沒有被第二次呼叫覆寫
    assert v2.source_instance_id == "inst-1"
    assert len(db.list_project_versions("proj1")) == 1


def test_get_or_create_project_version_different_commits_create_separate_rows(db):
    db.insert_project("proj1", "/repo/proj1")
    v1 = db.get_or_create_project_version("proj1", "commit1")
    v2 = db.get_or_create_project_version("proj1", "commit2")
    assert v1.id != v2.id

    versions = db.list_project_versions("proj1")
    assert len(versions) == 2
    assert versions[0].created_at >= versions[1].created_at  # 新到舊


def test_get_or_create_project_version_populates_project_id(db):
    db.insert_project("proj1", "/repo/proj1")
    project_id = db.get_project("proj1").id

    version = db.get_or_create_project_version("proj1", "commitabc")
    assert version.project_id == project_id


def test_get_or_create_project_version_without_matching_project_leaves_project_id_none(db):
    """理論上不會發生(呼叫端一定先確認過專案存在),但防禦性地驗證:
    找不到對應 Project 時不腦補 project_id。"""
    version = db.get_or_create_project_version("ghost-project", "commitabc")
    assert version.project_id is None


def test_get_project_version_by_id(db):
    db.insert_project("proj1", "/repo/proj1")
    created = db.get_or_create_project_version("proj1", "commitabc", git_ref="main")

    fetched = db.get_project_version(created.id)
    assert fetched.git_commit == "commitabc"
    assert fetched.git_ref == "main"

    assert db.get_project_version("no-such-id") is None


def test_experiment_records_table_supports_full_crud_on_legacy_upgraded_db(tmp_path):
    """既有 DB 升級後，新表不只是「存在」——CRUD 也要能正常運作（跟全新
    DB 的行為完全一致，不會因為是 ALTER 出來的鄰居表就有差異，這裡順便
    確認 `experiment_records` 表本身不需要遷移——`CREATE TABLE IF NOT
    EXISTS` 對全新表向來是一次到位，不像 `projects`/`jobs` 需要 ALTER
    TABLE 補欄位）。"""
    db_path = tmp_path / "legacy_projects2.db"
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(_OLD_PROJECTS_SCHEMA)
    raw_conn.commit()
    raw_conn.close()

    db = Database(str(db_path))
    try:
        db.insert_project("proj1", "/repo/proj1")
        record_id = db.insert_experiment_record("proj1", "測試紀錄", kind="note")
        record = db.get_experiment_record(record_id)
        assert record.project == "proj1"
        assert record.content == "測試紀錄"
    finally:
        db.close()
