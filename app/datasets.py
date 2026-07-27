"""專案／資料集註冊表、sync 任務、資料引力（PLAN.md D 節）。

- 資料集用「檔案清單＋各檔大小」的 manifest 代替全量 hash：大檔（幾十到
  幾百 GB）逐位元組算 checksum 太貴，manifest（相對路徑＋大小）已經足以
  在同步後做「檔案數與總大小比對」這個等級的完整性檢查；真的要抓「內容被
  改過但大小剛好沒變」這種邊角案例需要全量 hash，這裡刻意不做，取捨記錄
  在 README。
- sync 任務在 Server A 本地執行（`server` 欄記 `_local`，見
  `app/localrun.py`），rsync 從 Server A 推到目標機；`app/scheduler.py` 把
  `_local` 視為永遠在線、可並行（上限 `LOCAL_SYNC_CONCURRENCY`）的特殊目標。
- 資料集名/版本一律驗證字元集限 `[A-Za-z0-9._-]` 再拼進 shell 指令
  （rsync 目的地路徑、mkdir 路徑），不把使用者輸入直接拼進 shell。
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Callable, Optional

from app.audit import SYSTEM_AUDIT_ACTOR, append_audit, now_iso
from app.db import Database, Dataset, Job
from app.monitor import parse_df_output

#: sync 任務的特殊執行目標：在 Server A 本地跑（同一套哨兵協議、本地
#: tmux），排程器把它當成「永遠在線、可並行」的偽伺服器。
LOCAL_SERVER = "_local"

#: 同時間最多幾個 sync 任務在本地並行執行（PLAN.md D 節：上限 2 個）。
LOCAL_SYNC_CONCURRENCY = 2

#: sync 前檢查目標機剩餘空間至少要是資料集大小的幾倍（原規格 5.5）。
SPACE_SAFETY_FACTOR = 1.2

_NAME_CHARSET_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class InvalidNameError(ValueError):
    """資料集名/版本/專案名含有不允許的字元。"""


class InvalidDatasetCardError(ValueError):
    """階段 16（PLAN.md Q.3）：資料卡欄位不合法——`description`／`method`
    缺失或全是空白字元，或 `counts` 內某個值不是 str/int/float。"""


def validate_name_component(value: str, *, field: str = "name") -> str:
    """限制字元集為 `[A-Za-z0-9._-]`，避免使用者輸入直接拼進 shell 指令
    （rsync 目的地路徑、`mkdir -p` 路徑、git clone 目錄名）造成注入風險。
    """
    if not value or not _NAME_CHARSET_RE.match(value):
        raise InvalidNameError(
            f"{field} 含有不允許的字元（僅允許英數字、`.`、`_`、`-`）: {value!r}"
        )
    return value


def dataset_remote_dir(name: str, version: str) -> str:
    """資料集在工作機上的相對路徑（不帶 `~/`，沿用既有慣例，見
    `app/jobqueue.py` 的 `AGENT_JOBS_DIR` 說明）：`datasets/{name}/{version}`。
    """
    validate_name_component(name, field="dataset_name")
    validate_name_component(version, field="dataset_version")
    return f"datasets/{name}/{version}"


# ---------------------------------------------------------------------------
# manifest：掃描本機來源目錄，產生「檔案清單＋各檔大小」
# ---------------------------------------------------------------------------


def build_manifest(source_path: str) -> dict:
    """掃描 `source_path`（Server A 上的本機路徑），回傳：
        {"file_count": N, "total_size": N, "files": [{"path": rel, "size": n}, ...]}
    `path` 是相對於 `source_path` 的相對路徑，用 `/` 分隔（跨平台一致）。
    這是阻塞的檔案系統操作，呼叫端（API handler）應該用
    `asyncio.to_thread()` 包起來，避免卡住 event loop。
    """
    if not os.path.isdir(source_path):
        raise ValueError(f"來源路徑不存在或不是目錄: {source_path}")
    files: list[dict] = []
    total_size = 0
    for root, _dirs, filenames in os.walk(source_path):
        for fname in sorted(filenames):
            full = os.path.join(root, fname)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            rel = os.path.relpath(full, source_path).replace(os.sep, "/")
            files.append({"path": rel, "size": size})
            total_size += size
    files.sort(key=lambda f: f["path"])
    return {"file_count": len(files), "total_size": total_size, "files": files}


def verify_manifest_match(
    manifest: dict, remote_file_count: int, remote_total_size: int
) -> tuple[bool, str]:
    """同步後驗證（原規格 5.5）：只比對「檔案數」與「總大小」，不逐檔比對
    內容或 hash（取捨見模組 docstring）。"""
    expected_count = manifest.get("file_count", 0)
    expected_size = manifest.get("total_size", 0)
    if remote_file_count != expected_count:
        return False, (
            f"檔案數不符：manifest 記錄 {expected_count}，目標機實際 {remote_file_count}"
        )
    if remote_total_size != expected_size:
        return False, (
            f"總大小不符：manifest 記錄 {expected_size} bytes，"
            f"目標機實際 {remote_total_size} bytes"
        )
    return True, ""


# ---------------------------------------------------------------------------
# rsync / df / manifest-check 指令組裝（純函式，不真的執行）
# ---------------------------------------------------------------------------


def build_ssh_opts(key_path: str, port: int = 22) -> str:
    """所有 rsync/ssh 指令組裝的**唯一** ssh 選項入口：port 支援集中在此
    （2026-07-10 修復 pro6000 32221 埠的結果回收/資料集 sync 連線失敗）。
    `port` 非 22（asyncssh／`ServerConfig.port` 的預設值）時在字串尾附加
    ` -p {port}`（int，不需 `shlex.quote()`）；`port == 22` 時輸出與既有格式
    完全相同，向下相容。"""
    opts = f"ssh -i {shlex.quote(key_path)} -o StrictHostKeyChecking=no -o BatchMode=yes"
    if port != 22:
        opts += f" -p {port}"
    return opts


def build_sync_script(
    source_path: str, user: str, host: str, dest_dir: str, key_path: str, port: int = 22
) -> str:
    """組出 sync 任務的 cmd.sh 內容：先在目標機 `mkdir -p` 目的地目錄，成功後
    才 rsync 推過去（`-a --partial --info=progress2`，原規格 5.5）。
    `dest_dir` 一律來自 `dataset_remote_dir()`（已驗證字元集），`source_path`
    是管理員在 POST /datasets 時填入、視為可信任的本機路徑，仍用
    `shlex.quote()` 包起來（可能含空白），不直接字串拼接使用者輸入。
    `port`：非預設 SSH port（見 `build_ssh_opts()`）；`-e {ssh_opts}` 同時
    涵蓋 rsync 傳輸與前置的 remote mkdir（兩處都用同一份 ssh_opts，改一處
    即全生效）。
    """
    ssh_opts = build_ssh_opts(key_path, port)
    remote = f"{user}@{host}"
    src = source_path.rstrip("/") + "/"
    remote_mkdir = f"{ssh_opts} {shlex.quote(remote)} {shlex.quote(f'mkdir -p {dest_dir}')}"
    rsync_cmd = (
        f"rsync -a --partial --info=progress2 -e {shlex.quote(ssh_opts)} "
        f"{shlex.quote(src)} {shlex.quote(remote)}:{dest_dir}/"
    )
    return f"{remote_mkdir} && {rsync_cmd}"


def build_df_command(path: str = ".") -> str:
    """sync 前的剩餘空間檢查指令（原規格 5.5）。用目標機 home 目錄所在檔案
    系統的可用空間（`.`），跟總覽卡片的磁碟餘量探測共用同一個解析函式
    `app.monitor.parse_df_output`。"""
    return f"df -Pk {shlex.quote(path)} 2>/dev/null | tail -1"


def has_enough_space(
    avail_bytes: Optional[int], dataset_size_bytes: int, factor: float = SPACE_SAFETY_FACTOR
) -> bool:
    if avail_bytes is None:
        return False
    return avail_bytes > dataset_size_bytes * factor


def build_remote_manifest_check_command(dest_dir: str) -> str:
    """sync 後在目標機統計實際檔案數與總大小，供 `verify_manifest_match()`
    比對。輸出固定兩行 `COUNT:n` / `SIZE:n`，方便解析。"""
    d = shlex.quote(dest_dir)
    return (
        f"echo COUNT:$(find {d} -type f 2>/dev/null | wc -l); "
        f"echo SIZE:$(find {d} -type f -printf '%s\\n' 2>/dev/null "
        f"| awk '{{s+=$1}} END{{print s+0}}')"
    )


def parse_remote_manifest_check(output: str) -> tuple[int, int]:
    count = 0
    size = 0
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("COUNT:"):
            try:
                count = int(line.split(":", 1)[1].strip() or 0)
            except ValueError:
                count = 0
        elif line.startswith("SIZE:"):
            try:
                size = int(line.split(":", 1)[1].strip() or 0)
            except ValueError:
                size = 0
    return count, size


# ---------------------------------------------------------------------------
# setup_cmd：機器首次跑某專案時的 git clone/pull + setup_cmd
# ---------------------------------------------------------------------------


def build_setup_script(project_name: str, repo_or_path: str, setup_cmd: Optional[str]) -> str:
    """git clone（第一次）或 pull（目錄已存在，理論上不會發生，因為呼叫端只
    在「未曾成功跑過 setup」時才建立這個任務，但 `git pull` 分支還是留著，
    防呆：萬一目錄是用別的方式先建立的）＋ `setup_cmd`（選填）。
    專案名同樣驗證字元集，因為會被當成 `projects/{name}` 目錄名，直接拼進
    shell 指令。
    """
    validate_name_component(project_name, field="project_name")
    repo_quoted = shlex.quote(repo_or_path)
    dir_path = f"projects/{project_name}"
    parts = [
        "mkdir -p projects",
        (
            f"if [ -d {dir_path}/.git ]; then git -C {dir_path} pull; "
            f"else git clone {repo_quoted} {dir_path}; fi"
        ),
    ]
    if setup_cmd:
        parts.append(f"cd {dir_path} && {setup_cmd}")
    return " && ".join(parts)


# ---------------------------------------------------------------------------
# 派工計畫：訓練任務目標機沒資料 → sync 計畫；機器沒跑過該專案 → setup 計畫
# ---------------------------------------------------------------------------


@dataclass
class DispatchPlan:
    sync_plan: Optional[dict] = None
    setup_plan: Optional[dict] = None


def build_dispatch_plan(db: Database, project, target_server: str) -> DispatchPlan:
    """純 DB 查詢（不 SSH），在「建立核准請求」當下算出這一次派工要不要附帶
    sync / setup 依賴任務，讓使用者在核准卡片上就看到完整計畫（PLAN.md D：
    「這整包計畫顯示在核准卡片上」），核准的當下才真的建立任務列
    （`app/approvals.py` 的 `approve()`）。

    只有明確指定 `pin_server`（使用者在派工彈窗選了具體機器）時才會呼叫
    這個函式；選「自動」時無法預先知道排程器最終會挑哪台機，因此不預先
    產生 sync/setup 計畫——鐵律第 2 條要求「排任務、同步資料要先核准」，
    如果排程器之後才臨時發明一個沒被核准過的 sync 任務會違反這條鐵律
    （Fable 覆核：鐵律優先於原規格 5.4「排程器自動建 sync」）。所以
    「自動」模式下，`pick_job` 的 `has_dataset` 掛勾只會**資格過濾**掉
    「所需資料集哪都沒有快取」的訓練任務（見 `app/scheduler.py` 模組
    docstring），不會自動生出新任務——這種任務會留在 queued，
    `request_enqueue_approval()` 會在建立核准請求當下附上 `warning`
    提醒使用者改用指定機器派工（見下）。
    """
    sync_plan: Optional[dict] = None
    if project.dataset_name and project.dataset_version:
        if not db.is_dataset_cached(target_server, project.dataset_name, project.dataset_version):
            dataset = db.get_dataset(project.dataset_name, project.dataset_version)
            if dataset is None:
                raise ValueError(
                    f"資料集 {project.dataset_name}@{project.dataset_version} 尚未註冊"
                    "，請先呼叫 POST /datasets 註冊"
                )
            sync_plan = {
                "dataset_name": dataset.name,
                "dataset_version": dataset.version,
                "size_bytes": dataset.size_bytes,
                "source_path": dataset.source_path,
                "target_server": target_server,
            }

    setup_plan: Optional[dict] = None
    if project.setup_cmd or project.repo_or_path:
        if project.setup_cmd and not db.has_completed_setup(project.name, target_server):
            setup_plan = {
                "project": project.name,
                "target_server": target_server,
                "repo_or_path": project.repo_or_path,
                "setup_cmd": project.setup_cmd,
            }

    return DispatchPlan(sync_plan=sync_plan, setup_plan=setup_plan)


def make_has_dataset(db: Database, server_name: str) -> Callable[[Job], bool]:
    """`app.scheduler.pick_job()` 的 `has_dataset` 掛勾：**資格過濾**——回傳
    False 的任務直接不會被這台機器挑走（不是排後面）。非 train 類型、沒有
    掛專案、專案沒有指定資料集的任務一律視為「沒有資料集要求」（回傳
    True，不受限制）。
    """

    def has_dataset(job: Job) -> bool:
        if job.type != "train" or not job.project:
            return True
        project = db.get_project(job.project)
        if project is None or not project.dataset_name:
            return True
        return db.is_dataset_cached(server_name, project.dataset_name, project.dataset_version)

    return has_dataset


# ---------------------------------------------------------------------------
# 每小時快取地圖校正：`ls -d datasets/*/*/`
# ---------------------------------------------------------------------------


def parse_dataset_ls_output(text: str) -> list[tuple[str, str]]:
    """解析 `ls -d datasets/*/*/` 輸出，回傳 [(dataset_name, version), ...]。
    容錯：忽略空行、`ls: cannot access ...` 這類錯誤訊息、格式不是
    `datasets/x/y/` 的行。"""
    pairs: list[tuple[str, str]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("datasets/"):
            continue
        line = line.rstrip("/")
        parts = line.split("/")
        if len(parts) == 3 and parts[0] == "datasets" and parts[1] and parts[2]:
            pairs.append((parts[1], parts[2]))
    return pairs


def reconcile_server_dataset_cache(
    db: Database, server_name: str, found: list[tuple[str, str]]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """用 `ls` 找到的實際內容校正某台機器的快取地圖：新增沒登記過的、移除
    已經不存在的。回傳 (新增的集合, 移除的集合)，方便呼叫端寫稽核。"""
    existing = db.list_dataset_cache(server=server_name)
    existing_set = {(e.dataset, e.version) for e in existing}
    found_set = set(found)

    to_add = found_set - existing_set
    to_remove = existing_set - found_set

    for name, version in to_add:
        db.upsert_dataset_cache(server_name, name, version)
    for name, version in to_remove:
        db.delete_dataset_cache(server_name, name, version)

    return to_add, to_remove


# ---------------------------------------------------------------------------
# 非同步：sync 前的空間檢查、sync 後的 manifest 驗證與快取登記
# ---------------------------------------------------------------------------


async def check_disk_space(
    ssh_run, target_server: str, dataset_size_bytes: int, factor: float = SPACE_SAFETY_FACTOR
) -> tuple[bool, str, Optional[int]]:
    """sync 前先 SSH 到目標機 `df` 檢查剩餘空間 > 資料集大小 × factor
    （原規格 5.5）。回傳 (是否足夠, 說明, 可用空間 bytes)。
    SSH 連不上或 df 輸出無法解析都視為「不放行」（保守判定，不足以放行才
    安全，避免同步到一半才發現空間不夠、任務跑到不上不下的狀態）。
    """
    try:
        result = await ssh_run(target_server, build_df_command(), 15)
    except Exception as exc:  # noqa: BLE001
        return False, f"df 檢查失敗（SSH 連不上 {target_server}）：{exc}", None

    avail = parse_df_output(result.stdout or "")
    if avail is None:
        return False, f"df 輸出無法解析，無法確認 {target_server} 剩餘空間是否足夠", None

    required = int(dataset_size_bytes * factor)
    if avail < required:
        return (
            False,
            f"{target_server} 剩餘空間不足：可用 {avail} bytes，"
            f"需要至少 {required} bytes（資料集 {dataset_size_bytes} bytes ×{factor}）",
            avail,
        )
    return True, "", avail


async def finalize_sync_job(
    db: Database,
    job: Job,
    exit_code: Optional[int],
    log_tail: Optional[str],
    ssh_run,
    audit_path: str = "audit.jsonl",
) -> None:
    """sync 任務的 cmd.sh（mkdir + rsync）已經跑完且 exit_code == 0 時呼叫：
    SSH 到 `job.target_server` 統計實際檔案數/大小，跟 manifest 比對，通過
    才登記進 dataset_cache；不通過則把任務**改判為 failed**（就算 rsync 本身
    exit code 是 0，manifest 對不上也不能當作同步成功——原規格 5.5：
    「同步後驗證...成功才登記進快取地圖」）。
    """
    # Import locally because jobqueue imports dataset helpers at module load.
    # Protected Engineering Task/validation sync logs must be sanitized before
    # SQLite persistence; ordinary dataset Jobs retain their legacy behavior.
    from app.jobqueue import safe_persisted_engineering_log_tail

    log_tail = safe_persisted_engineering_log_tail(job, log_tail)
    finished_at = now_iso()
    dataset = None
    if job.dataset_name and job.dataset_version:
        dataset = db.get_dataset(job.dataset_name, job.dataset_version)

    if dataset is None or not job.target_server:
        # 理論上不會發生（sync 任務一定是透過 build_dispatch_plan 建立，欄位
        # 一定齊全），防呆：資料不完整就無法驗證，如實記錄、不登記快取。
        db.update_job(
            job.id, status="done", finished_at=finished_at, exit_code=exit_code, log_tail=log_tail
        )
        append_audit(
            "sync_verify_skipped",
            {"job_id": job.id, "reason": "缺少 dataset 或 target_server 資訊，無法驗證"},
            result="ok",
            path=audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
        )
        return

    dest_dir = dataset_remote_dir(dataset.name, dataset.version)
    try:
        check_res = await ssh_run(
            job.target_server, build_remote_manifest_check_command(dest_dir), 30
        )
    except Exception as exc:  # noqa: BLE001
        failed_log = safe_persisted_engineering_log_tail(
            job,
            f"{log_tail or ''}\n[驗證失敗] 無法連線 "
            f"{job.target_server} 檢查同步結果: {exc}",
        )
        db.update_job(
            job.id,
            status="failed",
            finished_at=finished_at,
            exit_code=exit_code,
            log_tail=failed_log,
        )
        append_audit(
            "sync_verify_failed",
            {
                "job_id": job.id,
                "reason": (
                    "ssh_unreachable: protected details withheld"
                    if job.type == "coding"
                    or job.engineering_task_id is not None
                    or job.engineering_validation_request_id is not None
                    else f"ssh_unreachable: {exc}"
                ),
            },
            result="failed",
            path=audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
        )
        return

    remote_count, remote_size = parse_remote_manifest_check(check_res.stdout or "")
    ok, reason = verify_manifest_match(dataset.manifest, remote_count, remote_size)
    if ok:
        db.update_job(
            job.id, status="done", finished_at=finished_at, exit_code=exit_code, log_tail=log_tail
        )
        db.upsert_dataset_cache(job.target_server, dataset.name, dataset.version)
        append_audit(
            "sync_verified",
            {
                "job_id": job.id,
                "server": job.target_server,
                "dataset": f"{dataset.name}@{dataset.version}",
            },
            path=audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
        )
    else:
        failed_log = safe_persisted_engineering_log_tail(
            job, f"{log_tail or ''}\n[驗證失敗] {reason}"
        )
        db.update_job(
            job.id,
            status="failed",
            finished_at=finished_at,
            exit_code=1,
            log_tail=failed_log,
        )
        append_audit(
            "sync_verify_failed",
            {"job_id": job.id, "reason": reason},
            result="failed",
            path=audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
        )


# ---------------------------------------------------------------------------
# 階段 16（PLAN.md Q.3 節）：資料卡——結構化存 DB（datasets.card JSON
# 欄，見 app/db.py）、人可讀渲染（GET .../card 的 `rendered` 欄）、agent
# 可答（MCP bridge／vLLM agent_tools 的 get_dataset_card 唯讀工具）。
#
# 使用者定的規則：**登記新版本時 description/method 必填**，沒寫就 400
# （見 app/main.py 的 POST /datasets）；可事後 PATCH 補登，補登時一樣
# 必填（同一套 validate_card_fields()）。舊版本（階段 16 之前建立、
# card=None）查詢不報錯、不回空——呼叫端（app/main.py 的 GET
# .../card）自己組 `note` 文字，這裡的 render_dataset_card() 也把同一句
# 固定句寫進 rendered 全文，維持「agent 必須照實說沒有紀錄，不得腦補」。
# ---------------------------------------------------------------------------

#: 無卡（或補登前）資料集在人可讀渲染／API note 欄位裡固定使用的句子
#: （PLAN.md Q.3：「agent 必須照實說『沒有紀錄』，不得腦補」）。
NO_CARD_NOTE = (
    "此版本未登記資料卡（建立於資料卡制度之前），"
    "製作方式與用途無紀錄——可事後補登。"
)

#: `counts`（自訂數量，如 train/val/test 切分數、類別數量）只接受純量值
#: ——不接受巢狀物件/列表，維持「人可讀渲染」時能直接逐項列出。
_ALLOWED_COUNT_VALUE_TYPES = (str, int, float)


def validate_card_fields(description: Optional[str], method: Optional[str]) -> None:
    """`description`／`method` strip 後都不可為空——這是使用者定的規則
    （「資料集必須明確記錄製作方式與數量」），登記新版本與事後 PATCH 補登
    共用同一套驗證。空白字元（全形/半形空格、換行）視同空字串。"""
    if not (description or "").strip():
        raise InvalidDatasetCardError(
            "資料集必須明確記錄製作方式與數量：description（這版是什麼）不可為空"
        )
    if not (method or "").strip():
        raise InvalidDatasetCardError(
            "資料集必須明確記錄製作方式與數量：method（怎麼做的）不可為空"
        )


def _validate_counts(counts: Optional[dict]) -> None:
    if counts is None:
        return
    if not isinstance(counts, dict):
        raise InvalidDatasetCardError("counts 必須是一個 JSON 物件（key -> 數值/字串）")
    for key, value in counts.items():
        if not isinstance(value, _ALLOWED_COUNT_VALUE_TYPES):
            raise InvalidDatasetCardError(
                f"counts 欄位 {key!r} 的值型別不合法（{value!r}）："
                "只允許字串／整數／浮點數"
            )


def build_dataset_card(
    description: Optional[str],
    method: Optional[str],
    derived_from: Optional[dict] = None,
    counts: Optional[dict] = None,
) -> dict:
    """組出一份資料卡 dict（存進 `datasets.card` JSON 欄）：
        {description, method, derived_from: {name, version}|None,
         counts_custom: dict|None, created_at, updated_at}
    `description`／`method` 驗證失敗丟 `InvalidDatasetCardError`（呼叫端
    /app/main.py 轉 400）。`created_at`／`updated_at` 在**新建**時相同
    （呼叫端如果是「更新既有卡」要自己保留原 `created_at`，見 PATCH
    `/datasets/{name}/{version}/card` 的實作——這個函式本身不知道「這是
    新建還是更新」，只負責組出一份自洽的新卡）。
    """
    validate_card_fields(description, method)
    _validate_counts(counts)
    now = now_iso()
    return {
        "description": description.strip(),  # type: ignore[union-attr]
        "method": method.strip(),  # type: ignore[union-attr]
        "derived_from": dict(derived_from) if derived_from else None,
        "counts_custom": dict(counts) if counts else None,
        "created_at": now,
        "updated_at": now,
    }


def build_dataset_auto_facts(db: Database, dataset: Dataset) -> dict:
    """`GET /datasets/{name}/{version}/card` 的 `auto_facts`：系統已知、
    不靠資料卡也答得出來的自動事實——檔案數/總大小（從 manifest）、登記
    時間、目前快取在哪幾台機器（從 dataset_cache 表，`db.list_dataset_
    cache()` 沒有依 dataset 過濾的介面，這裡在 Python 端過濾——資料集
    數量級小，不需要為此加新的 DB 查詢方法）。"""
    cached_on = sorted(
        {
            entry.server
            for entry in db.list_dataset_cache()
            if entry.dataset == dataset.name and entry.version == dataset.version
        }
    )
    return {
        "file_count": dataset.manifest.get("file_count"),
        "total_size_bytes": dataset.manifest.get("total_size"),
        "size_bytes": dataset.size_bytes,
        "created_at": dataset.created_at,
        "cached_on": cached_on,
    }


def render_dataset_card(
    name: str, version: str, card: Optional[dict], auto_facts: dict
) -> str:
    """人可讀的 Markdown 段落式渲染（**不是 JSON 堆疊**）——網頁資料集頁
    與 `GET .../card` 的 `rendered` 欄共用這份文字，agent（MCP
    bridge／vLLM）轉述給使用者時也是這份文字的（可能截斷的）原樣。

    `card is None`（階段 16 之前建立、或補登前）時，描述／製作方式段落
    固定寫 `NO_CARD_NOTE`——不留給呼叫端自己組措辭的空間，避免不同呼叫端
    講法不一致，也避免 agent 自己腦補一句聽起來像有紀錄的話。數量段永遠
    照出（`auto_facts` 不受 `card` 是否存在影響）。
    """
    lines: list[str] = [f"# {name}@{version}", ""]

    lines.append("## 描述")
    lines.append(card["description"] if card else NO_CARD_NOTE)
    lines.append("")

    lines.append("## 製作方式")
    lines.append(card["method"] if card else NO_CARD_NOTE)
    lines.append("")

    lines.append("## 數量")
    file_count = auto_facts.get("file_count")
    total_size = auto_facts.get("total_size_bytes")
    lines.append(f"- 檔案數：{file_count if file_count is not None else '未知'}")
    lines.append(f"- 總大小：{total_size if total_size is not None else '未知'} bytes")
    lines.append(f"- 登記時間：{auto_facts.get('created_at') or '未知'}")
    cached_on = auto_facts.get("cached_on") or []
    if cached_on:
        lines.append(f"- 目前快取在：{', '.join(cached_on)}")
    else:
        lines.append("- 目前快取在：（尚無機器快取此版本）")
    counts_custom = card.get("counts_custom") if card else None
    if counts_custom:
        lines.append("- 自訂數量：")
        for key, value in counts_custom.items():
            lines.append(f"  - {key}: {value}")
    lines.append("")

    lines.append("## 衍生自")
    derived_from = card.get("derived_from") if card else None
    if derived_from:
        lines.append(f"- {derived_from.get('name')}@{derived_from.get('version')}")
    else:
        lines.append("- （無，這是原始版本，或建立時未登記衍生來源）")

    return "\n".join(lines)
