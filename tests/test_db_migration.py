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


# ---------------------------------------------------------------------------
# Goal 1 / Slice 1: actor identity persistence and nullable approval attribution
# ---------------------------------------------------------------------------

_PRE_GOAL1_APPROVALS_SCHEMA = """
CREATE TABLE approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    note TEXT
);
"""

_PRE_GOAL1_TEN_TABLE_SCHEMA = _PRE_GOAL1_APPROVALS_SCHEMA + """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'queued'
);
CREATE TABLE projects (
    name TEXT PRIMARY KEY,
    id TEXT
);
CREATE TABLE datasets (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE dataset_cache (
    server TEXT NOT NULL,
    dataset TEXT NOT NULL,
    version TEXT NOT NULL,
    PRIMARY KEY (server, dataset, version)
);
CREATE TABLE project_candidates (
    id TEXT PRIMARY KEY,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE project_instances (
    id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    project_id TEXT,
    state TEXT NOT NULL DEFAULT 'unknown'
);
CREATE TABLE project_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    project_name TEXT NOT NULL,
    git_commit TEXT NOT NULL
);
CREATE TABLE coding_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'queued'
);
CREATE TABLE experiment_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL
);
"""


def test_fresh_db_has_goal1_identity_tables_and_approval_columns(tmp_path):
    db = Database(str(tmp_path / "fresh_goal1.db"))
    try:
        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "actors",
            "oidc_identities",
            "oidc_login_flows",
            "actor_sessions",
            "service_accounts",
            "service_account_tokens",
            "project_memberships",
        } <= tables

        approval_cols = {
            row[1] for row in db._conn.execute("PRAGMA table_info(approvals)").fetchall()
        }
        assert {
            "requester_actor_id",
            "decision_actor_id",
            "decision_mechanism",
        } <= approval_cols

        indexes = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "idx_oidc_identities_issuer_subject" in indexes
        assert "idx_actor_sessions_secret_hash" in indexes
        assert "idx_service_account_tokens_secret_hash" in indexes
        assert "idx_project_memberships_actor_id" in indexes
        assert "idx_approvals_requester_actor_id" in indexes
        assert "idx_approvals_decision_actor_id" in indexes
    finally:
        db.close()


def test_opening_pre_goal1_db_preserves_approval_and_audit_history(tmp_path):
    db_path = tmp_path / "pre_goal1.db"
    audit_path = tmp_path / "audit.jsonl"
    audit_bytes = b'{"ts":"old","action":"approval_requested","params":{"approval_id":1},"result":"ok"}\n'
    audit_path.write_bytes(audit_bytes)

    raw = sqlite3.connect(str(db_path))
    raw.executescript(_PRE_GOAL1_APPROVALS_SCHEMA)
    payload = '{"command":"echo legacy","source":"api"}'
    raw.execute(
        """
        INSERT INTO approvals
            (id, kind, payload, status, created_at, decided_at, note)
        VALUES (1, 'enqueue', ?, 'approved', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:01:00+00:00', 'legacy note')
        """,
        (payload,),
    )
    before = raw.execute("SELECT * FROM approvals").fetchone()
    raw.commit()
    raw.close()

    db = Database(str(db_path))
    try:
        row = db._conn.execute(
            """
            SELECT id, kind, payload, status, created_at, decided_at, note,
                   requester_actor_id, decision_actor_id, decision_mechanism
            FROM approvals WHERE id = 1
            """
        ).fetchone()
        assert tuple(row[:7]) == tuple(before)
        assert row[7:] == (None, None, None)
        assert db._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert db._conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM project_memberships").fetchone()[0] == 0
    finally:
        db.close()

    # Opening the SQLite database must never rewrite append-only audit evidence.
    assert audit_path.read_bytes() == audit_bytes

    reopened = Database(str(db_path))
    try:
        assert reopened._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert reopened._conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0] == 0
    finally:
        reopened.close()


