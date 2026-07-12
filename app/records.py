"""時間軸合併（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、目標／
方法／進度管理」計畫第 2 節）：把 `experiment_records`／`jobs`／
`coding_runs` 三個既有／新表的資料，在**查詢當下**動態合併成一條時間軸，
不新增任何寫入路徑去同步/複製資料到別的表——設計總覽定案的取捨：零侵入
既有派工流程（`jobs`／`coding_runs` 的 schema、CRUD 一行都沒改）、天然
一致（沒有「時間軸跟原始資料不同步」這種 bug 可能性）、歷史資料自動出現
（這批功能上線前就有的 jobs/coding_runs 立刻能在時間軸看到，不用回填）。

`GET /projects/{name}/timeline`（`app/main.py`）與 agent 工具
`search_experiment_timeline`（`app/agent_tools.py`）共用這裡的
`build_timeline()`，維持「同一套合併邏輯只寫一次」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.db import CodingRun, Database, Job, VALID_RECORD_KINDS

#: 每個時間軸項目 dict 都有 `type`（這筆資料是從哪張表來的：
#: "record"/"job"/"coding_run"）與 `kind`（給前端「kind 勾選」過濾 UI
#: 統一使用的分類值）兩個欄位。`type == "record"` 的項目 `kind` 是
#: `experiment_records.kind`（note/observation/conclusion/decision）；
#: job／coding_run 項目的 `kind` 固定等於各自的 `type` 值，讓前端可以用
#: 同一份 kind 清單（含 job/coding_run 兩個「假 kind」）勾選要不要顯示整個
#: 來源。
_JOB_KIND = "job"
_CODING_RUN_KIND = "coding_run"

#: `db.list_coding_runs()` 預設 `limit=50`——這裡合併用途要拿到「某專案
#: 全部 coding_runs」再自行過濾/排序/截斷（跟 `db.list_jobs(project=...)`
#: 一貫「回傳全部、呼叫端自己切」的既有慣例一致，見 `GET
#: /projects/{name}/activity` 對 jobs 的既有用法），刻意給一個遠大於任何
#: 真實專案可能累積的 coding run 數量的上限，避免因為 `list_coding_runs()`
#: 自己的預設值把結果截斷在合併之前。
_ALL_CODING_RUNS_LIMIT = 100_000


def _truncate(text: Optional[str], limit: int = 200) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _job_timeline_item(job: Job) -> dict:
    """job 摘要：邏輯對齊 `app/main.py` 的 `_job_activity_summary()`（同樣的
    耗時計算方式）＋ `command` 截 200 字（PLAN.md 第 2 節）。**這裡刻意獨立
    複製一份計算邏輯，不是 `from app.main import _job_activity_summary`**
    ——`app/main.py` 反過來要 `from app.records import build_timeline`
    （給 `GET /projects/{name}/timeline` 用），兩邊互相 import 會循環
    import。比照 `app/mcp_bridge.py` 模組 docstring 對複製
    `app/config.py` 的 `_load_dotenv()` 的既有取捨：這是刻意的隔離，不是
    偷懶，兩份邏輯目前一致，之後若耗時計算方式改了要記得手動同步兩邊。"""
    duration_seconds: Optional[int] = None
    if job.started_at:
        try:
            start = datetime.fromisoformat(job.started_at)
            end = (
                datetime.fromisoformat(job.finished_at)
                if job.finished_at
                else datetime.now(timezone.utc)
            )
            duration_seconds = max(0, int((end - start).total_seconds()))
        except ValueError:
            duration_seconds = None
    return {
        "type": _JOB_KIND,
        "kind": _JOB_KIND,
        "ts": job.created_at,
        "id": job.id,
        "status": job.status,
        "exit_code": job.exit_code,
        "duration_seconds": duration_seconds,
        "server": job.server,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "command": _truncate(job.command),
    }


def _coding_run_timeline_item(run: CodingRun) -> dict:
    return {
        "type": _CODING_RUN_KIND,
        "kind": _CODING_RUN_KIND,
        "ts": run.created_at,
        "id": run.id,
        "status": run.status,
        "runner_server": run.runner_server,
        "instruction": _truncate(run.instruction),
        "result_branch": run.result_branch,
        "result_commit": run.result_commit,
        "error_message": _truncate(run.error_message),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _record_timeline_item(record) -> dict:
    return {
        "type": "record",
        "kind": record.kind,
        "ts": record.created_at,
        "id": record.id,
        "title": record.title,
        "content": record.content,
        "author": record.author,
        "job_id": record.job_id,
        "coding_run_id": record.coding_run_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def build_timeline(
    db: Database,
    project: str,
    *,
    q: Optional[str] = None,
    limit: int = 20,
    before_ts: Optional[str] = None,
    kinds: Optional[list[str]] = None,
) -> dict:
    """把 `experiment_records`／`jobs`／`coding_runs` 三個來源合併成一條
    時間軸。

    `limit`：頁面大小，夾在 `[1, 50]`（呼叫端／Pydantic 通常已經驗證過，
    這裡防禦性再夾一次，不信任呼叫端）。

    `before_ts`：分頁游標，字串比較 `created_at < before_ts`——
    `app.db.now_iso()` 回傳的時間字串含微秒，字典序比較足夠安全（同一微秒
    內兩筆不同來源的紀錄同時出現的機率可忽略），不需要額外維護一個跨三張
    表的全域自增 id 當游標；換頁時前端／agent 原樣把上一頁最後一筆的
    `ts` 傳回來即可。

    `q`：跨三源的關鍵字（不分大小寫子字串）——`experiment_records` 比對
    `title`/`content`（DB 層 LIKE，見 `Database.list_experiment_records()`）；
    `jobs` 比對 `command`；`coding_runs` 比對
    `instruction`/`result_branch`/`error_message`。後兩者在 Python 層比對
    ——`db.list_jobs()`/`db.list_coding_runs()` 本來就沒有 LIKE 參數，這裡
    刻意不修改那兩張表既有的查詢介面（零侵入既有派工流程，見模組
    docstring／設計總覽），對這個內部工具的資料量（單一專案的任務數量）
    來說，抓全部再過濾的成本可以忽略。

    `kinds`：過濾項目類型，值域是 `note`/`observation`/`conclusion`/
    `decision`（`experiment_records` 各 `kind`）＋ `"job"`／`"coding_run"`
    （另外兩個來源整體，當成「假 kind」用同一份勾選 UI 過濾）；省略或空
    值＝不過濾（三源全部混合）。

    **正確性（k-way merge）**：每個來源各自抓「這一頁大小 `limit` + 1
    筆」（已按 `created_at DESC` 排序、套用 `before_ts`/`q`/`kind` 過濾），
    三源合併後整體再排序一次、切前 `limit` 筆——只要每個來源都拿了「頁大小
    + 1」筆，合併結果的前 `limit` 筆保證正確：任一項目若在自己來源內排名
    超過 `limit`（也就是這裡沒抓到的部分），光是同來源內就已經有至少
    `limit` 筆排名比它高，不可能擠進三源合併後的整體前 `limit` 名，所以
    漏抓它不影響這一頁的正確性；多抓的那 1 筆只用來判斷 `has_more`，不會
    出現在回傳的 `items` 裡（見下方 `has_more` 計算）。
    """
    limit = max(1, min(int(limit or 20), 50))
    fetch_n = limit + 1
    kind_filter = set(kinds) if kinds else None

    pool: list[dict] = []

    # ---- 來源 1：experiment_records（DB 層已支援 q/kind/before_ts/limit）--
    if kind_filter is None or (kind_filter & VALID_RECORD_KINDS):
        record_kinds = (kind_filter & VALID_RECORD_KINDS) if kind_filter else None
        if record_kinds is None:
            pool.extend(
                _record_timeline_item(r)
                for r in db.list_experiment_records(
                    project, q=q, limit=fetch_n, before_ts=before_ts
                )
            )
        else:
            # kinds 同時勾選多個 record kind：list_experiment_records() 的
            # kind 參數一次只接受一個值，這裡逐一查詢再合併（每種 kind 各
            # 自的紀錄不會重複，不需要額外去重）。
            for k in sorted(record_kinds):
                pool.extend(
                    _record_timeline_item(r)
                    for r in db.list_experiment_records(
                        project, q=q, kind=k, limit=fetch_n, before_ts=before_ts
                    )
                )

    # ---- 來源 2：jobs（Python 層套用 q/before_ts 過濾，見上方 docstring）--
    if kind_filter is None or _JOB_KIND in kind_filter:
        q_lower = q.lower() if q else None
        matched_jobs = [
            job
            for job in db.list_jobs(project=project)
            if (not before_ts or job.created_at < before_ts)
            and (not q_lower or q_lower in (job.command or "").lower())
        ]
        matched_jobs.sort(key=lambda j: j.created_at, reverse=True)
        pool.extend(_job_timeline_item(j) for j in matched_jobs[:fetch_n])

    # ---- 來源 3：coding_runs（同上，Python 層過濾）------------------------
    if kind_filter is None or _CODING_RUN_KIND in kind_filter:
        q_lower = q.lower() if q else None
        all_runs = db.list_coding_runs(project=project, limit=_ALL_CODING_RUNS_LIMIT)
        matched_runs = [
            run
            for run in all_runs
            if (not before_ts or run.created_at < before_ts)
            and (
                not q_lower
                or q_lower in (run.instruction or "").lower()
                or q_lower in (run.result_branch or "").lower()
                or q_lower in (run.error_message or "").lower()
            )
        ]
        matched_runs.sort(key=lambda r: r.created_at, reverse=True)
        pool.extend(_coding_run_timeline_item(r) for r in matched_runs[:fetch_n])

    pool.sort(key=lambda item: item["ts"], reverse=True)
    has_more = len(pool) > limit
    items = pool[:limit]
    next_before_ts = items[-1]["ts"] if items else None

    return {"items": items, "next_before_ts": next_before_ts, "has_more": has_more}
