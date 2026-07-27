"""白名單工具註冊表（PLAN.md H 節）——這是 agent 的安全邊界。

`TOOLS: dict[str, ToolSpec]` 是唯一的分派來源：`app/agent_runtime.py` 的
tool loop **只查這張表**（`name in TOOLS` 判斷存不存在、`TOOLS[name].handler`
呼叫），不用 `getattr()`／`eval()`／字串組指令去動態呼叫任何東西。

鐵律（違反＝實作錯誤）：**本模組不得 `import app.sshpool`、
`import app.localrun`、`subprocess`，也不得呼叫 `os.system`**——所有 handler
只透過既有 Python 函式（`app.chat`／`app.db`／`app.approvals`／`app.audit`／
`app.llm_local`）讀寫，唯讀工具不碰任何即時 SSH，寫入工具一律只建立
`approvals` 表的核准請求（`app.approvals.request_enqueue_approval()` /
`request_stop_approval()`），絕不直接入列、絕不直接停止任務。
`tests/test_agent_tools.py` 有一條測試靜態掃這個模組的原始碼／
`sys.modules`，釘住這條鐵律。

`job_log` 工具只回傳 DB 裡存好的 `log_tail`（哨兵協議 reconcile 或
stop 核准時寫入的那份），**不做即時 SSH 抓 log**——要看即時 log 請使用者
自己去任務分頁看（`GET /jobs/{id}/log`），工具層不碰 SSH。

**例外（階段 8 第二批／階段 11／階段 12，已知、刻意的例外，不是違反上一
段）**：`test_server_ssh`（唯讀測試一組機器設定的 SSH 連線）、
`get_project_activity`（PLAN.md L 節，階段 11）、`list_project_files`／
`read_project_file`（PLAN.md M.1 節，階段 12）**會**觸發真正的、唯讀的即時
SSH——前者呼叫 `ctx.ssh_run_direct`，其餘呼叫 `app.activity` 的
`probe_instance()`／`list_instance_files()`／`read_instance_file()`（進而
呼叫 `ctx.ssh_run`）。這幾個是本模組會碰即時 SSH 的工具：只探測/讀取該
專案在 DB 已登記的 `project_instances` 路徑（絕不接受任意路徑），
`read_project_file` 額外對 `file_path` 做路徑穿越/絕對路徑/秘密檔名驗證
（`app.activity.validate_rel_path()`，驗證失敗直接回錯誤，不 SSH），沒有
`ctx.ssh_run` 或機器離線時直接回錯誤（不拋例外）——安全邊界細節見
`app/activity.py` 模組 docstring。

**沒有 approve/reject 工具，沒有自由 shell 工具。**

**PLAN.md M.2 裁定**：`request_apply_patch`（建立 diff 核准請求的寫入
工具）**只加在 MCP bridge（ChatGPT），不加進本模組（vLLM）**——7B 模型
寫 diff 品質不可靠，只會製造核准垃圾。本模組因此**沒有** apply_patch 相關
的任何工具（`tests/test_apply_patch.py` 有一條測試靜態掃描本模組，釘住
這條鐵律）。

**專案詳情頁計畫（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、
目標／方法／進度管理」）第 4 節新增、正式登記的鐵律例外**：
`add_experiment_record`／`update_project_doc` 這兩個寫入工具**不建立
approval**，直接執行——跟「寫入工具一律只建立核准請求」的鐵律第 2 條
表面上矛盾，但這是刻意的、有明確理由的例外，比照
`POST /projects/{name}/records`／`PATCH /projects/{name}`（`app/main.py`）
兩支網頁端點本身就是直接執行、不走核准的既有設計（先例是資料集資料卡
`PATCH /datasets/{name}/{version}/card`：純 DB 文字寫入，不觸發任何機器
動作、可逆）：
- `add_experiment_record` 只會寫入 `experiment_records` 表的一列 Markdown
  文字，**強制 `author="agent"`**（呼叫端不能自己填別的值，見
  `_tool_add_experiment_record()`），不會派工、不會碰任何機器、能刪能改
  （可逆）。
- `update_project_doc` 只會更新 `projects` 表的 `goal`／`optimization_
  notes`／`progress` 三欄其中之一（白名單，見 `_tool_update_project_doc()`
  ），同樣是純 DB 文字覆蓋、可逆（改壞了人可以再改回去或看歷史紀錄
  `events` 工具查稽核）。
- 兩者都不會觸發任何 SSH、任何任務派工、任何核准繞過——`enqueue`/`stop`
  這兩種真正有「執行力」（會在機器上跑指令）的動作，鐵律第 2 條完全不受
  影響，一律仍然只能建立 approval。
- 兩個 handler 都呼叫 `app.audit.append_audit()` 留下稽核紀錄
  （`experiment_record_created`／`project_doc_updated`），跟人工透過網頁
  操作留下一樣的軌跡，方便事後追查是 agent 自己補的筆記還是人填的。
- `tests/test_agent_tools.py` 除了既有的鐵律靜態掃描（不 import
  app.sshpool/app.localrun/subprocess），也涵蓋這兩個工具「不建立
  approval、直接寫進 DB」的行為斷言。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app.activity import (
    ProjectInstanceResolutionError,
    list_instance_files,
    probe_instance,
    read_instance_file,
    resolve_project_instance,
    validate_rel_path,
)
from app.approvals import (
    CandidateNotFoundError,
    CandidateNotPendingError,
    ForbiddenScanRootError,
    InvalidServerConfigError,
    JobNotFoundError,
    JobNotRunningError,
    ProjectNotFoundError,
    ServerNotFoundError,
    ServerRenameNotSupportedError,
    approval_to_dict,
    maybe_auto_approve,
    request_enqueue_approval,
    request_ignore_project_candidate_approval,
    request_import_project_approval,
    request_inventory_scan_approval,
    request_server_add_approval,
    request_server_disable_approval,
    request_server_update_approval,
    request_stop_approval,
)
from app.audit import append_audit, audit_actor_from_request_context, tail_audit
from app.autoapprove import get_rules
from app.chat import build_status_reply
from app.config import AppConfig, ServerConfig
from app.datasets import NO_CARD_NOTE, build_dataset_auto_facts, render_dataset_card
from app.db import (
    Approval,
    Database,
    Job,
    Project,
    ProjectCandidate,
    ProjectInstance,
    VALID_RECORD_KINDS,
)
from app.engineering_tasks import redact_engineering_text
from app.jobqueue import DangerousCommandError
from app.records import build_timeline
from app.server_config import server_config_to_safe_dict, test_ssh_connection
from app import llm_local
from app.authorization_catalog import LOCAL_TOOL_AUTHORIZATION, LOCAL_TOOL_RESOURCES
from app.authorization_shadow import collect_shadow_evidence, emit_shadow_evidence
from app.identity import RequestContext

#: 專案詳情頁計畫第 4 節：`update_project_doc` 工具允許更新的欄位白名單
#: （只有這三個自由文字 Markdown 欄位開放給 agent 改，`name`／
#: `repo_or_path`／`dataset_mode` 等結構性欄位不開放）。
_PROJECT_DOC_FIELDS = {"goal", "optimization_notes", "progress"}


@dataclass
class AgentContext:
    """工具 handler 執行時可以用到的一切，取代逐一傳一堆散裝參數。"""

    db: Database
    server_states: dict
    config: AppConfig
    audit_path: str = "audit.jsonl"
    #: 呼叫本地 vLLM（`vllm_health` 工具）用的 `httpx.AsyncClient`，測試可
    #: 注入假 client；`None` 時 `app.llm_local` 自己開短命 client。
    http_client: Optional[Any] = None
    #: 階段 8（第二批）：`server_configs`（server name -> app.config.ServerConfig）
    #: 給 `scan_project_inventory`（project_roots 自動代入用）與
    #: `list_server_configs`/`get_server_config`/`request_update_server`
    #: 用；呼叫端（`app/main.py`）傳入 `app_state.server_configs`。
    server_configs: Optional[dict] = None
    #: 階段 8（第二批，legacy）：舊版 `test_server_ssh` 工具曾經用它「按名字」
    #: 查現有 server_configs 送 SSH（`ssh_run(name, command, timeout)`），
    #: 但這樣會忽略 payload 帶的 host/port/user/key，測不到「還沒加入
    #: servers.yaml 的機器」——這是一個已修正的 bug（見 `ssh_run_direct`）。
    #: 保留這個欄位是因為 `app.agent_runtime.run_agent()` 建構 `AgentContext`
    #: 時仍會傳入（其他呼叫端可能還依賴這個介面形狀），但
    #: `_tool_test_server_ssh` 已經不再讀它。
    ssh_run: Optional[Any] = None
    #: 階段 8（第二批，bug 修正）：`test_server_ssh` 工具唯讀直接執行（不建
    #: approval），需要用 payload 組出的 `ServerConfig` 真的測 SSH，**不能**
    #: 按 name 去查 `server_configs`（否則新機器/還沒核准的設定會被忽略，
    #: 見 `app/main.py` 的 `test_server_ssh_endpoint` docstring）。呼叫端
    #: （`app/main.py`）注入 `app_state.ssh_pool.run`（介面
    #: `(server_cfg, command, timeout) -> CommandResult`），本模組**不**
    #: import sshpool，只是呼叫端注入的 callable，不違反鐵律。
    ssh_run_direct: Optional[Any] = None
    #: Goal 1 / Slice 3: immutable principal propagated to every tool handler.
    #: Policy is not evaluated until the later shadow-mode slice.  The default
    #: preserves construction by existing tests and non-request callers.
    request_context: Optional[RequestContext] = None


ToolHandler = Callable[[dict, AgentContext], Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    #: 給 prompt 用的簡述：`{arg_name: "型別/是否必填/說明"}`，不是嚴格的
    #: JSON Schema，夠讓模型知道怎麼填就好。
    args: dict[str, str] = field(default_factory=dict)
    handler: Optional[ToolHandler] = None
    #: Goal 1 / Slice 2 metadata only. Runtime evaluation is introduced by a
    #: later shadow-mode slice; this field must not affect dispatch behavior.
    authorization_action: Optional[str] = None
    authorization_resource: Optional[str] = None


# ---------------------------------------------------------------------------
# 小工具：安全地把使用者/模型給的值轉成 int，並夾在 [lo, hi] 範圍內
# ---------------------------------------------------------------------------


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


def _job_summary(job: Job) -> dict:
    command = (
        _engineering_job_command_label(job)
        if _engineering_protected_job(job)
        else job.command or ""
    )
    hint = command[:80] + ("…" if len(command) > 80 else "")
    return {
        "id": job.id,
        "type": job.type,
        "project": job.project,
        "command": hint,
        "status": job.status,
        "server": job.server,
        "priority": job.priority,
        "created_at": job.created_at,
    }


def _job_detail(job: Job) -> dict:
    command = (
        _engineering_job_command_label(job)
        if _engineering_protected_job(job)
        else job.command
    )
    return {
        "id": job.id,
        "type": job.type,
        "project": job.project,
        "command": command,
        "require_tag": job.require_tag,
        "pin_server": job.pin_server,
        "depends_on": job.depends_on,
        "gpus_needed": job.gpus_needed,
        "status": job.status,
        "server": job.server,
        "priority": job.priority,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "exit_code": job.exit_code,
        "target_server": job.target_server,
        "dataset_name": job.dataset_name,
        "dataset_version": job.dataset_version,
        "stalled_suspect": bool(job.stalled_suspect),
        "engineering_task_id": job.engineering_task_id,
        "engineering_task_role": job.engineering_task_role,
        "engineering_validation_request_id": (
            job.engineering_validation_request_id
        ),
    }


def _engineering_protected_job(job: Job) -> bool:
    return (
        job.engineering_task_id is not None
        or job.engineering_validation_request_id is not None
    )


def _engineering_job_command_label(job: Job) -> str:
    if job.engineering_validation_request_id is not None:
        return (
            "Push verified Engineering Task bundle to approved worker"
            if job.type == "sync"
            else "Run approved Engineering Task worker validation"
        )
    return {
        "staging": "Prepare immutable Engineering Task inputs",
        "coding": "Run Codex agent in an isolated worktree",
        "validation": "Run approved Engineering Task validation",
    }.get(job.engineering_task_role or "", "Run Engineering Task step")


def _safe_engineering_job_log(job: Job) -> str:
    preview = redact_engineering_text(job.log_tail or "")
    if preview["withheld"]:
        return "（任務日誌含敏感內容，已隱藏）"
    return preview["content"] or ""


def _approval_summary(approval: Approval) -> dict:
    return approval_to_dict(approval)


def _candidate_summary(c: ProjectCandidate) -> dict:
    return {
        "id": c.id,
        "server": c.server,
        "path": c.path,
        "name_guess": c.name_guess,
        "markers": c.markers,
        "confidence": c.confidence,
        "status": c.status,
        "estimated_data_bytes": c.estimated_data_bytes,
        "updated_at": c.updated_at,
    }


def _candidate_detail(c: ProjectCandidate) -> dict:
    return {
        "id": c.id,
        "server": c.server,
        "path": c.path,
        "name_guess": c.name_guess,
        "kind": c.kind,
        "git_remote": c.git_remote,
        "git_branch": c.git_branch,
        "git_commit": c.git_commit,
        "markers": c.markers,
        "readme_excerpt": c.readme_excerpt,
        "command_guess": c.command_guess,
        "embedded_data_paths": c.embedded_data_paths,
        "embedded_data_summary": c.embedded_data_summary,
        "estimated_data_bytes": c.estimated_data_bytes,
        "excluded_paths": c.excluded_paths,
        "confidence": c.confidence,
        "status": c.status,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _truncate_doc(text: Optional[str], limit: int = 300) -> Optional[str]:
    """`_project_summary()` 專用：目標／優化方法／進度可能是很長的
    Markdown，摘要工具只需要讓 agent 知道「有沒有填、大概寫什麼」，截
    ~300 字（比照 `app/records.py` 的 `_truncate()` 200 字上限，這裡稍微
    寬一點，因為是專案層級的單一摘要而不是時間軸列表項）。"""
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _project_summary(p: Project) -> dict:
    return {
        "name": p.name,
        "repo_or_path": p.repo_or_path,
        "dataset_name": p.dataset_name,
        "dataset_version": p.dataset_version,
        "default_command": p.default_command,
        "require_tag": p.require_tag,
        "setup_cmd": p.setup_cmd,
        "summary": p.summary,
        "dataset_mode": p.dataset_mode,
        "created_at": p.created_at,
        #: 專案詳情頁計畫第 4 節：目標／優化方法／目前進度摘要（截 ~300
        #: 字），`None`＝尚未填寫。
        "goal": _truncate_doc(p.goal),
        "optimization_notes": _truncate_doc(p.optimization_notes),
        "progress": _truncate_doc(p.progress),
    }


def _instance_summary(i: ProjectInstance) -> dict:
    return {
        "id": i.id,
        "project_name": i.project_name,
        "server": i.server,
        "path": i.path,
        "git_remote": i.git_remote,
        "git_branch": i.git_branch,
        "git_commit": i.git_commit,
        "dirty": i.dirty,
        "embedded_data_paths": i.embedded_data_paths,
        "last_seen": i.last_seen,
    }


def _server_state_summary(state, db: Optional[Database]) -> dict:
    d = {
        "name": state.name,
        "online": state.online,
        "load1": state.load1,
        "gpu_util_max": state.gpu_util_max,
        "gpu_count": state.gpu_count,
        "disk_avail_bytes": state.disk_avail_bytes,
        "updated_at": state.updated_at,
        "error": state.error,
    }
    if db is not None:
        d["cached_datasets"] = [
            f"{c.dataset}@{c.version}" for c in db.list_dataset_cache(server=state.name)
        ]
    else:
        d["cached_datasets"] = []
    return d


# ---------------------------------------------------------------------------
# 唯讀工具
# ---------------------------------------------------------------------------


async def _tool_status(args: dict, ctx: AgentContext) -> str:
    return build_status_reply(ctx.server_states)


async def _tool_servers(args: dict, ctx: AgentContext) -> list[dict]:
    return [_server_state_summary(s, ctx.db) for s in ctx.server_states.values()]


async def _tool_jobs(args: dict, ctx: AgentContext) -> list[dict]:
    status = args.get("status") or None
    project = args.get("project") or None
    limit = _clamp_int(args.get("limit"), default=20, lo=1, hi=20)
    jobs = ctx.db.list_jobs(status=status, project=project)
    return [_job_summary(j) for j in jobs[-limit:]]


async def _tool_job_detail(args: dict, ctx: AgentContext) -> dict:
    job_id = args.get("job_id")
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return {"error": "job_id 必須是整數"}
    job = ctx.db.get_job(job_id)
    if job is None:
        return {"error": f"job {job_id} 不存在"}
    return _job_detail(job)


async def _tool_job_log(args: dict, ctx: AgentContext) -> dict:
    """**只回 DB 存的 `log_tail`**，不做即時 SSH（工具層禁止碰 SSH）。"""
    job_id = args.get("job_id")
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return {"error": "job_id 必須是整數"}
    job = ctx.db.get_job(job_id)
    if job is None:
        return {"error": f"job {job_id} 不存在"}
    lines = _clamp_int(args.get("lines"), default=40, lo=1, hi=80)
    log_tail = (
        _safe_engineering_job_log(job)
        if _engineering_protected_job(job)
        else job.log_tail or ""
    )
    tail_lines = log_tail.splitlines()[-lines:]
    return {
        "job_id": job_id,
        "status": job.status,
        "log_tail": "\n".join(tail_lines) or "（無存檔 log，若任務執行中請到任務分頁看即時 log）",
    }


async def _tool_approvals(args: dict, ctx: AgentContext) -> list[dict]:
    status = args.get("status") or None
    return [_approval_summary(a) for a in ctx.db.list_approvals(status=status)]


async def _tool_events(args: dict, ctx: AgentContext) -> list[dict]:
    n = _clamp_int(args.get("n"), default=20, lo=1, hi=50)
    return tail_audit(ctx.audit_path, n=n)


async def _tool_gpu(args: dict, ctx: AgentContext) -> list[dict]:
    result = []
    for state in ctx.server_states.values():
        result.append(
            {
                "name": state.name,
                "online": state.online,
                "gpu_count": state.gpu_count,
                "gpu_util_max": state.gpu_util_max,
                "gpus": [
                    {
                        "util_percent": g.util_percent,
                        "mem_used_mb": g.mem_used_mb,
                        "mem_total_mb": g.mem_total_mb,
                    }
                    for g in state.gpus
                ],
            }
        )
    return result


async def _tool_vllm_health(args: dict, ctx: AgentContext) -> dict:
    return await llm_local.health_check(ctx.config, client=ctx.http_client)


# ---------------------------------------------------------------------------
# 唯讀工具：資料集資料卡（階段 16，PLAN.md Q.3 節）
# ---------------------------------------------------------------------------


async def _tool_get_dataset_card(args: dict, ctx: AgentContext) -> dict:
    """一對一映射 `GET /datasets/{name}/{version}/card` 的邏輯（唯讀，直接
    查 DB，不碰 SSH）：`card` 可能是 `None`（階段 16 之前建立、或尚未
    補登的版本沒有卡，這不是錯誤——`auto_facts`／`rendered` 仍會提供，
    `note` 明確標示「未登記資料卡」，見工具描述「不得推測」的規則）。"""
    name = args.get("name")
    version = args.get("version")
    if not name or not version:
        return {"error": "缺少 name 或 version"}
    dataset = ctx.db.get_dataset(name, version)
    if dataset is None:
        return {"error": f"資料集 {name}@{version} 不存在"}
    auto_facts = build_dataset_auto_facts(ctx.db, dataset)
    rendered = render_dataset_card(name, version, dataset.card, auto_facts)
    note = None if dataset.card is not None else NO_CARD_NOTE
    return {
        "name": name,
        "version": version,
        "card": dataset.card,
        "auto_facts": auto_facts,
        "rendered": rendered,
        "note": note,
    }


# ---------------------------------------------------------------------------
# 唯讀工具：Project Inventory（階段 8 第一批，PLAN.md I 節）
# ---------------------------------------------------------------------------


async def _tool_list_project_candidates(args: dict, ctx: AgentContext) -> list[dict]:
    server = args.get("server") or None
    status = args.get("status") or "pending"
    candidates = ctx.db.list_project_candidates(server=server, status=status)
    return [_candidate_summary(c) for c in candidates]


async def _tool_get_project_candidate(args: dict, ctx: AgentContext) -> dict:
    candidate_id = args.get("candidate_id")
    if not candidate_id:
        return {"error": "缺少 candidate_id"}
    candidate = ctx.db.get_project_candidate(str(candidate_id))
    if candidate is None:
        return {"error": f"candidate {candidate_id} 不存在"}
    return _candidate_detail(candidate)


async def _tool_search_projects(args: dict, ctx: AgentContext) -> list[dict]:
    """比對 `projects.name`／`summary`／`project_instances.path`（不分大小
    寫，簡單子字串比對，非全文檢索）。"""
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "缺少 query"}
    q_lower = query.lower()
    matched: list[dict] = []
    for p in ctx.db.list_projects():
        hit = q_lower in p.name.lower() or (p.summary is not None and q_lower in p.summary.lower())
        if not hit:
            instances = ctx.db.list_project_instances(p.name)
            hit = any(q_lower in (i.path or "").lower() for i in instances)
        if hit:
            matched.append(_project_summary(p))
    return matched


async def _tool_get_project_profile(args: dict, ctx: AgentContext) -> dict:
    name = args.get("project_name")
    if not name:
        return {"error": "缺少 project_name"}
    project = ctx.db.get_project(name)
    if project is None:
        return {"error": f"專案 {name} 不存在"}
    instances = ctx.db.list_project_instances(name)
    return {
        "project": _project_summary(project),
        "instances": [_instance_summary(i) for i in instances],
    }


async def _tool_list_project_instances(args: dict, ctx: AgentContext) -> list[dict]:
    name = args.get("project_name")
    if not name:
        return {"error": "缺少 project_name"}
    return [_instance_summary(i) for i in ctx.db.list_project_instances(name)]


async def _tool_get_project_activity(args: dict, ctx: AgentContext) -> dict:
    """階段 11（PLAN.md L 節）：某個已註冊專案的完整執行近況——見模組
    docstring 的「例外」說明，這是本模組唯二會觸發即時 SSH 的唯讀工具之一
    （另一個是 `test_server_ssh`）。只探測 DB 已登記的 `project_instances`
    路徑，離線的 instance 或沒有 `ctx.ssh_run` 時一律標記 `skipped:
    "offline"`（不拋例外），安全邊界細節見 `app/activity.py`。"""
    name = args.get("project_name")
    if not name:
        return {"error": "缺少 project_name"}
    project = ctx.db.get_project(name)
    if project is None:
        return {"error": f"專案 {name} 不存在"}

    instances = ctx.db.list_project_instances(name)
    jobs = ctx.db.list_jobs(project=name)
    recent_jobs = [_job_summary(j) for j in jobs[-10:]]
    latest_job_log_tail = (
        _safe_engineering_job_log(jobs[-1])
        if jobs and jobs[-1].engineering_task_id is not None
        else jobs[-1].log_tail if jobs else None
    )

    server_states = ctx.server_states or {}
    server_configs = ctx.server_configs or {}

    activity: Any
    if not instances:
        activity = (
            f"專案 {name} 沒有已登記的機器/路徑（project_instances 為空），"
            "無法探測執行近況——請先透過 inventory scan／匯入流程登記至少一台機器。"
        )
    else:
        activity = []
        for inst in instances:
            state = server_states.get(inst.server)
            online = bool(getattr(state, "online", False)) if state is not None else False
            if ctx.ssh_run is None or not online:
                activity.append(
                    {
                        "instance_id": inst.id,
                        "server": inst.server,
                        "path": inst.path,
                        "skipped": "offline",
                    }
                )
                continue
            server_cfg = server_configs.get(inst.server)
            exclude_names = (
                server_cfg.project_exclude_names if server_cfg is not None else None
            )
            probe_result = await probe_instance(ctx.ssh_run, inst.server, inst.path, exclude_names)
            activity.append(
                {
                    "instance_id": inst.id,
                    "server": inst.server,
                    "path": inst.path,
                    **probe_result,
                }
            )

    return {
        "project": _project_summary(project),
        "instances": [_instance_summary(i) for i in instances],
        "recent_jobs": recent_jobs,
        "latest_job_log_tail": latest_job_log_tail,
        "activity": activity,
    }


# ---------------------------------------------------------------------------
# 唯讀工具：AI 改碼層次一——讀檔（階段 12，PLAN.md M.1 節）
# ---------------------------------------------------------------------------


async def _tool_list_project_files(args: dict, ctx: AgentContext) -> dict:
    """列出某個已註冊專案在指定機器（省略且剛好一個 instance 時自動選；
    多個 instance 時要求指定 `server`）上的檔案（相對路徑，秘密檔已過濾，
    至多 200 筆）。`subdir` 選填，含 `..`/絕對路徑一律拒絕，不嘗試 SSH。"""
    name = args.get("project_name")
    if not name:
        return {"error": "缺少 project_name"}
    project = ctx.db.get_project(name)
    if project is None:
        return {"error": f"專案 {name} 不存在"}

    try:
        instance = resolve_project_instance(ctx.db, name, args.get("server") or None)
    except ProjectInstanceResolutionError as exc:
        return {"error": str(exc)}

    target_path = instance.path
    subdir = args.get("subdir") or None
    if subdir:
        err = validate_rel_path(subdir)
        if err is not None:
            return {"error": err}
        target_path = f"{instance.path.rstrip('/')}/{subdir}"

    if ctx.ssh_run is None:
        return {"error": "此環境未提供 ssh_run，無法列出檔案"}

    server_configs = ctx.server_configs or {}
    server_cfg = server_configs.get(instance.server)
    exclude_names = server_cfg.project_exclude_names if server_cfg is not None else None
    result = await list_instance_files(ctx.ssh_run, instance.server, target_path, exclude_names)
    return {"server": instance.server, "path": target_path, **result}


async def _tool_read_project_file(args: dict, ctx: AgentContext) -> dict:
    """讀取某個已註冊專案在指定機器上的單一檔案內容（前 64KB）。
    `file_path` 是相對 instance 路徑的相對路徑——含 `..`/絕對路徑/秘密檔名
    一律拒絕，不嘗試 SSH（`app.activity.read_instance_file()` 內部先驗證，
    驗證失敗直接回錯誤 dict）。"""
    name = args.get("project_name")
    if not name:
        return {"error": "缺少 project_name"}
    project = ctx.db.get_project(name)
    if project is None:
        return {"error": f"專案 {name} 不存在"}
    file_path = args.get("file_path")
    if not file_path:
        return {"error": "缺少 file_path"}

    try:
        instance = resolve_project_instance(ctx.db, name, args.get("server") or None)
    except ProjectInstanceResolutionError as exc:
        return {"error": str(exc)}

    if ctx.ssh_run is None:
        return {"error": "此環境未提供 ssh_run，無法讀取檔案"}

    result = await read_instance_file(ctx.ssh_run, instance.server, instance.path, file_path)
    return {"server": instance.server, "path": file_path, **result}


# ---------------------------------------------------------------------------
# 寫入工具：全部只建立 approval，絕不直接入列/停止（鐵律第 2 條）
# ---------------------------------------------------------------------------


async def _tool_request_enqueue_job(args: dict, ctx: AgentContext) -> dict:
    """階段 10（PLAN.md K.1/K.3）：`source="vllm"`——建立成功後諮詢自動核准
    規則（使用者預先寫好的確定性規則，不是模型自己核准，見
    `app.autoapprove` 模組 docstring）；命中就回傳 `auto_approved: True`
    讓模型如實轉述「已排入佇列」，未命中維持既有 `{"approval": {...}}`
    （待核准）形狀。"""
    command = (args.get("command") or "").strip()
    if not command:
        return {"error": "缺少 command：請告訴我要執行的完整指令"}
    try:
        approval = request_enqueue_approval(
            ctx.db,
            command=command,
            type=args.get("type") or "adhoc",
            project=args.get("project") or None,
            require_tag=args.get("require_tag") or None,
            pin_server=args.get("pin_server") or None,
            priority=args.get("priority") or "normal",
            source="vllm",
            audit_path=ctx.audit_path,
            request_context=ctx.request_context,
        )
    except DangerousCommandError as exc:
        # 既有行為（同 app/chat.py）：危險指令轉成工具結果文字，不建立
        # approval、不給核准機會。
        return {"rejected": True, "reason": f"指令被拒絕：{exc}"}
    except ProjectNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    rules = get_rules(ctx.config.auto_approve_rules_path)
    result = await maybe_auto_approve(
        ctx.db,
        approval,
        source="vllm",
        rules=rules,
        server_configs=ctx.server_configs,
        audit_path=ctx.audit_path,
        request_context=ctx.request_context,
    )
    if result is not None:
        return {"approval": approval_to_dict(result["approval"]), "auto_approved": True}
    return {"approval": approval_to_dict(approval)}


async def _tool_request_stop_job(args: dict, ctx: AgentContext) -> dict:
    """階段 10：同 `_tool_request_enqueue_job()`；stop 的自動核准需要
    `ctx.ssh_run`，沒有的話 `maybe_auto_approve()` 會回傳 `None`（保持
    pending，不報錯），見 `app.approvals.maybe_auto_approve()` docstring。"""
    job_id = args.get("job_id")
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return {"error": "job_id 必須是整數"}
    try:
        approval = request_stop_approval(
            ctx.db,
            job_id,
            source="vllm",
            audit_path=ctx.audit_path,
            request_context=ctx.request_context,
        )
    except JobNotFoundError as exc:
        return {"error": str(exc)}
    except JobNotRunningError as exc:
        return {"error": str(exc)}

    rules = get_rules(ctx.config.auto_approve_rules_path)
    result = await maybe_auto_approve(
        ctx.db,
        approval,
        source="vllm",
        rules=rules,
        ssh_run=ctx.ssh_run,
        server_configs=ctx.server_configs,
        audit_path=ctx.audit_path,
        request_context=ctx.request_context,
    )
    if result is not None:
        return {"approval": approval_to_dict(result["approval"]), "auto_approved": True}
    return {"approval": approval_to_dict(approval)}


async def _tool_request_rerun_job(args: dict, ctx: AgentContext) -> dict:
    """讀原 job，只複製 command/type/project/require_tag/pin_server/priority
    這些「派工參數」，**不複製** server/started_at/finished_at/exit_code/
    depends_on/gpus_needed 等執行期／依賴期欄位——重跑是一次全新的派工
    請求，不是恢復舊任務的執行狀態。"""
    job_id = args.get("job_id")
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return {"error": "job_id 必須是整數"}
    job = ctx.db.get_job(job_id)
    if job is None:
        return {"error": f"job {job_id} 不存在"}
    if _engineering_protected_job(job):
        return {
            "error": (
                "AI 工程任務的內部 Job 不能透過通用重跑；"
                "請使用工程任務的安全重試流程（尚未啟用）"
            )
        }
    try:
        approval = request_enqueue_approval(
            ctx.db,
            command=job.command,
            type=job.type,
            project=job.project,
            require_tag=job.require_tag,
            pin_server=job.pin_server,
            priority=job.priority,
            audit_path=ctx.audit_path,
            request_context=ctx.request_context,
        )
    except DangerousCommandError as exc:
        return {"rejected": True, "reason": f"指令被拒絕：{exc}"}
    except ProjectNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}
    return {"approval": approval_to_dict(approval)}


# ---------------------------------------------------------------------------
# 寫入工具：Project Inventory（階段 8 第一批）——全部只建立 approval，
# LLM 不能直接匯入/忽略候選專案，也不能直接觸發掃描。
# ---------------------------------------------------------------------------


async def _tool_scan_project_inventory(args: dict, ctx: AgentContext) -> dict:
    """階段 8 第二批：`project_roots` 改為選填——沒帶時從
    `ctx.server_configs[server].project_roots` 自動代入；`server="all"` 現在
    真的支援（對每一台 enabled 機器各自建立一筆 approval）。"""
    server = args.get("server")
    if not server:
        return {"error": "缺少 server"}
    project_roots = args.get("project_roots")
    if project_roots is not None and not isinstance(project_roots, list):
        return {"error": "project_roots 必須是字串列表"}
    try:
        result = request_inventory_scan_approval(
            ctx.db,
            server,
            project_roots,
            server_configs=ctx.server_configs,
            audit_path=ctx.audit_path,
            request_context=ctx.request_context,
        )
    except ForbiddenScanRootError as exc:
        # 同既有 request_enqueue_job 對危險指令的處理風格：轉成 rejected
        # 結果文字，不建立 approval、不給核准機會。
        return {"rejected": True, "reason": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}
    if isinstance(result, list):
        return {"approvals": [approval_to_dict(a) for a in result]}
    return {"approval": approval_to_dict(result)}


async def _tool_request_import_project_candidate(args: dict, ctx: AgentContext) -> dict:
    candidate_id = args.get("candidate_id")
    if not candidate_id:
        return {"error": "缺少 candidate_id"}
    overrides = args.get("overrides") or {}
    if not isinstance(overrides, dict):
        return {"error": "overrides 必須是一個 JSON 物件"}
    try:
        approval = request_import_project_approval(
            ctx.db,
            str(candidate_id),
            overrides,
            audit_path=ctx.audit_path,
            request_context=ctx.request_context,
        )
    except CandidateNotFoundError as exc:
        return {"error": str(exc)}
    except CandidateNotPendingError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}
    return {"approval": approval_to_dict(approval)}


async def _tool_request_ignore_project_candidate(args: dict, ctx: AgentContext) -> dict:
    candidate_id = args.get("candidate_id")
    if not candidate_id:
        return {"error": "缺少 candidate_id"}
    try:
        approval = request_ignore_project_candidate_approval(
            ctx.db,
            str(candidate_id),
            audit_path=ctx.audit_path,
            request_context=ctx.request_context,
        )
    except CandidateNotFoundError as exc:
        return {"error": str(exc)}
    except CandidateNotPendingError as exc:
        return {"error": str(exc)}
    return {"approval": approval_to_dict(approval)}


# ---------------------------------------------------------------------------
# 唯讀工具：Web Server Management（階段 8 第二批，PLAN.md I 節）
# ---------------------------------------------------------------------------


async def _tool_list_server_configs(args: dict, ctx: AgentContext) -> list[dict]:
    server_configs = ctx.server_configs or {}
    return [server_config_to_safe_dict(cfg) for cfg in server_configs.values()]


async def _tool_get_server_config(args: dict, ctx: AgentContext) -> dict:
    name = args.get("name")
    if not name:
        return {"error": "缺少 name"}
    server_configs = ctx.server_configs or {}
    cfg = server_configs.get(name)
    if cfg is None:
        return {"error": f"server {name} 不存在"}
    return server_config_to_safe_dict(cfg)


async def _tool_test_server_ssh(args: dict, ctx: AgentContext) -> dict:
    """唯讀，直接執行（不建 approval，比照使用者規格）。`payload` 是一組
    完整的 server 設定，可以測試「還沒加入 servers.yaml 的機器」——不是查
    現有 name。需要 `ctx.ssh_run_direct`（呼叫端注入
    `app_state.ssh_pool.run`，本模組不 import sshpool）。

    **Bug 修正**：舊版用 `ctx.ssh_run(name, command, timeout)` 按名字查
    `server_configs`，完全無視這裡組出來的 `server_cfg`（payload 的
    host/port/user/key），測到的是舊設定或直接 KeyError。這裡改用一個
    綁定 `server_cfg` 的 closure 直接呼叫 `ctx.ssh_run_direct`，`_name`
    參數刻意不使用。
    """
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else args
    if ctx.ssh_run_direct is None:
        return {"error": "此環境未提供 ssh_run_direct，無法測試 SSH"}
    try:
        server_cfg = ServerConfig(
            name=str(payload.get("name") or "test-server"),
            host=str(payload.get("host") or ""),
            user=str(payload.get("user") or ""),
            key=str(payload.get("key") or ""),
            gpu=bool(payload.get("gpu", False)),
            idle_gpu_util=float(payload.get("idle_gpu_util", 15.0) or 15.0),
            idle_load=float(payload.get("idle_load", 2.0) or 2.0),
            tags=list(payload.get("tags") or []),
            port=int(payload.get("port", 22) or 22),
            project_roots=list(payload.get("project_roots") or []),
            dataset_roots=list(payload.get("dataset_roots") or []),
        )
    except (TypeError, ValueError) as exc:
        return {"error": f"設定格式不正確：{exc}"}

    async def _direct_run(_name: str, command: str, timeout: float):
        return await ctx.ssh_run_direct(server_cfg, command, timeout)

    return await test_ssh_connection(server_cfg, _direct_run)


# ---------------------------------------------------------------------------
# 寫入工具：Web Server Management（階段 8 第二批）——全部只建立 approval，
# 沒有 request_delete_server 工具：LLM 不能刪除機器，連建立刪除請求的權限
# 都沒有，只有網頁介面可以發起 delete-request（PLAN.md I.6）。
# ---------------------------------------------------------------------------


async def _tool_request_add_server(args: dict, ctx: AgentContext) -> dict:
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else args
    try:
        approval = request_server_add_approval(
            ctx.db,
            payload,
            ctx.config,
            audit_path=ctx.audit_path,
            request_context=ctx.request_context,
        )
    except InvalidServerConfigError as exc:
        return {"rejected": True, "reason": str(exc), "errors": exc.errors}
    except ValueError as exc:
        return {"error": str(exc)}
    return {"approval": approval_to_dict(approval)}


async def _tool_request_update_server(args: dict, ctx: AgentContext) -> dict:
    name = args.get("name")
    updates = args.get("updates") or {}
    if not name:
        return {"error": "缺少 name"}
    if not isinstance(updates, dict):
        return {"error": "updates 必須是一個 JSON 物件"}
    server_configs = ctx.server_configs or {}
    current_servers = [server_config_to_safe_dict(cfg) for cfg in server_configs.values()]
    try:
        approval = request_server_update_approval(
            ctx.db,
            name,
            updates,
            ctx.config,
            current_servers,
            audit_path=ctx.audit_path,
            request_context=ctx.request_context,
        )
    except ServerRenameNotSupportedError as exc:
        return {"error": str(exc)}
    except ServerNotFoundError as exc:
        return {"error": str(exc)}
    except InvalidServerConfigError as exc:
        return {"rejected": True, "reason": str(exc), "errors": exc.errors}
    except ValueError as exc:
        return {"error": str(exc)}
    return {"approval": approval_to_dict(approval)}


async def _tool_request_disable_server(args: dict, ctx: AgentContext) -> dict:
    name = args.get("name")
    if not name:
        return {"error": "缺少 name"}
    server_configs = ctx.server_configs or {}
    current_names = list(server_configs.keys())
    try:
        approval = request_server_disable_approval(
            ctx.db,
            name,
            current_names,
            audit_path=ctx.audit_path,
            request_context=ctx.request_context,
        )
    except ServerNotFoundError as exc:
        return {"error": str(exc)}
    return {"approval": approval_to_dict(approval)}


# ---------------------------------------------------------------------------
# 專案詳情頁（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、目標／
# 方法／進度管理」計畫第 4 節）：一個唯讀工具 + 兩個**已正式登記的鐵律
# 例外**寫入工具（不建 approval，直接執行，見模組 docstring 的例外段落）。
# ---------------------------------------------------------------------------


async def _tool_search_experiment_timeline(args: dict, ctx: AgentContext) -> dict:
    """唯讀，直接呼叫 `app.records.build_timeline()`——跟
    `GET /projects/{name}/timeline`（`app/main.py`）共用同一套合併邏輯，
    agent 跟網頁使用者查到的是同一份資料。`kinds` 接受字串列表（不像 HTTP
    端點的 querystring 需要逗號分隔）。"""
    name = args.get("project_name")
    if not name:
        return {"error": "缺少 project_name"}
    project = ctx.db.get_project(name)
    if project is None:
        return {"error": f"專案 {name} 不存在"}
    kinds = args.get("kinds")
    if kinds is not None and not isinstance(kinds, list):
        return {"error": "kinds 必須是字串列表"}
    limit = _clamp_int(args.get("limit"), default=20, lo=1, hi=50)
    return build_timeline(
        ctx.db,
        name,
        q=args.get("q") or None,
        limit=limit,
        before_ts=args.get("before_ts") or None,
        kinds=kinds,
    )


async def _tool_add_experiment_record(args: dict, ctx: AgentContext) -> dict:
    """**鐵律第 2 條的正式登記例外**（見模組 docstring）：直接寫入
    `experiment_records` 表一列，不建立 approval。**強制 `author="agent"`
    **——呼叫端無法透過 `args` 覆蓋這個值（handler 完全不讀 `args` 裡任何
    叫 `author` 的鍵），避免模型假冒成 `"user"` 寫的紀錄。`kind` 不合法 ->
    error（不是拋例外，跟其他唯讀/寫入工具一致的錯誤回傳風格）。`job_id`／
    `coding_run_id` 選填，把這筆紀錄掛在某次派工或 Codex 執行底下（弱關聯，
    同 `app.db.insert_experiment_record()`，不驗證所指 id 是否存在）。"""
    name = args.get("project_name")
    if not name:
        return {"error": "缺少 project_name"}
    project = ctx.db.get_project(name)
    if project is None:
        return {"error": f"專案 {name} 不存在"}
    content = (args.get("content") or "").strip()
    if not content:
        return {"error": "缺少 content"}
    kind = args.get("kind") or "note"
    if kind not in VALID_RECORD_KINDS:
        return {"error": f"kind 必須是 {sorted(VALID_RECORD_KINDS)} 其中之一"}

    job_id = args.get("job_id")
    if job_id is not None:
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            return {"error": "job_id 必須是整數"}

    coding_run_id = args.get("coding_run_id")
    if coding_run_id is not None:
        try:
            coding_run_id = int(coding_run_id)
        except (TypeError, ValueError):
            return {"error": "coding_run_id 必須是整數"}

    record_id = ctx.db.insert_experiment_record(
        project=name,
        content=content,
        kind=kind,
        title=args.get("title") or None,
        author="agent",
        job_id=job_id,
        coding_run_id=coding_run_id,
    )
    append_audit(
        "experiment_record_created",
        {
            "project": name,
            "record_id": record_id,
            "kind": kind,
            "author": "agent",
            "job_id": job_id,
            "coding_run_id": coding_run_id,
        },
        path=ctx.audit_path,
        actor=audit_actor_from_request_context(ctx.request_context),
    )
    record = ctx.db.get_experiment_record(record_id)
    return {
        "id": record.id,
        "project": record.project,
        "kind": record.kind,
        "title": record.title,
        "content": record.content,
        "author": record.author,
        "job_id": record.job_id,
        "coding_run_id": record.coding_run_id,
        "created_at": record.created_at,
    }


async def _tool_update_project_doc(args: dict, ctx: AgentContext) -> dict:
    """**鐵律第 2 條的正式登記例外**（見模組 docstring）：直接更新
    `projects` 表的 `goal`/`optimization_notes`/`progress` 其中一欄，不建立
    approval。`field` 限白名單 `_PROJECT_DOC_FIELDS`，不在白名單內 ->
    error（不允許透過這個工具改到 `name`/`repo_or_path` 等結構性欄位）。"""
    name = args.get("project_name")
    if not name:
        return {"error": "缺少 project_name"}
    project = ctx.db.get_project(name)
    if project is None:
        return {"error": f"專案 {name} 不存在"}
    field = args.get("field")
    if field not in _PROJECT_DOC_FIELDS:
        return {"error": f"field 必須是 {sorted(_PROJECT_DOC_FIELDS)} 其中之一"}
    content = args.get("content")
    if content is None:
        return {"error": "缺少 content"}

    ctx.db.update_project(name, **{field: content})
    append_audit(
        "project_updated",
        {"name": name, "fields": [field], "author": "agent"},
        path=ctx.audit_path,
        actor=audit_actor_from_request_context(ctx.request_context),
    )
    updated = ctx.db.get_project(name)
    return {"project": _project_summary(updated)}


# ---------------------------------------------------------------------------
# 註冊表
# ---------------------------------------------------------------------------

TOOLS: dict[str, ToolSpec] = {
    "status": ToolSpec(
        name="status",
        description="查詢所有伺服器目前的線上/離線、GPU 使用率、load1 摘要文字。",
        args={},
        handler=_tool_status,
    ),
    "servers": ToolSpec(
        name="servers",
        description="列出所有伺服器的監控狀態（結構化資料，含已快取資料集）。",
        args={},
        handler=_tool_servers,
    ),
    "jobs": ToolSpec(
        name="jobs",
        description="查詢任務佇列列表（摘要，不含完整 log）。",
        args={
            "status": "字串，選填，篩選狀態（queued/running/done/failed/blocked/cancelled）",
            "project": "字串，選填，篩選專案名稱",
            "limit": "整數，選填，最多回傳幾筆（≤20，預設 20）",
        },
        handler=_tool_jobs,
    ),
    "job_detail": ToolSpec(
        name="job_detail",
        description="查詢單一任務的完整欄位（不含即時 log，只有存檔的 log_tail）。",
        args={"job_id": "整數，必填，任務 id"},
        handler=_tool_job_detail,
    ),
    "job_log": ToolSpec(
        name="job_log",
        description="查詢單一任務存檔的 log 尾段（不做即時 SSH，只回 DB 存的 log_tail）。",
        args={
            "job_id": "整數，必填，任務 id",
            "lines": "整數，選填，取尾幾行（≤80，預設 40）",
        },
        handler=_tool_job_log,
    ),
    "approvals": ToolSpec(
        name="approvals",
        description="查詢待核准/已核准/已拒絕的核准請求列表。",
        args={"status": "字串，選填，篩選狀態（pending/approved/rejected）"},
        handler=_tool_approvals,
    ),
    "events": ToolSpec(
        name="events",
        description="查詢最近的稽核事件（audit.jsonl 尾段，新到舊）。",
        args={"n": "整數，選填，取最近幾筆（≤50，預設 20）"},
        handler=_tool_events,
    ),
    "gpu": ToolSpec(
        name="gpu",
        description="查詢所有伺服器的 GPU 讀數摘要（每卡使用率/顯存）。",
        args={},
        handler=_tool_gpu,
    ),
    "vllm_health": ToolSpec(
        name="vllm_health",
        description="檢查本地 vLLM 服務是否健康（GET /models）。",
        args={},
        handler=_tool_vllm_health,
    ),
    "get_dataset_card": ToolSpec(
        name="get_dataset_card",
        description=(
            "查詢單一資料集版本的資料卡：怎麼做的（製作方式/預處理/生成"
            "模型與設定/資料來源）、數量（自動事實：檔案數/總大小/登記"
            "時間/目前快取在哪幾台機器，加上使用者自訂的數量如"
            "train/val/test 切分數、類別數量）、衍生自哪一版（lineage）。"
            "`card` 欄位可能是 null——階段 16 之前建立、或尚未補登的版本"
            "沒有資料卡，這不是錯誤，auto_facts／rendered 仍會照常提供；"
            "此時必須照實告知使用者「這個版本未登記資料卡，製作方式與"
            "用途無紀錄」，絕對不得推測或腦補製作方式。"
        ),
        args={
            "name": "字串，必填，資料集名稱",
            "version": "字串，必填，資料集版本",
        },
        handler=_tool_get_dataset_card,
    ),
    "request_enqueue_job": ToolSpec(
        name="request_enqueue_job",
        description=(
            "建立一筆派工核准請求（絕不直接入列，一定要人工核准才會真的排隊）。"
            "危險指令會在這裡直接被拒絕，不會建立核准請求。"
        ),
        args={
            "command": "字串，必填，要執行的完整 shell 指令",
            "type": "字串，選填，adhoc/train（預設 adhoc）",
            "project": "字串，選填，專案名稱",
            "pin_server": "字串，選填，指定機器名稱",
            "require_tag": "字串，選填，要求機器帶有的標籤",
            "priority": "字串，選填，normal/low（預設 normal）",
        },
        handler=_tool_request_enqueue_job,
    ),
    "request_stop_job": ToolSpec(
        name="request_stop_job",
        description="建立一筆停止任務的核准請求（絕不直接停止，一定要人工核准才會真的停止）。",
        args={"job_id": "整數，必填，要停止的任務 id（必須是 running 狀態）"},
        handler=_tool_request_stop_job,
    ),
    "request_rerun_job": ToolSpec(
        name="request_rerun_job",
        description=(
            "複製一筆既有任務的派工參數（command/type/project/require_tag/"
            "pin_server/priority），建立一筆新的派工核准請求（不複製任務執行期"
            "狀態，絕不直接入列）。"
        ),
        args={"job_id": "整數，必填，要重跑的原任務 id"},
        handler=_tool_request_rerun_job,
    ),
    "list_project_candidates": ToolSpec(
        name="list_project_candidates",
        description="列出 inventory 掃描找到的候選專案（預設只顯示 pending 狀態）。",
        args={
            "server": "字串，選填，篩選機器名稱",
            "status": "字串，選填，pending/imported/ignored（預設 pending）",
        },
        handler=_tool_list_project_candidates,
    ),
    "get_project_candidate": ToolSpec(
        name="get_project_candidate",
        description="查詢單一候選專案的完整欄位（含 markers/README 摘要/embedded 資料路徑）。",
        args={"candidate_id": "字串，必填，候選專案 id"},
        handler=_tool_get_project_candidate,
    ),
    "search_projects": ToolSpec(
        name="search_projects",
        description="搜尋已註冊的專案（比對名稱/摘要/各機路徑，簡單子字串比對）。",
        args={"query": "字串，必填，搜尋關鍵字"},
        handler=_tool_search_projects,
    ),
    "get_project_profile": ToolSpec(
        name="get_project_profile",
        description="查詢單一已註冊專案的完整資料（含各機的 project_instances 列表）。",
        args={"project_name": "字串，必填，專案名稱"},
        handler=_tool_get_project_profile,
    ),
    "list_project_instances": ToolSpec(
        name="list_project_instances",
        description="列出某個已註冊專案在各機器上的實例（路徑/git 資訊/embedded 資料）。",
        args={"project_name": "字串，必填，專案名稱"},
        handler=_tool_list_project_instances,
    ),
    "get_project_activity": ToolSpec(
        name="get_project_activity",
        description=(
            "查詢某個已註冊專案的完整執行近況：各機 GPU/磁碟現況、最近 10 筆"
            "任務摘要與最新一筆 log_tail，以及對每個在線 instance 的唯讀 SSH"
            "探測（近期變動檔案清單、至多 3 個 log 檔的尾段內容，含系統外"
            "手動跑的訓練 log）。會觸發即時 SSH（唯讀），只探測該專案已登記"
            "的機器/路徑，離線機器直接跳過。"
        ),
        args={"project_name": "字串，必填，專案名稱"},
        handler=_tool_get_project_activity,
    ),
    "list_project_files": ToolSpec(
        name="list_project_files",
        description=(
            "列出某個已註冊專案在指定機器上的檔案（相對路徑，秘密檔已過濾"
            "，至多 200 筆，只讀不寫）。server 選填，只有一個 instance 時"
            "自動選，多個 instance 時必須指定。subdir 選填（相對 instance"
            "路徑的子目錄），含 .. 或絕對路徑會被拒絕。"
        ),
        args={
            "project_name": "字串，必填，專案名稱",
            "server": "字串，選填，機器名稱（多個 instance 時必填）",
            "subdir": "字串，選填，相對 instance 路徑的子目錄",
        },
        handler=_tool_list_project_files,
    ),
    "read_project_file": ToolSpec(
        name="read_project_file",
        description=(
            "讀取某個已註冊專案在指定機器上的單一檔案內容（前 64KB，只讀"
            "不寫）。file_path 必須是相對 instance 路徑的相對路徑，含 .. "
            "、絕對路徑、秘密檔名（.env/*.pem/id_rsa* 等）一律拒絕。"
        ),
        args={
            "project_name": "字串，必填，專案名稱",
            "file_path": "字串，必填，相對 instance 路徑的檔案路徑",
            "server": "字串，選填，機器名稱（多個 instance 時必填）",
        },
        handler=_tool_read_project_file,
    ),
    "scan_project_inventory": ToolSpec(
        name="scan_project_inventory",
        description=(
            "建立一筆掃描機器 project_roots 的核准請求（絕不直接掃描，一定"
            "要人工核准後才會真的 SSH 掃描）。project_roots 選填，沒帶時從"
            "該機器 servers.yaml 設定的 project_roots 自動代入；server=\"all\""
            "會對每一台已啟用的機器各自建立一筆獨立的核准請求。"
        ),
        args={
            "server": "字串，必填，機器名稱或 \"all\"",
            "project_roots": "字串列表，選填，要掃描的根目錄（不帶則用該機器設定值）",
        },
        handler=_tool_scan_project_inventory,
    ),
    "request_import_project_candidate": ToolSpec(
        name="request_import_project_candidate",
        description="建立一筆把候選專案匯入成正式專案的核准請求（絕不直接匯入）。",
        args={
            "candidate_id": "字串，必填，候選專案 id",
            "overrides": "JSON 物件，選填，覆蓋候選專案猜測值（name/default_command/dataset_name/dataset_version/dataset_mode/require_tag/setup_cmd/summary）",
        },
        handler=_tool_request_import_project_candidate,
    ),
    "request_ignore_project_candidate": ToolSpec(
        name="request_ignore_project_candidate",
        description="建立一筆忽略候選專案的核准請求（絕不直接忽略）。",
        args={"candidate_id": "字串，必填，候選專案 id"},
        handler=_tool_request_ignore_project_candidate,
    ),
    "list_server_configs": ToolSpec(
        name="list_server_configs",
        description="列出所有 servers.yaml 設定（key 只顯示路徑，不回傳私鑰內容）。",
        args={},
        handler=_tool_list_server_configs,
    ),
    "get_server_config": ToolSpec(
        name="get_server_config",
        description="查詢單一機器的 servers.yaml 設定（key 只顯示路徑，不回傳私鑰內容）。",
        args={"name": "字串，必填，機器名稱"},
        handler=_tool_get_server_config,
    ),
    "test_server_ssh": ToolSpec(
        name="test_server_ssh",
        description=(
            "唯讀測試一組機器設定的 SSH 連線（不建 approval，直接執行）："
            "只跑 hostname/whoami/tmux -V/nvidia-smi 與各 project_roots/"
            "dataset_roots 的 test -d 檢查，不寫遠端檔案。"
        ),
        args={"payload": "JSON 物件，必填，一組完整的 server 設定（name/host/user/key/port/project_roots/dataset_roots 等）"},
        handler=_tool_test_server_ssh,
    ),
    "request_add_server": ToolSpec(
        name="request_add_server",
        description="建立一筆新增機器的核准請求（絕不直接寫 servers.yaml，不合法的設定會直接被拒絕）。",
        args={"payload": "JSON 物件，必填，一組完整的 server 設定"},
        handler=_tool_request_add_server,
    ),
    "request_update_server": ToolSpec(
        name="request_update_server",
        description="建立一筆更新既有機器設定的核准請求（絕不直接寫 servers.yaml，不支援改名）。",
        args={
            "name": "字串，必填，機器名稱",
            "updates": "JSON 物件，必填，要更新的欄位",
        },
        handler=_tool_request_update_server,
    ),
    "request_disable_server": ToolSpec(
        name="request_disable_server",
        description="建立一筆停用機器的核准請求（絕不直接停用；核准當下若該機器有執行中任務會被拒絕）。",
        args={"name": "字串，必填，機器名稱"},
        handler=_tool_request_disable_server,
    ),
    "search_experiment_timeline": ToolSpec(
        name="search_experiment_timeline",
        description=(
            "查詢某個已註冊專案的實驗紀錄時間軸（唯讀）：手動筆記/觀察/"
            "結論/決策（experiment_records）與自動出現的任務"
            "（jobs）、Codex 改碼紀錄（coding_runs）三個來源動態合併、"
            "依時間新到舊排序。可用 q 做跨表關鍵字搜尋，kinds 過濾類型"
            "（note/observation/conclusion/decision/job/coding_run），"
            "before_ts 配合上一次回傳的 next_before_ts 載入更舊的紀錄。"
        ),
        args={
            "project_name": "字串，必填，專案名稱",
            "q": "字串，選填，跨表關鍵字搜尋",
            "limit": "整數，選填，最多回傳幾筆（≤50，預設 20）",
            "before_ts": "字串，選填，分頁游標（上一次回傳的 next_before_ts）",
            "kinds": "字串列表，選填，過濾類型",
        },
        handler=_tool_search_experiment_timeline,
    ),
    "add_experiment_record": ToolSpec(
        name="add_experiment_record",
        description=(
            "在某個已註冊專案的實驗紀錄時間軸補一筆筆記/觀察/結論/決策"
            "（Markdown 文字）。這是唯二不需要人工核准就直接寫入的工具之"
            "一（純 DB 文字，不會派工、不會碰任何機器、可刪可改）——"
            "author 固定記成 agent，不能偽裝成使用者手動填的紀錄。"
        ),
        args={
            "project_name": "字串，必填，專案名稱",
            "content": "字串，必填，紀錄內容（Markdown）",
            "kind": "字串，選填，note/observation/conclusion/decision（預設 note）",
            "title": "字串，選填，標題",
            "job_id": "整數，選填，把這筆紀錄掛在某次派工任務底下（弱關聯，不驗證是否存在）",
            "coding_run_id": "整數，選填，把這筆紀錄掛在某次 Codex 改碼執行底下（弱關聯，不驗證是否存在）",
        },
        handler=_tool_add_experiment_record,
    ),
    "update_project_doc": ToolSpec(
        name="update_project_doc",
        description=(
            "更新某個已註冊專案的目標/優化方法/目前進度其中一欄（自由文字"
            "Markdown，整欄覆蓋寫入）。這是唯二不需要人工核准就直接寫入"
            "的工具之一（純 DB 文字，不會派工、不會碰任何機器，改壞了可"
            "以再改回去）。field 只能是 goal/optimization_notes/progress"
            "，不能用來改專案名稱/路徑等結構性欄位。"
        ),
        args={
            "project_name": "字串，必填，專案名稱",
            "field": "字串，必填，goal/optimization_notes/progress 其中之一",
            "content": "字串，必填，新的內容（整欄覆蓋）",
        },
        handler=_tool_update_project_doc,
    ),
}

if set(TOOLS) != set(LOCAL_TOOL_AUTHORIZATION) or set(TOOLS) != set(LOCAL_TOOL_RESOURCES):
    raise RuntimeError("local tool authorization catalog is out of sync with TOOLS")
for _tool_name, _tool_spec in TOOLS.items():
    _tool_spec.authorization_action = LOCAL_TOOL_AUTHORIZATION[_tool_name].value
    _tool_spec.authorization_resource = LOCAL_TOOL_RESOURCES[_tool_name]


def list_tool_specs() -> list[dict]:
    """`GET /agent/tools` 與 prompt 生成共用：把 `TOOLS` 表轉成
    JSON-serializable 列表，避免兩處各自維護一份容易漂移的工具清單。"""
    return [
        {"name": spec.name, "description": spec.description, "args": spec.args}
        for spec in TOOLS.values()
    ]


async def dispatch_tool(name: str, args: dict, ctx: AgentContext) -> Any:
    """唯一的工具分派入口：只查 `TOOLS` 表，不用 `getattr()`/`eval()`。
    呼叫端（`app.agent_runtime`）應該先確認 `name in TOOLS` 再呼叫這個
    函式；這裡仍防禦性地在找不到工具時丟 `KeyError`。"""
    spec = TOOLS[name]
    assert spec.handler is not None
    handler_args = dict(args or {})
    evidence = collect_shadow_evidence(
        mode=getattr(ctx.config, "authorization_mode", "off"),
        db=ctx.db,
        context=ctx.request_context,
        action=spec.authorization_action or "",
        resource_kind=spec.authorization_resource or "",
        values=handler_args,
        interface_kind="tool",
        interface_name=name,
    )
    try:
        return await spec.handler(handler_args, ctx)
    finally:
        # Emit after the handler result is materialized so the `events` tool's
        # response is byte-for-byte compatible with authorization mode `off`.
        emit_shadow_evidence(evidence, audit_path=ctx.audit_path)
