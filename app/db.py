"""SQLite 層：schema 建立與存取。

選擇：使用標準函式庫 `sqlite3`（同步）+ `threading.Lock` 序列化寫入，
不用 aiosqlite。原因：階段 1 佇列操作量很小（幾十筆任務等級），用同步
sqlite3 搭配單一鎖最簡單可靠；非同步呼叫端（FastAPI route、scheduler
迴圈）用 `asyncio.to_thread()` 呼叫這裡的同步函式即可，不需要在整個
專案中維護兩套 DB API。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

VALID_STATUSES = {"queued", "running", "done", "failed", "blocked", "cancelled"}
VALID_PRIORITIES = {"normal", "low"}
#: 階段 3 新增 "setup"：機器首次跑某專案時的 git clone/pull + setup_cmd 依賴任務。
#: 階段 13 新增 "coding"：AI 改碼層次二（Codex Worker），見 app/approvals.py
#: 的 request_coding_task_approval()／approve() 的 coding_task 分支。
VALID_TYPES = {"train", "sync", "adhoc", "setup", "coding"}

#: 階段 2：approvals 表（PLAN.md C 節）。核准對象不只「排任務」——停止任務
#: （本階段）、同步計畫（階段 3）、聊天 enqueue 卡片（階段 5）都走同一套
#: 核准，因此用獨立表而不是在 jobs 上加 pending 狀態。
#: 階段 8（第一批）：新增 import_project/ignore_project_candidate/
#: inventory_scan 三個 kind（PLAN.md I 節）。
#: 階段 8（第二批）：新增 server_add/server_update/server_disable/
#: server_delete 四個 kind（Web Server Management，見 app/server_config.py／
#: app/approvals.py）。
#: 階段 12（PLAN.md M 節）：新增 apply_patch——AI 改碼層次一（讀檔＋diff
#: 核准卡）。**這個 kind 永遠不進 `app.approvals.maybe_auto_approve()` 的
#: 自動核准白名單**（該函式只認 "enqueue"/"stop"，見其 docstring）——人必須
#: 逐個看過 diff 才能核准，不允許使用者自己的規則檔繞過。
#: 階段 13（PLAN.md N 節）：新增 coding_task——AI 改碼層次二（Codex
#: Worker）。理由同 apply_patch：**這個 kind 也永遠不進自動核准白名單**，
#: instruction 是自然語言、agent 有真實行動力（跑指令/裝套件/連網），人必須
#: 逐個核准。
#: 階段 15 Phase A（PLAN.md P.1.2 節，Fable 裁定第 2 點）：新增
#: ignore_nested_candidates——一次核准把「路徑位於其他非 ignored 候選之下」
#: 的 pending 候選全部標 ignored 的批次卡（維持「候選整理走核准」的既有
#: 一致性，不是繞過核准的直接執行）。同樣不進自動核准白名單（`maybe_
#: auto_approve()` 只認 "enqueue"/"stop"，天然排除，見該函式 docstring）。
#: 階段 15 Phase B（PLAN.md P.2.1 節）：新增 git_init——把一個尚未受 git
#: 管理的 project_instance 就地初始化成 git repo（寫 .gitignore／
#: `git init`／`git add -A`／size guard／commit）。理由同 apply_patch／
#: coding_task：**這個 kind 也永遠不會被 `maybe_auto_approve()` 自動核准**
#: （白名單只認 "enqueue"/"stop"，天然排除）——即使 size guard 通過，
#: 「把哪個目錄變成 git repo」仍然是人必須看過 .gitignore 內容才能決定的
#: 動作。
#: 階段 15 Phase C（PLAN.md P.3 節）：新增 project_deploy——把中央 hub
#: 的某個 ref 部署成另一台機器上一個全新的 project_instance（bundle 建立
#: →rsync 推送→目標機 clone）。理由同 git_init：**這個 kind 也永遠不會被
#: `maybe_auto_approve()` 自動核准**（白名單只認 "enqueue"/"stop"，天然
#: 排除）——把哪個目錄部署到哪台機器仍然是人必須核准的動作。
VALID_APPROVAL_KINDS = {
    "enqueue",
    "stop",
    "import_project",
    "ignore_project_candidate",
    "inventory_scan",
    "server_add",
    "server_update",
    "server_disable",
    "server_delete",
    "apply_patch",
    "coding_task",
    "ignore_nested_candidates",
    "git_init",
    "project_deploy",
}
VALID_APPROVAL_STATUSES = {"pending", "approved", "rejected"}

#: 階段 8：project_candidates.status 值域（PLAN.md I.1）。
VALID_CANDIDATE_STATUSES = {"pending", "imported", "ignored"}
#: 階段 8：projects.dataset_mode 值域，Python 層驗證（PLAN.md I.1）。
#: "embedded" 表示資料就放在專案目錄底下（掃描到的 embedded dataset），
#: 不透過 datasets/dataset_cache 表管理、不自動同步——見
#: app/approvals.py 的 import_project 核准分支說明。
VALID_DATASET_MODES = {"none", "registered", "embedded"}

#: 專案詳情頁（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、目標／
#: 方法／進度管理」計畫第 1 節）：experiment_records.kind 值域，Python 層
#: 驗證，比照 `VALID_DATASET_MODES`。note＝一般筆記、observation＝觀察、
#: conclusion＝結論、decision＝決策——純粹給人／agent 分類用，不影響任何
#: 排程或核准邏輯。
VALID_RECORD_KINDS = {"note", "observation", "conclusion", "decision"}

#: PLAN.md 2026-07-11 版 §14 切片 1（Project identity migration）：
#: project_instances.state 值域（§6.1）。本批只做 schema readiness——新增
#: 欄位、預設 'unknown'、Python 層驗證；真正的 available/missing/dirty/
#: diverged 判定與轉移由切片 2（instance reconciliation）接手,在那之前
#: 既有列一律維持 'unknown'（誠實表示「還沒有 reconcile 過」）。
VALID_INSTANCE_STATES = {"available", "missing", "dirty", "diverged", "unknown"}


def _validate_record_author(author: str) -> None:
    """`experiment_records.author` 值域：`"user"`（網頁使用者手動填）或以
    `"agent"` 開頭的字串（`"agent"`＝本地 vLLM，`"agent:chatgpt"`＝MCP
    Bridge，見 `app/agent_tools.py`／`app/mcp_bridge.py`）——用前綴比對而
    不是固定集合，因為 agent 來源未來可能再細分，不想每加一種來源就要改
    這裡的值域常數。"""
    if author != "user" and not author.startswith("agent"):
        raise ValueError(f"invalid experiment record author: {author}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
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
    dataset_version TEXT,
    log_size INTEGER,
    log_size_changed_at TEXT,
    stalled_suspect INTEGER NOT NULL DEFAULT 0,
    stall_notified INTEGER NOT NULL DEFAULT 0,
    source_coding_run_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

-- 階段 3：專案／資料集註冊表、快取地圖（PLAN.md D 節）

-- PLAN.md 2026-07-11 版 §14 切片 1：`id` 是 Project 的正式跨系統身分
-- （UUID，INV-PROJECT-1 草案）；`name` 過渡期仍是 PRIMARY KEY 與既有外鍵
-- 的實際鍵（legacy name adapter 雙讀,見 get_project()）,等雙軌驗證完成
-- 才移除 name 引用（PLAN.md §12）。唯一索引在 _init_schema() 內建立
-- （不能寫在這裡：舊 DB 要先 ALTER TABLE 補上 id 欄,executescript 先跑
-- 會因欄位不存在而失敗）。
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    id TEXT,
    repo_or_path TEXT NOT NULL,
    dataset_name TEXT,
    dataset_version TEXT,
    default_command TEXT,
    require_tag TEXT,
    setup_cmd TEXT,
    created_at TEXT NOT NULL,
    goal TEXT,
    optimization_notes TEXT,
    progress TEXT
);

-- 資料集用「檔案清單＋各檔大小」的 manifest 代替全量 hash（大檔算 checksum
-- 太貴）；manifest 存成 JSON：{"file_count": N, "total_size": N,
-- "files": [{"path": rel_path, "size": bytes}, ...]}。取捨在 README 說明。
-- 階段 16（PLAN.md Q 節）：新增 `card`（JSON，nullable，資料卡——見
-- app/datasets.py 的 build_dataset_card()/render_dataset_card()，形狀
-- {description, method, derived_from, counts_custom, created_at,
-- updated_at}；階段 16 之前建立的舊版本一律是 NULL，查詢不報錯，見
-- GET /datasets/{name}/{version}/card 的 note 欄位）與 `sync_mode`
-- （'packed'｜'rolling'，Q.1/Q.2；本批只存取，不使用——rolling 模式的
-- 實際行為由後續批次接手）。
CREATE TABLE IF NOT EXISTS datasets (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    manifest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    card TEXT,
    sync_mode TEXT NOT NULL DEFAULT 'packed',
    PRIMARY KEY (name, version)
);

-- 快取地圖：記錄每台機器已有哪些「資料集＠版本」。來源兩個：sync 任務成功
-- 後登記；每小時對各機 `ls datasets/*/*/` 校正一次。
CREATE TABLE IF NOT EXISTS dataset_cache (
    server TEXT NOT NULL,
    dataset TEXT NOT NULL,
    version TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (server, dataset, version)
);

-- 階段 8（第一批）：Project Inventory（PLAN.md I.1）。掃描是唯讀的；
-- candidate 的 id 是 server+path 的穩定 hash（見 make_candidate_id()），
-- 重複掃描是 upsert，不是每次新增一列。
CREATE TABLE IF NOT EXISTS project_candidates (
    id TEXT PRIMARY KEY,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    name_guess TEXT,
    kind TEXT NOT NULL DEFAULT 'project',
    git_remote TEXT,
    git_branch TEXT,
    git_commit TEXT,
    markers TEXT NOT NULL DEFAULT '[]',
    readme_excerpt TEXT,
    command_guess TEXT,
    embedded_data_paths TEXT NOT NULL DEFAULT '[]',
    embedded_data_summary TEXT,
    estimated_data_bytes INTEGER,
    excluded_paths TEXT NOT NULL DEFAULT '[]',
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_candidates_server_path
    ON project_candidates(server, path);
CREATE INDEX IF NOT EXISTS idx_project_candidates_status ON project_candidates(status);

-- id 是 project_name+server+path 的穩定 hash（見 make_instance_id()）：
-- 同一個專案在同一台機器同一個路徑只會有一筆，重覆匯入/掃描是 upsert。
-- 切片 1 補 `project_id`（projects.id 的 UUID,過渡期與 project_name 並存
-- 雙寫）與 `state`（值域見 VALID_INSTANCE_STATES；判定邏輯屬切片 2,本批
-- 一律 'unknown'）。
CREATE TABLE IF NOT EXISTS project_instances (
    id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    project_id TEXT,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    git_remote TEXT,
    git_branch TEXT,
    git_commit TEXT,
    dirty INTEGER NOT NULL DEFAULT 0,
    embedded_data_paths TEXT NOT NULL DEFAULT '[]',
    last_seen TEXT,
    state TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_project_instances_project_name
    ON project_instances(project_name);

-- PLAN.md 2026-07-11 版 §14 切片 4(canonical version service):一個
-- ProjectVersion＝某專案在某個時間點的不可變 hub commit(INV-PROJECT-2
-- 草案)。全新表,不需要 ALTER TABLE 遷移(同 coding_runs/
-- experiment_records 的既有慣例)。`(project_name, git_commit)` 唯一
-- ——同一個 commit 只會有一筆,重複遇到（hub_sync 重跑／deploy 用到已
-- 存在的 commit）回既有列,不新增、不覆寫(commit 的身分不可變)。
-- `source_instance_id` 記錄「這個 commit 是從哪個 project_instance 同步
-- 上來的」,project_deploy 是反方向(hub → 新 instance),沒有來源
-- instance,一律 NULL。
CREATE TABLE IF NOT EXISTS project_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    project_name TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    git_ref TEXT,
    source_instance_id TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_versions_project_commit
    ON project_versions(project_name, git_commit);
CREATE INDEX IF NOT EXISTS idx_project_versions_project_id
    ON project_versions(project_id);

-- 階段 13（PLAN.md N.6）：Codex Worker v2——coding_runs 記錄每一次
-- `codex exec` 任務的結果（不再只塞 jobs.log_tail）。回填時機見
-- app/approvals.py 的 coding_task 分支與 on_job_finished hook（N.6）：
-- approve() 建 run（status=queued）→ scheduler dispatch 記 started_at →
-- job 完成時解析回收的 result.json 回填 status/result_commit/test_*/
-- finished_at/error_message。
CREATE TABLE IF NOT EXISTS coding_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id INTEGER NOT NULL,
    job_id INTEGER,
    project TEXT NOT NULL,
    runner_server TEXT NOT NULL,
    instruction TEXT NOT NULL,
    base_branch TEXT,
    base_commit TEXT,
    result_branch TEXT,
    result_commit TEXT,
    worktree_path TEXT,
    bundle_path TEXT,
    validation_target TEXT,
    codex_version TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    test_command TEXT,
    test_exit_code INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_coding_runs_status ON coding_runs(status);

-- 專案詳情頁（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、目標／
-- 方法／進度管理」計畫第 1 節）：手動實驗紀錄——時間軸採「查詢時動態
-- 合併」設計（見 app/records.py 的 build_timeline()），這張表只存人／agent
-- 手動補的筆記，jobs／coding_runs 的自動紀錄不寫進這張表、合併時才拼在
-- 一起。`job_id`／`coding_run_id` 是弱關聯（全案慣例，無 FK），純粹讓一筆
-- 紀錄能標註「針對哪一次執行/哪一次改碼寫的」，不做參照完整性檢查、也不
-- 因為被引用的 job/coding_run 被刪就跟著清掉。刪除專案時這張表的紀錄
-- 不會被清掉（見 `Database.delete_project()` docstring）。
CREATE TABLE IF NOT EXISTS experiment_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note',      -- note/observation/conclusion/decision
    title TEXT,
    content TEXT NOT NULL,                  -- Markdown
    author TEXT NOT NULL DEFAULT 'user',    -- 'user' | 'agent' | 'agent:chatgpt'
    job_id INTEGER,                         -- 弱關聯，無 FK（全案慣例）
    coding_run_id INTEGER,
    extra TEXT NOT NULL DEFAULT '{}',       -- JSON 擴充位（未來存指標數值）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiment_records_project ON experiment_records(project, id);
"""