def test_pre_goal1_style_approval_insert_still_works(tmp_path):
    db = Database(str(tmp_path / "compat.db"))
    try:
        approval_id = db.insert_approval("enqueue", {"command": "echo compatible"})
        approval = db.get_approval(approval_id)
        assert approval.requester_actor_id is None
        assert approval.decision_actor_id is None
        assert approval.decision_mechanism is None
    finally:
        db.close()


def test_slice6_approval_kinds_are_additive_on_a_pre_goal1_database(tmp_path):
    db_path = tmp_path / "slice6_approval_kinds.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript(_PRE_GOAL1_APPROVALS_SCHEMA)
    raw.execute(
        "INSERT INTO approvals VALUES "
        "(1, 'enqueue', '{\"command\":\"legacy\"}', 'pending', "
        "'created', NULL, NULL)"
    )
    raw.commit()
    raw.close()

    db = Database(str(db_path))
    try:
        payloads = {
            "service_account_create": {
                "actor_id": "00000000-0000-0000-0000-000000000010",
                "name": "automation",
                "description": None,
            },
            "service_token_issue": {
                "service_account_actor_id": (
                    "00000000-0000-0000-0000-000000000010"
                ),
                "label": None,
                "scopes": [],
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
            "service_token_revoke": {
                "token_id": "00000000-0000-0000-0000-000000000011"
            },
            "project_membership_upsert": {
                "project_id": "00000000-0000-0000-0000-000000000012",
                "actor_id": "00000000-0000-0000-0000-000000000013",
                "role": "viewer",
            },
            "project_membership_remove": {
                "project_id": "00000000-0000-0000-0000-000000000012",
                "actor_id": "00000000-0000-0000-0000-000000000013",
            },
        }
        inserted = [
            db.insert_approval(kind, payload) for kind, payload in payloads.items()
        ]

        assert [db.get_approval(approval_id).kind for approval_id in inserted] == list(
            payloads
        )
        legacy = db.get_approval(1)
        assert legacy.kind == "enqueue"
        assert legacy.payload == {"command": "legacy"}
        assert legacy.requester_actor_id is None
    finally:
        db.close()


def test_goal1_migration_preserves_rows_in_all_ten_existing_tables(tmp_path):
    """A literal pre-Goal-1 schema keeps every existing table's known facts.

    The compact fixture includes every column referenced by current indexes and
    migration backfills; columns unrelated to Goal 1 are intentionally omitted
    because CREATE TABLE IF NOT EXISTS has never served as a column migration.
    """

    db_path = tmp_path / "ten_tables.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript(_PRE_GOAL1_TEN_TABLE_SCHEMA)
    raw.execute(
        "INSERT INTO approvals VALUES (1, 'enqueue', '{\"command\":\"legacy\"}',"
        " 'pending', 'created', NULL, 'note')"
    )
    raw.execute("INSERT INTO jobs (id, status) VALUES (1, 'queued')")
    raw.execute("INSERT INTO projects VALUES ('project-a', 'project-uuid-a')")
    raw.execute("INSERT INTO datasets VALUES ('dataset-a', 'v1')")
    raw.execute("INSERT INTO dataset_cache VALUES ('server-a', 'dataset-a', 'v1')")
    raw.execute(
        "INSERT INTO project_candidates VALUES ('candidate-a', 'server-a', '/p', 'pending')"
    )
    raw.execute(
        "INSERT INTO project_instances VALUES"
        " ('instance-a', 'project-a', 'project-uuid-a', 'unknown')"
    )
    raw.execute(
        "INSERT INTO project_versions VALUES"
        " ('version-a', 'project-uuid-a', 'project-a', 'abc123')"
    )
    raw.execute("INSERT INTO coding_runs (id, status) VALUES (1, 'queued')")
    raw.execute("INSERT INTO experiment_records (id, project) VALUES (1, 'project-a')")

    table_names = (
        "jobs",
        "approvals",
        "projects",
        "datasets",
        "dataset_cache",
        "project_candidates",
        "project_instances",
        "project_versions",
        "coding_runs",
        "experiment_records",
    )
    old_columns = {
        table: [row[1] for row in raw.execute(f"PRAGMA table_info({table})").fetchall()]
        for table in table_names
    }
    before = {
        table: raw.execute(
            f"SELECT {', '.join(old_columns[table])} FROM {table} ORDER BY 1"
        ).fetchall()
        for table in table_names
    }
    raw.commit()
    raw.close()

    migrated = Database(str(db_path))
    try:
        for table in table_names:
            after = migrated._conn.execute(
                f"SELECT {', '.join(old_columns[table])} FROM {table} ORDER BY 1"
            ).fetchall()
            assert [tuple(row) for row in after] == before[table]
        assert migrated._conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0] == 0
        assert migrated._conn.execute(
            "SELECT COUNT(*) FROM project_memberships"
        ).fetchone()[0] == 0
    finally:
        migrated.close()