def make_candidate_id(server: str, path: str) -> str:
    """`project_candidates.id`：`server+path` 的穩定 hash（不是隨機
    UUID）——重複掃描同一台機器同一個路徑會 upsert 成同一列，不會每次
    掃描都新增一列垃圾資料。"""
    return hashlib.sha1(f"{server}:{path}".encode("utf-8")).hexdigest()[:16]


def make_instance_id(project_name: str, server: str, path: str) -> str:
    """`project_instances.id`：`project_name+server+path` 的穩定 hash，理由
    同 `make_candidate_id()`。"""
    return hashlib.sha1(f"{project_name}:{server}:{path}".encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: int
    type: str
    project: Optional[str]
    command: str
    require_tag: Optional[str]
    pin_server: Optional[str]
    depends_on: list[int] = field(default_factory=list)
    gpus_needed: Optional[int] = None
    status: str = "queued"
    server: Optional[str] = None
    priority: str = "normal"
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    log_tail: Optional[str] = None
    #: 階段 3：sync 任務專用欄位。sync 任務本身 `server` 一律是 `_local`
    #: （在 Server A 本地跑 rsync），`target_server` 記錄實際要推去的工作機；
    #: `dataset_name`/`dataset_version` 供任務完成後驗證 manifest、登記
    #: dataset_cache 用。train/adhoc/setup 任務這三欄一律是 None。
    target_server: Optional[str] = None
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    #: 階段 4：卡死偵測（PLAN.md E）。`log_size`/`log_size_changed_at` 是
    #: `app.stall.update_stall_state()` 的持久化狀態；`stalled_suspect` 是
    #: 只給人看的旗標（不影響 `status`）；`stall_notified` 用來讓提醒信只寄
    #: 一次（去重，log 又增長也不清這個旗標，避免同一次卡住反覆寄信）。
    log_size: Optional[int] = None
    log_size_changed_at: Optional[str] = None
    stalled_suspect: int = 0
    stall_notified: int = 0
    #: 階段 13（PLAN.md N.6）：這個 job 若是「用某次 Codex coding run 的
    #: result（changes.bundle）當起點」的後續 train／驗證 job，記對應
    #: coding_runs.id；一般任務一律 None。
    source_coding_run_id: Optional[int] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Job":
        return Job(
            id=row["id"],
            type=row["type"],
            project=row["project"],
            command=row["command"],
            require_tag=row["require_tag"],
            pin_server=row["pin_server"],
            depends_on=json.loads(row["depends_on"] or "[]"),
            gpus_needed=row["gpus_needed"],
            status=row["status"],
            server=row["server"],
            priority=row["priority"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            exit_code=row["exit_code"],
            log_tail=row["log_tail"],
            target_server=row["target_server"],
            dataset_name=row["dataset_name"],
            dataset_version=row["dataset_version"],
            log_size=row["log_size"],
            log_size_changed_at=row["log_size_changed_at"],
            stalled_suspect=row["stalled_suspect"] if row["stalled_suspect"] is not None else 0,
            stall_notified=row["stall_notified"] if row["stall_notified"] is not None else 0,
            source_coding_run_id=row["source_coding_run_id"],
        )


@dataclass
class Approval:
    id: int
    kind: str
    payload: dict
    status: str = "pending"
    created_at: str = ""
    decided_at: Optional[str] = None
    note: Optional[str] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Approval":
        return Approval(
            id=row["id"],
            kind=row["kind"],
            payload=json.loads(row["payload"] or "{}"),
            status=row["status"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            note=row["note"],
        )


@dataclass
class Project:
    name: str
    repo_or_path: str
    #: PLAN.md 2026-07-11 版 §14 切片 1：正式跨系統身分（UUID）。理論上
    #: migration backfill 後不會是 None,型別仍留 Optional 防禦「backfill
    #: 前就被讀出」的邊界。
    id: Optional[str] = None
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    default_command: Optional[str] = None
    require_tag: Optional[str] = None
    setup_cmd: Optional[str] = None
    created_at: str = ""
    #: 階段 8（第一批）：inventory 匯入的專案可以帶一段摘要（多半來自 README
    #: 前段）；`dataset_mode` 區分「沒有資料集要求」／「用 datasets/
    #: dataset_cache 表管理的資料集」／「資料就內嵌在專案目錄底下，不自動
    #: 同步」（見 app/approvals.py 的 import_project 分支說明）。
    summary: Optional[str] = None
    dataset_mode: str = "none"
    #: 專案詳情頁計畫第 1 節：目標／優化方法／目前進度，自由文字 Markdown，
    #: `None` 代表尚未填寫（前端顯示「尚未填寫，點擊編輯」，不是錯誤）。
    goal: Optional[str] = None
    optimization_notes: Optional[str] = None
    progress: Optional[str] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Project":
        return Project(
            name=row["name"],
            repo_or_path=row["repo_or_path"],
            id=row["id"],
            dataset_name=row["dataset_name"],
            dataset_version=row["dataset_version"],
            default_command=row["default_command"],
            require_tag=row["require_tag"],
            setup_cmd=row["setup_cmd"],
            created_at=row["created_at"],
            summary=row["summary"],
            dataset_mode=row["dataset_mode"] if row["dataset_mode"] is not None else "none",
            goal=row["goal"],
            optimization_notes=row["optimization_notes"],
            progress=row["progress"],
        )


@dataclass
class ProjectCandidate:
    """`project_candidates` 一列：inventory 掃描找到的候選專案（PLAN.md
    I.1）。`id` 是 `make_candidate_id(server, path)` 的穩定 hash。"""

    id: str
    server: str
    path: str
    name_guess: Optional[str] = None
    kind: str = "project"
    git_remote: Optional[str] = None
    git_branch: Optional[str] = None
    git_commit: Optional[str] = None
    markers: list[str] = field(default_factory=list)
    readme_excerpt: Optional[str] = None
    command_guess: Optional[str] = None
    embedded_data_paths: list[str] = field(default_factory=list)
    embedded_data_summary: Optional[str] = None
    estimated_data_bytes: Optional[int] = None
    excluded_paths: list[str] = field(default_factory=list)
    confidence: Optional[float] = None
    status: str = "pending"
    created_at: str = ""
    updated_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ProjectCandidate":
        return ProjectCandidate(
            id=row["id"],
            server=row["server"],
            path=row["path"],
            name_guess=row["name_guess"],
            kind=row["kind"],
            git_remote=row["git_remote"],
            git_branch=row["git_branch"],
            git_commit=row["git_commit"],
            markers=json.loads(row["markers"] or "[]"),
            readme_excerpt=row["readme_excerpt"],
            command_guess=row["command_guess"],
            embedded_data_paths=json.loads(row["embedded_data_paths"] or "[]"),
            embedded_data_summary=row["embedded_data_summary"],
            estimated_data_bytes=row["estimated_data_bytes"],
            excluded_paths=json.loads(row["excluded_paths"] or "[]"),
            confidence=row["confidence"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class ProjectInstance:
    """`project_instances` 一列：某個已匯入專案在某台機器上實際確認過存在
    的一份副本。`id` 是 `make_instance_id(project_name, server, path)` 的
    穩定 hash。"""

    id: str
    project_name: str
    server: str
    path: str
    git_remote: Optional[str] = None
    git_branch: Optional[str] = None
    git_commit: Optional[str] = None
    dirty: bool = False
    embedded_data_paths: list[str] = field(default_factory=list)
    last_seen: str = ""
    #: 切片 1：所屬 Project 的 UUID（projects.id）雙寫;project 已被刪除的
    #: 孤兒列可能是 None。
    project_id: Optional[str] = None
    #: 切片 1 只做 schema readiness:一律 'unknown',真正的判定與轉移由
    #: 切片 2（instance reconciliation）接手。值域見 VALID_INSTANCE_STATES。
    state: str = "unknown"

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ProjectInstance":
        return ProjectInstance(
            id=row["id"],
            project_name=row["project_name"],
            server=row["server"],
            path=row["path"],
            git_remote=row["git_remote"],
            git_branch=row["git_branch"],
            git_commit=row["git_commit"],
            dirty=bool(row["dirty"]),
            embedded_data_paths=json.loads(row["embedded_data_paths"] or "[]"),
            last_seen=row["last_seen"],
            project_id=row["project_id"],
            state=row["state"] or "unknown",
        )


@dataclass
class ProjectVersion:
    """`project_versions` 一列:PLAN.md 2026-07-11 版 §14 切片 4（canonical
    version service）。某專案在某個時間點的不可變 hub commit——同一個
    `(project_name, git_commit)` 只會有一筆(見 `get_or_create_project_
    version()`),身分一經建立永不改變(同 INV-DATA-1 草案「DatasetSnapshot
    不可變」的同一精神)。"""

    id: str
    project_name: str
    git_commit: str
    project_id: Optional[str] = None
    git_ref: Optional[str] = None
    source_instance_id: Optional[str] = None
    created_at: str = ""
    metadata: Optional[dict] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ProjectVersion":
        return ProjectVersion(
            id=row["id"],
            project_name=row["project_name"],
            git_commit=row["git_commit"],
            project_id=row["project_id"],
            git_ref=row["git_ref"],
            source_instance_id=row["source_instance_id"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else None,
        )


@dataclass
class CodingRun:
    """`coding_runs` 一列：一次 Codex Worker（`codex exec`）任務的完整記錄
    （PLAN.md N.6）。`approval_id`／`project`／`runner_server`／`instruction`
    是建立時必填；其餘欄位隨任務進行陸續回填（見
    `app/approvals.py` 的 coding_task 分支與 on_job_finished hook）。"""

    id: int
    approval_id: int
    project: str
    runner_server: str
    instruction: str
    job_id: Optional[int] = None
    base_branch: Optional[str] = None
    base_commit: Optional[str] = None
    result_branch: Optional[str] = None
    result_commit: Optional[str] = None
    worktree_path: Optional[str] = None
    bundle_path: Optional[str] = None
    validation_target: Optional[str] = None
    codex_version: Optional[str] = None
    status: str = "queued"
    test_command: Optional[str] = None
    test_exit_code: Optional[int] = None
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_message: Optional[str] = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "CodingRun":
        return CodingRun(
            id=row["id"],
            approval_id=row["approval_id"],
            job_id=row["job_id"],
            project=row["project"],
            runner_server=row["runner_server"],
            instruction=row["instruction"],
            base_branch=row["base_branch"],
            base_commit=row["base_commit"],
            result_branch=row["result_branch"],
            result_commit=row["result_commit"],
            worktree_path=row["worktree_path"],
            bundle_path=row["bundle_path"],
            validation_target=row["validation_target"],
            codex_version=row["codex_version"],
            status=row["status"],
            test_command=row["test_command"],
            test_exit_code=row["test_exit_code"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_message=row["error_message"],
        )


@dataclass
class ExperimentRecord:
    """`experiment_records` 一列：專案詳情頁的手動實驗紀錄（PLAN.md「專案
    詳情頁：可點入、調派、實驗紀錄時間軸、目標／方法／進度管理」計畫第 1
    節）。人與 agent 都能補筆記；`job_id`／`coding_run_id` 是弱關聯（無
    FK，比照全案慣例），純粹讓紀錄能標註「這是針對哪一次執行/哪一次改碼
    寫的觀察」，不做參照完整性檢查。時間軸實際呈現是跟 jobs／coding_runs
    查詢時動態合併，見 `app/records.py` 的 `build_timeline()`。"""

    id: int
    project: str
    content: str
    kind: str = "note"
    title: Optional[str] = None
    author: str = "user"
    job_id: Optional[int] = None
    coding_run_id: Optional[int] = None
    extra: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ExperimentRecord":
        return ExperimentRecord(
            id=row["id"],
            project=row["project"],
            kind=row["kind"],
            title=row["title"],
            content=row["content"],
            author=row["author"],
            job_id=row["job_id"],
            coding_run_id=row["coding_run_id"],
            extra=json.loads(row["extra"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class Dataset:
    name: str
    version: str
    size_bytes: int
    source_path: str
    manifest: dict
    created_at: str = ""
    #: 階段 16（PLAN.md Q.3）：資料卡，`None` 表示這個版本是階段 16 之前
    #: 建立的（或建立時沒帶——理論上不會發生，因為 POST /datasets 強制
    #: 要求 description/method，但 `insert_dataset()` 的 `card` 參數仍是
    #: 選填，保留給既有呼叫端/測試用預設值的彈性）。
    card: Optional[dict] = None
    #: 階段 16（PLAN.md Q.1/Q.2）：'packed'（預設，Q.1 打包快照）｜
    #: 'rolling'（Q.2 滾動模式）。**本批（Q.3 資料卡）只存取這個欄位、
    #: 不使用其邏輯**——rolling 模式的實際同步行為由後續批次接手實作。
    sync_mode: str = "packed"

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Dataset":
        card_raw = row["card"]
        return Dataset(
            name=row["name"],
            version=row["version"],
            size_bytes=row["size_bytes"],
            source_path=row["source_path"],
            manifest=json.loads(row["manifest"] or "{}"),
            created_at=row["created_at"],
            card=json.loads(card_raw) if card_raw else None,
            sync_mode=row["sync_mode"] or "packed",
        )


@dataclass
class DatasetCacheEntry:
    server: str
    dataset: str
    version: str
    registered_at: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "DatasetCacheEntry":
        return DatasetCacheEntry(
            server=row["server"],
            dataset=row["dataset"],
            version=row["version"],
            registered_at=row["registered_at"],
        )


class Database:
    """薄封裝：一個 sqlite3 連線 + 一把鎖。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    #: 階段 4 新增欄位：`CREATE TABLE IF NOT EXISTS` 只對「全新資料庫」有效，
    #: 對「已經存在、舊 schema 的 jobs 表」不會補上新欄位，因此用
    #: `ALTER TABLE ... ADD COLUMN` 遷移既有 DB（PLAN.md E：「既有 DB 用
    #: ALTER TABLE 遷移」）。新建的 DB 這裡會是no-op（SCHEMA 已經包含這些欄位）。
    _JOB_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("log_size", "INTEGER"),
        ("log_size_changed_at", "TEXT"),
        ("stalled_suspect", "INTEGER NOT NULL DEFAULT 0"),
        ("stall_notified", "INTEGER NOT NULL DEFAULT 0"),
        #: 階段 13（PLAN.md N.6）：後續 train／驗證 job 可選填
        #: source_coding_run_id，指向產出它要用的 changes.bundle 的
        #: coding_runs.id。
        ("source_coding_run_id", "INTEGER"),
    )

    #: 階段 8（第一批）：同上一段說明的遷移模式，補 projects 表兩欄
    #: （PLAN.md I.1）。
    _PROJECT_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("summary", "TEXT"),
        ("dataset_mode", "TEXT NOT NULL DEFAULT 'none'"),
        #: 專案詳情頁計畫第 1 節：目標／優化方法／目前進度，三欄都是
        #: nullable 的自由文字 Markdown，既有專案一律從 NULL（「尚未填寫」）
        #: 開始，不強制回填。
        ("goal", "TEXT"),
        ("optimization_notes", "TEXT"),
        ("progress", "TEXT"),
        #: PLAN.md 2026-07-11 版 §14 切片 1：UUID 身分欄。既有列的實際
        #: UUID 值由 _init_schema() 的 backfill 逐列產生（ALTER TABLE 的
        #: DEFAULT 只能是常數,產生不了每列不同的 UUID）。
        ("id", "TEXT"),
    )

    #: 階段 16（PLAN.md Q 節）：同上一段說明的遷移模式，補 datasets 表兩欄
    #: （資料卡＋同步模式）。
    _DATASET_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("card", "TEXT"),
        ("sync_mode", "TEXT NOT NULL DEFAULT 'packed'"),
    )

    #: 切片 1：project_instances 補 project_id（UUID 雙寫）與 state
    #: （'unknown' 起步,切片 2 才有真正的判定,見 VALID_INSTANCE_STATES）。
    _INSTANCE_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("project_id", "TEXT"),
        ("state", "TEXT NOT NULL DEFAULT 'unknown'"),
    )

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            cur = self._conn.execute("PRAGMA table_info(jobs)")
            existing_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._JOB_COLUMN_MIGRATIONS:
                if col_name not in existing_cols:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")
            cur = self._conn.execute("PRAGMA table_info(projects)")
            existing_project_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._PROJECT_COLUMN_MIGRATIONS:
                if col_name not in existing_project_cols:
                    self._conn.execute(f"ALTER TABLE projects ADD COLUMN {col_name} {col_type}")
            cur = self._conn.execute("PRAGMA table_info(datasets)")
            existing_dataset_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._DATASET_COLUMN_MIGRATIONS:
                if col_name not in existing_dataset_cols:
                    self._conn.execute(f"ALTER TABLE datasets ADD COLUMN {col_name} {col_type}")
            cur = self._conn.execute("PRAGMA table_info(project_instances)")
            existing_instance_cols = {row[1] for row in cur.fetchall()}
            for col_name, col_type in self._INSTANCE_COLUMN_MIGRATIONS:
                if col_name not in existing_instance_cols:
                    self._conn.execute(
                        f"ALTER TABLE project_instances ADD COLUMN {col_name} {col_type}"
                    )
            # 切片 1 backfill：舊列補 UUID（逐列產生,只補 NULL——既有 id 一經
            # 產生永不改變）與 instance 的 project_id 雙寫;新 DB 這裡是 no-op。
            cur = self._conn.execute("SELECT name FROM projects WHERE id IS NULL")
            for (project_name,) in cur.fetchall():
                self._conn.execute(
                    "UPDATE projects SET id = ? WHERE name = ? AND id IS NULL",
                    (str(uuid.uuid4()), project_name),
                )
            self._conn.execute(
                """
                UPDATE project_instances SET project_id = (
                    SELECT id FROM projects
                    WHERE projects.name = project_instances.project_name)
                WHERE project_id IS NULL
                """
            )
            # 唯一索引不能寫進 SCHEMA：舊 DB 要先 ALTER 補欄位,executescript
            # 先跑會因欄位不存在而失敗,所以放在遷移與 backfill 之後。
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_id ON projects(id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_instances_project_id"
                " ON project_instances(project_id)"
            )
            self._conn.commit()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        self._conn.close()

    # ---- jobs CRUD -----------------------------------------------------

    def insert_job(
        self,
        command: str,
        type: str = "adhoc",
        project: Optional[str] = None,
        require_tag: Optional[str] = None,
        pin_server: Optional[str] = None,
        depends_on: Optional[list[int]] = None,
        gpus_needed: Optional[int] = None,
        priority: str = "normal",
        status: str = "queued",
        source_coding_run_id: Optional[int] = None,
    ) -> int:
        if type not in VALID_TYPES:
            raise ValueError(f"invalid job type: {type}")
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"invalid priority: {priority}")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        depends_on = depends_on or []
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs
                    (type, project, command, require_tag, pin_server,
                     depends_on, gpus_needed, status, priority, created_at,
                     source_coding_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    type,
                    project,
                    command,
                    require_tag,
                    pin_server,
                    json.dumps(depends_on),
                    gpus_needed,
                    status,
                    priority,
                    now_iso(),
                    source_coding_run_id,
                ),
            )
            return cur.lastrowid

    def get_job(self, job_id: int) -> Optional[Job]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            return Job.from_row(row) if row else None

    def list_jobs(
        self, status: Optional[str] = None, project: Optional[str] = None
    ) -> list[Job]:
        """階段 11（PLAN.md L.3）：新增選填 `project` 篩選（`WHERE
        project = ?`），可與 `status` 並用；兩者都省略時行為與既有呼叫完全
        相同（向下相容，比照 `list_approvals()` 的 `WHERE 1=1` 累加寫法）。
        """
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY id ASC"
        with self.cursor() as cur:
            cur.execute(query, params)
            return [Job.from_row(r) for r in cur.fetchall()]

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "depends_on" in fields and not isinstance(fields["depends_on"], str):
            fields["depends_on"] = json.dumps(fields["depends_on"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [job_id]
        with self.cursor() as cur:
            cur.execute(f"UPDATE jobs SET {cols} WHERE id = ?", values)

    def delete_job(self, job_id: int) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # ---- approvals CRUD -------------------------------------------------

    def insert_approval(self, kind: str, payload: dict) -> int:
        if kind not in VALID_APPROVAL_KINDS:
            raise ValueError(f"invalid approval kind: {kind}")
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approvals (kind, payload, status, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (kind, json.dumps(payload), now_iso()),
            )
            return cur.lastrowid

    def get_approval(self, approval_id: int) -> Optional[Approval]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            row = cur.fetchone()
            return Approval.from_row(row) if row else None

    def list_approvals(
        self, status: Optional[str] = None, kind: Optional[str] = None
    ) -> list[Approval]:
        query = "SELECT * FROM approvals WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY id ASC"
        with self.cursor() as cur:
            cur.execute(query, params)
            return [Approval.from_row(r) for r in cur.fetchall()]

    def update_approval(self, approval_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "payload" in fields and not isinstance(fields["payload"], str):
            fields["payload"] = json.dumps(fields["payload"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [approval_id]
        with self.cursor() as cur:
            cur.execute(f"UPDATE approvals SET {cols} WHERE id = ?", values)

    # ---- projects CRUD（階段 3）-----------------------------------------

    def insert_project(
        self,
        name: str,
        repo_or_path: str,
        dataset_name: Optional[str] = None,
        dataset_version: Optional[str] = None,
        default_command: Optional[str] = None,
        require_tag: Optional[str] = None,
        setup_cmd: Optional[str] = None,
        summary: Optional[str] = None,
        dataset_mode: str = "none",
    ) -> str:
        """名稱重複會丟 sqlite3.IntegrityError（PRIMARY KEY），呼叫端轉 400。
        切片 1 起同時產生並回傳 UUID 身分（既有呼叫端都不接回傳值,相容）。"""
        if dataset_mode not in VALID_DATASET_MODES:
            raise ValueError(f"invalid dataset_mode: {dataset_mode}")
        project_id = str(uuid.uuid4())
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects
                    (name, id, repo_or_path, dataset_name, dataset_version,
                     default_command, require_tag, setup_cmd, created_at,
                     summary, dataset_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    project_id,
                    repo_or_path,
                    dataset_name,
                    dataset_version,
                    default_command,
                    require_tag,
                    setup_cmd,
                    now_iso(),
                    summary,
                    dataset_mode,
                ),
            )
        return project_id

    def get_project(self, name: str) -> Optional[Project]:
        """切片 1 legacy name adapter：`name` 同時接受專案名稱或 UUID
        （`projects.id`）,名稱精確命中優先——所有既有 name-based 呼叫端
        行為不變,新呼叫端可直接拿 UUID 查,不必先反查名稱。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM projects WHERE name = ? OR id = ?"
                " ORDER BY (name = ?) DESC LIMIT 1",
                (name, name, name),
            )
            row = cur.fetchone()
            return Project.from_row(row) if row else None

    def list_projects(self) -> list[Project]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM projects ORDER BY name ASC")
            return [Project.from_row(r) for r in cur.fetchall()]

    def update_project(self, name: str, **fields: Any) -> None:
        """階段 8（第一批）新增：目前只有 import_project 核准分支用得到（設
        `summary`/`dataset_mode`），比照 `update_job`/`update_approval` 的
        寫法。"""
        if not fields:
            return
        if "dataset_mode" in fields and fields["dataset_mode"] not in VALID_DATASET_MODES:
            raise ValueError(f"invalid dataset_mode: {fields['dataset_mode']}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [name]
        with self.cursor() as cur:
            cur.execute(f"UPDATE projects SET {cols} WHERE name = ?", values)

    # ---- project_candidates / project_instances CRUD（階段 8，第一批）------

    def upsert_project_candidate(
        self,
        *,
        server: str,
        path: str,
        name_guess: Optional[str] = None,
        kind: str = "project",
        git_remote: Optional[str] = None,
        git_branch: Optional[str] = None,
        git_commit: Optional[str] = None,
        markers: Optional[list[str]] = None,
        readme_excerpt: Optional[str] = None,
        command_guess: Optional[str] = None,
        embedded_data_paths: Optional[list[str]] = None,
        embedded_data_summary: Optional[str] = None,
        estimated_data_bytes: Optional[int] = None,
        excluded_paths: Optional[list[str]] = None,
        confidence: Optional[float] = None,
    ) -> str:
        """用 `server+path` 的穩定 id（`make_candidate_id()`）upsert 一筆候選
        專案。**重新掃描不會覆蓋既有的 `status`**（pending/imported/
        ignored）——使用者已經做過的匯入/忽略決定不該被下一次掃描默默蓋掉；
        全新的候選（這個 id 第一次出現）才會以 `status='pending'` 建立。
        """
        cid = make_candidate_id(server, path)
        markers = markers or []
        embedded_data_paths = embedded_data_paths or []
        excluded_paths = excluded_paths or []
        now = now_iso()
        with self.cursor() as cur:
            cur.execute("SELECT id FROM project_candidates WHERE id = ?", (cid,))
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(
                    """
                    UPDATE project_candidates SET
                        name_guess = ?, kind = ?, git_remote = ?, git_branch = ?,
                        git_commit = ?, markers = ?, readme_excerpt = ?, command_guess = ?,
                        embedded_data_paths = ?, embedded_data_summary = ?,
                        estimated_data_bytes = ?, excluded_paths = ?, confidence = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name_guess,
                        kind,
                        git_remote,
                        git_branch,
                        git_commit,
                        json.dumps(markers),
                        readme_excerpt,
                        command_guess,
                        json.dumps(embedded_data_paths),
                        embedded_data_summary,
                        estimated_data_bytes,
                        json.dumps(excluded_paths),
                        confidence,
                        now,
                        cid,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO project_candidates
                        (id, server, path, name_guess, kind, git_remote, git_branch,
                         git_commit, markers, readme_excerpt, command_guess,
                         embedded_data_paths, embedded_data_summary, estimated_data_bytes,
                         excluded_paths, confidence, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        cid,
                        server,
                        path,
                        name_guess,
                        kind,
                        git_remote,
                        git_branch,
                        git_commit,
                        json.dumps(markers),
                        readme_excerpt,
                        command_guess,
                        json.dumps(embedded_data_paths),
                        embedded_data_summary,
                        estimated_data_bytes,
                        json.dumps(excluded_paths),
                        confidence,
                        now,
                        now,
                    ),
                )
        return cid

    def get_project_candidate(self, candidate_id: str) -> Optional[ProjectCandidate]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM project_candidates WHERE id = ?", (candidate_id,))
            row = cur.fetchone()
            return ProjectCandidate.from_row(row) if row else None

    def list_project_candidates(
        self,
        server: Optional[str] = None,
        status: Optional[str] = None,
        q: Optional[str] = None,
    ) -> list[ProjectCandidate]:
        """`q` 對 `name_guess`/`path`/`readme_excerpt` 做簡單 LIKE 搜尋。"""
        query = "SELECT * FROM project_candidates WHERE 1=1"
        params: list[Any] = []
        if server:
            query += " AND server = ?"
            params.append(server)
        if status:
            query += " AND status = ?"
            params.append(status)
        if q:
            query += " AND (name_guess LIKE ? OR path LIKE ? OR readme_excerpt LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like, like])
        query += " ORDER BY updated_at DESC"
        with self.cursor() as cur:
            cur.execute(query, params)
            return [ProjectCandidate.from_row(r) for r in cur.fetchall()]

    def update_project_candidate_status(self, candidate_id: str, status: str) -> None:
        if status not in VALID_CANDIDATE_STATUSES:
            raise ValueError(f"invalid candidate status: {status}")
        with self.cursor() as cur:
            cur.execute(
                "UPDATE project_candidates SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), candidate_id),
            )

    def insert_project_instance(
        self,
        *,
        project_name: str,
        server: str,
        path: str,
        git_remote: Optional[str] = None,
        git_branch: Optional[str] = None,
        git_commit: Optional[str] = None,
        dirty: bool = False,
        embedded_data_paths: Optional[list[str]] = None,
    ) -> str:
        """用 `project_name+server+path` 的穩定 id（`make_instance_id()`）
        upsert：已存在同一個 id 就更新 git 資訊與 `last_seen`，不新增一列。
        """
        iid = make_instance_id(project_name, server, path)
        embedded_data_paths = embedded_data_paths or []
        now = now_iso()
        with self.cursor() as cur:
            cur.execute("SELECT id FROM project_instances WHERE id = ?", (iid,))
            exists = cur.fetchone() is not None
            if exists:
                # 切片 1：project_id 一併補寫（子查詢查不到＝維持 NULL,
                # 孤兒列不腦補）;state 不動——那是切片 2 reconcile 的所有權。
                cur.execute(
                    """
                    UPDATE project_instances SET
                        git_remote = ?, git_branch = ?, git_commit = ?, dirty = ?,
                        embedded_data_paths = ?, last_seen = ?,
                        project_id = (SELECT id FROM projects WHERE name = ?)
                    WHERE id = ?
                    """,
                    (
                        git_remote,
                        git_branch,
                        git_commit,
                        int(bool(dirty)),
                        json.dumps(embedded_data_paths),
                        now,
                        project_name,
                        iid,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO project_instances
                        (id, project_name, project_id, server, path, git_remote,
                         git_branch, git_commit, dirty, embedded_data_paths,
                         last_seen)
                    VALUES (?, ?, (SELECT id FROM projects WHERE name = ?),
                            ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        iid,
                        project_name,
                        project_name,
                        server,
                        path,
                        git_remote,
                        git_branch,
                        git_commit,
                        int(bool(dirty)),
                        json.dumps(embedded_data_paths),
                        now,
                    ),
                )
        return iid

    def list_project_instances(self, project_name: str) -> list[ProjectInstance]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM project_instances WHERE project_name = ? ORDER BY server ASC",
                (project_name,),
            )
            return [ProjectInstance.from_row(r) for r in cur.fetchall()]

    def list_all_project_instances(self) -> list[ProjectInstance]:
        """切片 2（instance reconciliation）:背景 reconcile 迴圈一輪要掃的
        全部 instance,固定排序讓每輪順序可重現。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM project_instances ORDER BY project_name ASC, server ASC"
            )
            return [ProjectInstance.from_row(r) for r in cur.fetchall()]

    def update_instance_reconcile(
        self,
        instance_id: str,
        *,
        state: str,
        git_branch: Optional[str] = None,
        git_commit: Optional[str] = None,
        dirty: Optional[bool] = None,
        touch_last_seen: bool = False,
    ) -> None:
        """切片 2:reconcile 一輪對單一 instance 的落地。`state` 必在
        `VALID_INSTANCE_STATES`;`touch_last_seen=True`（真的觀察到 instance
        存在）才更新 git 快照三欄與 last_seen——unknown/missing 只動 state,
        git 欄位保留「最後一次確實觀察到」的事實(app/project_instances.py
        的呼叫端說明)。`instance_id` 不存在是 no-op（instance 可能在掃描與
        落地之間被專案刪除流程收走）。"""
        if state not in VALID_INSTANCE_STATES:
            raise ValueError(f"invalid instance state: {state}")
        with self.cursor() as cur:
            if touch_last_seen:
                cur.execute(
                    """
                    UPDATE project_instances SET
                        state = ?, git_branch = ?, git_commit = ?, dirty = ?,
                        last_seen = ?
                    WHERE id = ?
                    """,
                    (
                        state,
                        git_branch,
                        git_commit,
                        int(bool(dirty)),
                        now_iso(),
                        instance_id,
                    ),
                )
            else:
                cur.execute(
                    "UPDATE project_instances SET state = ? WHERE id = ?",
                    (state, instance_id),
                )

    def update_instance_git_state(
        self, instance_id: str, *, git_branch: Optional[str], git_commit: Optional[str]
    ) -> None:
        """階段 15 Phase B（PLAN.md P.2.1）：`kind=git_init` 核准成功後，把
        剛剛就地初始化出來的 branch/commit 寫回對應的 `project_instances`
        一列。刻意只更新這兩欄（不像 `insert_project_instance()` upsert 整列
        ——那個會把 `dirty`/`embedded_data_paths` 覆蓋成呼叫端傳入的預設值，
        這裡只想動 git_branch/git_commit，不動其他既有欄位）。`instance_id`
        不存在時是 no-op（呼叫端應該已經先用 `resolve_project_instance()`
        確認過存在）。"""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE project_instances SET git_branch = ?, git_commit = ? WHERE id = ?",
                (git_branch, git_commit, instance_id),
            )

    def delete_project(self, name: str) -> None:
        """階段 15 Phase B（PLAN.md P.2.4）：刪除 `projects` 一列＋該專案所有
        `project_instances` 列。**只動這兩張表，不碰任何其他資料**——
        `project_candidates` 的 `imported` 狀態不回溯（維持 imported，不會
        變回 pending，避免同一個候選被重複匯入）；`jobs` 歷史紀錄也不刪
        （保留執行歷史）。呼叫端（`app/main.py` 的 `DELETE /projects/{name}`）
        負責在呼叫這裡之前檢查有沒有 queued/running job 引用這個專案
        （409），這裡本身不做這個檢查，也絕不對任何機器發起 SSH 或動任何
        機器上的檔案（docstring 明確：這是純 DB 操作）。"""
        with self.cursor() as cur:
            cur.execute("DELETE FROM project_instances WHERE project_name = ?", (name,))
            cur.execute("DELETE FROM projects WHERE name = ?", (name,))
        # project_versions 刻意不刪:歷史 commit 紀錄比照 jobs 的既有慣例
        # 保留(上面 delete_project() docstring 的「jobs 歷史紀錄也不刪」
        # 同一精神)——之後 project_id 會是孤兒(專案已不存在),不腦補清除。

    # ---- project_versions CRUD（PLAN.md 2026-07-11 版 §14 切片 4）--------

    def get_or_create_project_version(
        self,
        project_name: str,
        git_commit: str,
        *,
        git_ref: Optional[str] = None,
        source_instance_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> ProjectVersion:
        """`(project_name, git_commit)` 已存在就原樣回傳既有列——commit 的
        身分不可變,重複呼叫(hub_sync 重跑同一個 commit、project_deploy 用
        到已經被同步過的 commit)**不**更新 `git_ref`/`source_instance_id`/
        `metadata`,不製造第二筆。找不到才新建,`project_id` 由當下查
        `projects.id` 決定(找不到對應 Project 時是 None,不腦補)。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM project_versions WHERE project_name = ? AND git_commit = ?",
                (project_name, git_commit),
            )
            row = cur.fetchone()
            if row is not None:
                return ProjectVersion.from_row(row)

            cur.execute("SELECT id FROM projects WHERE name = ?", (project_name,))
            proj_row = cur.fetchone()
            project_id = proj_row["id"] if proj_row is not None else None

            version_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO project_versions
                    (id, project_id, project_name, git_commit, git_ref,
                     source_instance_id, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    project_id,
                    project_name,
                    git_commit,
                    git_ref,
                    source_instance_id,
                    now_iso(),
                    json.dumps(metadata) if metadata is not None else None,
                ),
            )
            cur.execute("SELECT * FROM project_versions WHERE id = ?", (version_id,))
            return ProjectVersion.from_row(cur.fetchone())

    def list_project_versions(self, project_name: str) -> list[ProjectVersion]:
        """新到舊排序,供專案頁版本歷史使用。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM project_versions WHERE project_name = ?"
                " ORDER BY created_at DESC",
                (project_name,),
            )
            return [ProjectVersion.from_row(r) for r in cur.fetchall()]

    def get_project_version(self, version_id: str) -> Optional[ProjectVersion]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM project_versions WHERE id = ?", (version_id,))
            row = cur.fetchone()
            return ProjectVersion.from_row(row) if row else None

    # ---- datasets CRUD（階段 3）------------------------------------------

    def insert_dataset(
        self,
        name: str,
        version: str,
        size_bytes: int,
        source_path: str,
        manifest: dict,
        card: Optional[dict] = None,
        sync_mode: str = "packed",
    ) -> None:
        """同一個 (name, version) 重複註冊會丟 sqlite3.IntegrityError，呼叫端轉
        400——版本是一級概念（原規格 5.2），要更新內容請註冊新版本，不是覆蓋。

        階段 16（PLAN.md Q 節）：`card` 選填（`None` = 沒有資料卡，見
        `app/datasets.py` 的 `build_dataset_card()`）；`sync_mode` 選填，
        預設 `'packed'`（Q.1），本批只存取欄位、不使用其邏輯（見 Dataset
        dataclass 的欄位註解）。
        """
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO datasets
                    (name, version, size_bytes, source_path, manifest, created_at, card, sync_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    version,
                    size_bytes,
                    source_path,
                    json.dumps(manifest),
                    now_iso(),
                    json.dumps(card) if card is not None else None,
                    sync_mode,
                ),
            )

    def get_dataset(self, name: str, version: str) -> Optional[Dataset]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM datasets WHERE name = ? AND version = ?", (name, version)
            )
            row = cur.fetchone()
            return Dataset.from_row(row) if row else None

    def list_datasets(self) -> list[Dataset]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM datasets ORDER BY name ASC, version ASC")
            return [Dataset.from_row(r) for r in cur.fetchall()]

    def update_dataset_card(self, name: str, version: str, card: dict) -> None:
        """階段 16（PLAN.md Q.3）：整份 card dict 覆蓋寫入（不是逐欄
        UPDATE）——呼叫端（`app/main.py` 的 PATCH
        `/datasets/{name}/{version}/card`）負責組出完整的新 card（含保留
        原 `created_at`、更新 `updated_at`），這裡只管寫入序列化後的
        JSON。呼叫端應先 `get_dataset()` 確認資料集存在（同既有 CRUD
        慣例，這裡不檢查、不報錯，資料集不存在時是 no-op）。"""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE datasets SET card = ? WHERE name = ? AND version = ?",
                (json.dumps(card), name, version),
            )

    # ---- dataset_cache CRUD（階段 3：快取地圖）----------------------------

    def upsert_dataset_cache(self, server: str, dataset: str, version: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dataset_cache (server, dataset, version, registered_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(server, dataset, version)
                DO UPDATE SET registered_at = excluded.registered_at
                """,
                (server, dataset, version, now_iso()),
            )

    def delete_dataset_cache(self, server: str, dataset: str, version: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "DELETE FROM dataset_cache WHERE server = ? AND dataset = ? AND version = ?",
                (server, dataset, version),
            )

    def is_dataset_cached(self, server: str, dataset: str, version: str) -> bool:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM dataset_cache
                WHERE server = ? AND dataset = ? AND version = ? LIMIT 1
                """,
                (server, dataset, version),
            )
            return cur.fetchone() is not None

    def is_dataset_cached_anywhere(self, dataset: str, version: str) -> bool:
        """某個資料集＠版本是否已經同步到**任何一台**機器（不指定 server）。
        「自動」派工模式下用來判斷：這個訓練任務需要的資料集哪台機器都
        沒有，會提醒使用者（見 `app/approvals.py` 的 warning 機制），
        因為 `has_dataset` 資格過濾之後這種任務會一直卡在 queued。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM dataset_cache WHERE dataset = ? AND version = ? LIMIT 1",
                (dataset, version),
            )
            return cur.fetchone() is not None

    def list_dataset_cache(self, server: Optional[str] = None) -> list[DatasetCacheEntry]:
        with self.cursor() as cur:
            if server:
                cur.execute(
                    "SELECT * FROM dataset_cache WHERE server = ? ORDER BY dataset ASC, version ASC",
                    (server,),
                )
            else:
                cur.execute("SELECT * FROM dataset_cache ORDER BY server ASC, dataset ASC")
            return [DatasetCacheEntry.from_row(r) for r in cur.fetchall()]

    # ---- 輔助查詢：setup 任務是否已在某機完成過（階段 3）--------------------

    def has_completed_setup(self, project: str, server: str) -> bool:
        """某機是否已經成功跑過某專案的 setup 任務（type='setup' 且 status='done'）。

        用既有 `jobs` 表歷史紀錄判斷「機器是否已經跑過這個專案」，取代對工作機
        做即時 SSH 目錄探測——這樣建立核准請求時不需要 SSH（見
        app/datasets.py 的設計說明），且不需要為此另外新增一張表。
        """
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM jobs
                WHERE type = 'setup' AND project = ? AND server = ? AND status = 'done'
                LIMIT 1
                """,
                (project, server),
            )
            return cur.fetchone() is not None

    # ---- coding_runs CRUD（階段 13，PLAN.md N.6：Codex Worker v2）---------

    #: `update_coding_run()` 允許更新的欄位白名單，比照 `update_job`／
    #: `update_project` 的既有 whitelist 慣例（白名單外一律 raise
    #: ValueError）。刻意排除 `approval_id`/`project`/`runner_server`/
    #: `instruction`/`created_at`——這些是建立時就定案、不該被之後的
    #: dispatch/回填流程改動的欄位。
    _CODING_RUN_UPDATE_FIELDS = {
        "job_id",
        "base_branch",
        "base_commit",
        "result_branch",
        "result_commit",
        "worktree_path",
        "bundle_path",
        "validation_target",
        "codex_version",
        "status",
        "test_command",
        "test_exit_code",
        "started_at",
        "finished_at",
        "error_message",
    }

    def insert_coding_run(
        self,
        *,
        approval_id: int,
        project: str,
        runner_server: str,
        instruction: str,
        job_id: Optional[int] = None,
        base_branch: Optional[str] = None,
        base_commit: Optional[str] = None,
        result_branch: Optional[str] = None,
        result_commit: Optional[str] = None,
        worktree_path: Optional[str] = None,
        bundle_path: Optional[str] = None,
        validation_target: Optional[str] = None,
        codex_version: Optional[str] = None,
        status: str = "queued",
        test_command: Optional[str] = None,
        test_exit_code: Optional[int] = None,
    ) -> int:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO coding_runs
                    (approval_id, job_id, project, runner_server, instruction,
                     base_branch, base_commit, result_branch, result_commit,
                     worktree_path, bundle_path, validation_target, codex_version,
                     status, test_command, test_exit_code, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    job_id,
                    project,
                    runner_server,
                    instruction,
                    base_branch,
                    base_commit,
                    result_branch,
                    result_commit,
                    worktree_path,
                    bundle_path,
                    validation_target,
                    codex_version,
                    status,
                    test_command,
                    test_exit_code,
                    now_iso(),
                ),
            )
            return cur.lastrowid

    def get_coding_run(self, coding_run_id: int) -> Optional[CodingRun]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM coding_runs WHERE id = ?", (coding_run_id,))
            row = cur.fetchone()
            return CodingRun.from_row(row) if row else None

    def get_coding_run_by_job_id(self, job_id: int) -> Optional[CodingRun]:
        """依 `jobs.id` 找對應的 `coding_runs` 一列（PLAN.md N.6 回填）：
        `approve()` 建立 coding_run 時已經把 `job_id` 寫回去（見
        `update_coding_run(run_id, job_id=job.id)`），這裡只是反查。v1
        遺留的 coding job（沒有對應 coding_run）回傳 `None`，呼叫端
        （`app.jobfinish`）應該跳過回填、不當成錯誤。"""
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM coding_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            )
            row = cur.fetchone()
            return CodingRun.from_row(row) if row else None

    def list_coding_runs(
        self,
        status: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 50,
    ) -> list[CodingRun]:
        """新到舊（`id DESC`），比照其他「最近 N 筆」查詢的既有慣例。"""
        query = "SELECT * FROM coding_runs WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.cursor() as cur:
            cur.execute(query, params)
            return [CodingRun.from_row(r) for r in cur.fetchall()]

    def update_coding_run(self, coding_run_id: int, **fields: Any) -> None:
        if not fields:
            return
        invalid = set(fields) - self._CODING_RUN_UPDATE_FIELDS
        if invalid:
            raise ValueError(f"invalid coding_run field(s): {sorted(invalid)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [coding_run_id]
        with self.cursor() as cur:
            cur.execute(f"UPDATE coding_runs SET {cols} WHERE id = ?", values)

    # ---- experiment_records CRUD（專案詳情頁：實驗紀錄時間軸）--------------

    #: `update_experiment_record()` 允許更新的欄位白名單，比照
    #: `_CODING_RUN_UPDATE_FIELDS` 的既有慣例——刻意排除 `project`/`author`/
    #: `job_id`/`coding_run_id`/`created_at`：這些是建立時就定案的欄位，
    #: `author` 尤其不該事後被改成別的身分（見 `POST/PATCH
    #: /projects/{name}/records` 的稽核設計）。
    _EXPERIMENT_RECORD_UPDATE_FIELDS = {"title", "content", "kind"}

    def insert_experiment_record(
        self,
        project: str,
        content: str,
        kind: str = "note",
        title: Optional[str] = None,
        author: str = "user",
        job_id: Optional[int] = None,
        coding_run_id: Optional[int] = None,
        extra: Optional[dict] = None,
    ) -> int:
        if kind not in VALID_RECORD_KINDS:
            raise ValueError(f"invalid experiment record kind: {kind}")
        _validate_record_author(author)
        now = now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO experiment_records
                    (project, kind, title, content, author, job_id, coding_run_id,
                     extra, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project,
                    kind,
                    title,
                    content,
                    author,
                    job_id,
                    coding_run_id,
                    json.dumps(extra or {}),
                    now,
                    now,
                ),
            )
            return cur.lastrowid

    def get_experiment_record(self, record_id: int) -> Optional[ExperimentRecord]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM experiment_records WHERE id = ?", (record_id,))
            row = cur.fetchone()
            return ExperimentRecord.from_row(row) if row else None

    def list_experiment_records(
        self,
        project: str,
        q: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 20,
        before_ts: Optional[str] = None,
    ) -> list[ExperimentRecord]:
        """新到舊（`created_at DESC`，同一微秒內以 `id DESC` 當 tie-break）。
        `q` 對 `title`/`content` 做簡單 LIKE 搜尋；`before_ts` 是游標，
        `created_at < before_ts`——`app/records.py` 的 `build_timeline()`
        對這個游標選字串比較（而不是額外開一個 id 游標）的取捨有完整說明：
        `now_iso()` 含微秒，字典序比較足夠安全，碰撞機率可忽略。"""
        query = "SELECT * FROM experiment_records WHERE project = ?"
        params: list[Any] = [project]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        if q:
            query += " AND (title LIKE ? OR content LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like])
        if before_ts:
            query += " AND created_at < ?"
            params.append(before_ts)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.cursor() as cur:
            cur.execute(query, params)
            return [ExperimentRecord.from_row(r) for r in cur.fetchall()]

    def update_experiment_record(self, record_id: int, **fields: Any) -> None:
        """白名單 `title`/`content`/`kind`（比照 `update_coding_run()`），
        自動更新 `updated_at`（比照 `update_dataset_card()` 一帶「補登/更新
        要更新時間戳」的既有慣例）。`kind` 有帶時一樣要通過
        `VALID_RECORD_KINDS` 驗證。"""
        if not fields:
            return
        invalid = set(fields) - self._EXPERIMENT_RECORD_UPDATE_FIELDS
        if invalid:
            raise ValueError(f"invalid experiment record field(s): {sorted(invalid)}")
        if "kind" in fields and fields["kind"] not in VALID_RECORD_KINDS:
            raise ValueError(f"invalid experiment record kind: {fields['kind']}")
        fields = dict(fields)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [record_id]
        with self.cursor() as cur:
            cur.execute(f"UPDATE experiment_records SET {cols} WHERE id = ?", values)

    def delete_experiment_record(self, record_id: int) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM experiment_records WHERE id = ?", (record_id,))