# ---------------------------------------------------------------------------
# Plan v2 Slice 2: immutable AI Engineering Task contract.  The parent table
# is new, while ownership/version columns on jobs and coding_runs must be
# additive and must not invent bindings for historical rows.
# ---------------------------------------------------------------------------


def test_fresh_db_has_engineering_task_tables_columns_and_owner_indexes(tmp_path):
    db = Database(str(tmp_path / "fresh_engineering_tasks.db"))
    try:
        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "engineering_tasks",
            "engineering_task_events",
            "engineering_task_commands",
            "engineering_task_artifacts",
            "engineering_validation_requests",
        } <= tables

        task_columns = {
            row[1]
            for row in db._conn.execute(
                "PRAGMA table_info(engineering_tasks)"
            ).fetchall()
        }
        assert {
            "approval_id",
            "project_id",
            "project_version_id",
            "base_commit",
            "agent_provider_id",
            "execution_contract",
            "contract_version",
            "structured_request",
            "runner_server",
            "status",
        } <= task_columns

        event_columns = {
            row[1]
            for row in db._conn.execute(
                "PRAGMA table_info(engineering_task_events)"
            ).fetchall()
        }
        assert {
            "engineering_task_id",
            "attempt_number",
            "event_key",
            "event_type",
            "phase",
            "state",
            "details",
            "source_kind",
            "occurred_at",
            "recorded_at",
        } <= event_columns

        command_columns = {
            row[1]
            for row in db._conn.execute(
                "PRAGMA table_info(engineering_task_commands)"
            ).fetchall()
        }
        assert {
            "engineering_task_id",
            "attempt_number",
            "command_key",
            "job_id",
            "display_command",
            "command_digest",
            "execution_location",
            "working_directory_label",
            "policy_family",
            "policy_disposition",
            "status_source",
        } <= command_columns
        assert "command" not in command_columns
        assert "working_directory" not in command_columns

        artifact_columns = {
            row[1]
            for row in db._conn.execute(
                "PRAGMA table_info(engineering_task_artifacts)"
            ).fetchall()
        }
        assert {
            "artifact_key",
            "engineering_task_id",
            "attempt_number",
            "kind",
            "storage_key",
            "source_sha256",
            "source_size_bytes",
            "verification_status",
            "redaction_status",
            "availability",
        } <= artifact_columns

        job_columns = {
            row[1] for row in db._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        assert {
            "engineering_task_id",
            "engineering_task_role",
            "engineering_attempt_number",
            "engineering_validation_request_id",
        } <= job_columns

        validation_columns = {
            row[1]
            for row in db._conn.execute(
                "PRAGMA table_info(engineering_validation_requests)"
            ).fetchall()
        }
        assert {
            "engineering_task_id",
            "attempt_number",
            "coding_run_id",
            "approval_id",
            "project_version_id",
            "base_commit",
            "result_commit",
            "target_server",
            "request_snapshot",
            "request_snapshot_sha256",
            "bundle_push_command_sha256",
            "downstream_command_sha256",
            "bundle_push_job_id",
            "downstream_job_id",
            "result_status",
        } <= validation_columns

        run_columns = {
            row[1]
            for row in db._conn.execute("PRAGMA table_info(coding_runs)").fetchall()
        }
        assert {
            "engineering_task_id",
            "project_version_id",
            "base_binding",
            "attempt_number",
        } <= run_columns

        indexes = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "idx_jobs_engineering_owner" in indexes
        assert "idx_coding_runs_engineering_attempt" in indexes
        assert "idx_coding_runs_project_version_id" in indexes
        assert "idx_engineering_task_events_task" in indexes
        assert "idx_engineering_task_events_attempt" in indexes
        assert "idx_engineering_task_commands_task" in indexes
        assert "idx_engineering_task_commands_job" in indexes
        assert "idx_engineering_task_artifacts_task" in indexes
        assert "idx_engineering_validation_requests_task" in indexes
        assert "idx_engineering_validation_requests_run" in indexes
        assert "idx_jobs_engineering_validation_request" in indexes
    finally:
        db.close()


def test_pre_engineering_db_adds_nullable_ownership_without_fabricating_bindings(
    tmp_path,
):
    db_path = tmp_path / "pre_engineering_tasks.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript(_PRE_GOAL1_TEN_TABLE_SCHEMA)
    raw.execute("INSERT INTO jobs (id, status) VALUES (41, 'queued')")
    raw.execute("INSERT INTO coding_runs (id, status) VALUES (17, 'done')")
    raw.commit()
    raw.close()

    migrated = Database(str(db_path))
    try:
        job = migrated._conn.execute(
            """
            SELECT id, status, engineering_task_id, engineering_task_role,
                   engineering_attempt_number,
                   engineering_validation_request_id
            FROM jobs WHERE id = 41
            """
        ).fetchone()
        assert tuple(job) == (41, "queued", None, None, None, None)

        run = migrated._conn.execute(
            """
            SELECT id, status, engineering_task_id, project_version_id,
                   base_binding, attempt_number
            FROM coding_runs WHERE id = 17
            """
        ).fetchone()
        assert tuple(run) == (17, "done", None, None, "legacy_unpinned", None)
        assert migrated.list_engineering_tasks() == []
        for table in (
            "engineering_task_events",
            "engineering_task_commands",
            "engineering_task_artifacts",
            "engineering_validation_requests",
        ):
            assert migrated._conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0
    finally:
        migrated.close()


def test_partial_worker_validation_schema_adds_command_digests(tmp_path):
    db_path = tmp_path / "partial_worker_validation.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript(_PRE_GOAL1_TEN_TABLE_SCHEMA)
    raw.executescript(
        """
        CREATE TABLE engineering_validation_requests (
            id TEXT PRIMARY KEY,
            engineering_task_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            coding_run_id INTEGER NOT NULL,
            approval_id INTEGER NOT NULL UNIQUE,
            project_id TEXT NOT NULL,
            project_name TEXT NOT NULL,
            project_version_id TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            result_commit TEXT NOT NULL,
            target_server TEXT NOT NULL,
            request_snapshot TEXT NOT NULL,
            request_snapshot_sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_approval',
            bundle_push_job_id INTEGER,
            downstream_job_id INTEGER,
            result_status TEXT,
            result_exit_code INTEGER,
            result_finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    raw.commit()
    raw.close()

    migrated = Database(str(db_path))
    try:
        columns = {
            row[1]
            for row in migrated._conn.execute(
                "PRAGMA table_info(engineering_validation_requests)"
            ).fetchall()
        }
        assert "bundle_push_command_sha256" in columns
        assert "downstream_command_sha256" in columns
    finally:
        migrated.close()
