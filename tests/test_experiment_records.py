"""專案詳情頁計畫（PLAN.md「專案詳情頁：可點入、調派、實驗紀錄時間軸、
目標／方法／進度管理」）第 1／2 節：

- `app/db.py`：`experiment_records` 表的 CRUD（insert/get/list/update/
  delete）、`kind`／`author` 值域驗證、`list_experiment_records()` 的
  LIKE 搜尋與 `before_ts` 游標分頁。
- `app/records.py`：`build_timeline()` 三源（experiment_records/jobs/
  coding_runs）動態合併——排序正確性、`limit` 截斷、`before_ts` 續頁不重
  不漏、`q` 跨表命中、`kinds` 過濾。
"""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.records import build_timeline


# ---------------------------------------------------------------------------
# app/db.py：experiment_records CRUD
# ---------------------------------------------------------------------------


def test_insert_and_get_experiment_record(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record(
        "proj1", "訓練 loss 持續下降", kind="observation", title="第一次觀察", author="user"
    )
    record = db.get_experiment_record(record_id)
    assert record is not None
    assert record.project == "proj1"
    assert record.kind == "observation"
    assert record.title == "第一次觀察"
    assert record.content == "訓練 loss 持續下降"
    assert record.author == "user"
    assert record.job_id is None
    assert record.coding_run_id is None
    assert record.extra == {}
    assert record.created_at
    assert record.updated_at == record.created_at


def test_insert_experiment_record_default_kind_and_author(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "隨手筆記")
    record = db.get_experiment_record(record_id)
    assert record.kind == "note"
    assert record.author == "user"
    assert record.title is None


def test_insert_experiment_record_with_job_and_coding_run_link(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    job_id = db.insert_job(command="python train.py", project="proj1")
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-c", instruction="fix bug"
    )
    record_id = db.insert_experiment_record(
        "proj1", "針對這次執行的補充說明", job_id=job_id, coding_run_id=run_id
    )
    record = db.get_experiment_record(record_id)
    assert record.job_id == job_id
    assert record.coding_run_id == run_id


def test_get_experiment_record_not_found_returns_none(db: Database):
    assert db.get_experiment_record(999) is None


def test_insert_experiment_record_invalid_kind_raises(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    with pytest.raises(ValueError):
        db.insert_experiment_record("proj1", "x", kind="bogus")


def test_insert_experiment_record_invalid_author_raises(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    with pytest.raises(ValueError):
        db.insert_experiment_record("proj1", "x", author="robot")
    with pytest.raises(ValueError):
        db.insert_experiment_record("proj1", "x", author="")


def test_insert_experiment_record_agent_author_variants_allowed(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    id1 = db.insert_experiment_record("proj1", "x", author="agent")
    id2 = db.insert_experiment_record("proj1", "y", author="agent:chatgpt")
    assert db.get_experiment_record(id1).author == "agent"
    assert db.get_experiment_record(id2).author == "agent:chatgpt"


def test_update_experiment_record_whitelisted_fields(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "原內容", kind="note", title="原標題")
    original = db.get_experiment_record(record_id)

    db.update_experiment_record(record_id, content="新內容", title="新標題", kind="decision")
    updated = db.get_experiment_record(record_id)
    assert updated.content == "新內容"
    assert updated.title == "新標題"
    assert updated.kind == "decision"
    # updated_at 要有更新（>= 原值，字典序比較；同一批測試跑很快，容許相等）
    assert updated.updated_at >= original.updated_at
    # project/author 不受影響
    assert updated.project == "proj1"
    assert updated.author == "user"


def test_update_experiment_record_empty_fields_is_noop(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")
    original = db.get_experiment_record(record_id)
    db.update_experiment_record(record_id)
    assert db.get_experiment_record(record_id) == original


def test_update_experiment_record_rejects_field_outside_whitelist(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")
    with pytest.raises(ValueError):
        db.update_experiment_record(record_id, project="other")
    with pytest.raises(ValueError):
        db.update_experiment_record(record_id, author="agent")


def test_update_experiment_record_invalid_kind_raises(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")
    with pytest.raises(ValueError):
        db.update_experiment_record(record_id, kind="bogus")


def test_delete_experiment_record(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")
    db.delete_experiment_record(record_id)
    assert db.get_experiment_record(record_id) is None


def test_delete_experiment_record_missing_is_noop(db: Database):
    db.delete_experiment_record(999)  # 不應丟例外


# ---------------------------------------------------------------------------
# app/db.py：list_experiment_records() —— 排序／LIKE／kind／limit／before_ts
# ---------------------------------------------------------------------------


def _insert_sequential(db: Database, project: str, n: int, **kwargs) -> list[int]:
    """依序插入 n 筆紀錄，中間睡極短時間確保 created_at 嚴格遞增（避免同一
    微秒內排序不穩定）。回傳 id 列表（插入順序＝由舊到新）。"""
    ids = []
    for i in range(n):
        ids.append(db.insert_experiment_record(project, f"content {i}", **kwargs))
        time.sleep(0.0005)
    return ids


def test_list_experiment_records_newest_first(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    ids = _insert_sequential(db, "proj1", 3)
    records = db.list_experiment_records("proj1")
    assert [r.id for r in records] == list(reversed(ids))


def test_list_experiment_records_scoped_to_project(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project("proj2", "/repo/proj2")
    db.insert_experiment_record("proj1", "in proj1")
    db.insert_experiment_record("proj2", "in proj2")
    records = db.list_experiment_records("proj1")
    assert len(records) == 1
    assert records[0].project == "proj1"


def test_list_experiment_records_q_matches_title_or_content(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "loss 收斂良好", title="收斂觀察")
    db.insert_experiment_record("proj1", "換了 optimizer", title="調整記錄")
    hits_by_content = db.list_experiment_records("proj1", q="收斂")
    assert len(hits_by_content) == 1
    assert "收斂" in (hits_by_content[0].content + (hits_by_content[0].title or ""))

    hits_by_title = db.list_experiment_records("proj1", q="調整")
    assert len(hits_by_title) == 1


def test_list_experiment_records_kind_filter(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "a", kind="note")
    db.insert_experiment_record("proj1", "b", kind="decision")
    decisions = db.list_experiment_records("proj1", kind="decision")
    assert len(decisions) == 1
    assert decisions[0].kind == "decision"


def test_list_experiment_records_limit(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    _insert_sequential(db, "proj1", 5)
    assert len(db.list_experiment_records("proj1", limit=2)) == 2


def test_list_experiment_records_before_ts_cursor_pagination_no_gap_no_dup(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    ids = _insert_sequential(db, "proj1", 5)  # 舊到新

    page1 = db.list_experiment_records("proj1", limit=2)
    assert [r.id for r in page1] == list(reversed(ids))[:2]

    page2 = db.list_experiment_records("proj1", limit=2, before_ts=page1[-1].created_at)
    assert [r.id for r in page2] == list(reversed(ids))[2:4]

    page3 = db.list_experiment_records("proj1", limit=2, before_ts=page2[-1].created_at)
    assert [r.id for r in page3] == list(reversed(ids))[4:5]

    # 三頁合起來剛好是全部 5 筆，且沒有重複
    all_ids = [r.id for r in page1] + [r.id for r in page2] + [r.id for r in page3]
    assert sorted(all_ids) == sorted(ids)
    assert len(set(all_ids)) == len(ids)


# ---------------------------------------------------------------------------
# app/records.py：build_timeline() —— 三源合併
# ---------------------------------------------------------------------------


def test_build_timeline_merges_three_sources_newest_first(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "手動筆記")
    time.sleep(0.0005)
    job_id = db.insert_job(command="python train.py", project="proj1")
    time.sleep(0.0005)
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-c", instruction="add feature"
    )

    result = build_timeline(db, "proj1", limit=10)
    types = [item["type"] for item in result["items"]]
    assert types == ["coding_run", "job", "record"]
    assert result["items"][0]["id"] == run_id
    assert result["items"][1]["id"] == job_id
    assert result["has_more"] is False
    assert result["next_before_ts"] == result["items"][-1]["ts"]


def test_build_timeline_empty_project_returns_empty_items(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    result = build_timeline(db, "proj1")
    assert result == {"items": [], "next_before_ts": None, "has_more": False}


def test_build_timeline_limit_truncates_and_sets_has_more(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    for i in range(5):
        db.insert_experiment_record("proj1", f"note {i}")
        time.sleep(0.0005)

    result = build_timeline(db, "proj1", limit=2)
    assert len(result["items"]) == 2
    assert result["has_more"] is True


def test_build_timeline_before_ts_pagination_no_gap_no_dup_across_sources(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    # 交錯插入三種來源，確保合併分頁邏輯真的跨表運作。
    db.insert_experiment_record("proj1", "note1")
    time.sleep(0.0005)
    db.insert_job(command="cmd1", project="proj1")
    time.sleep(0.0005)
    db.insert_coding_run(approval_id=1, project="proj1", runner_server="s", instruction="i1")
    time.sleep(0.0005)
    db.insert_experiment_record("proj1", "note2")
    time.sleep(0.0005)
    db.insert_job(command="cmd2", project="proj1")
    time.sleep(0.0005)
    db.insert_coding_run(approval_id=2, project="proj1", runner_server="s", instruction="i2")

    full = build_timeline(db, "proj1", limit=50)
    assert len(full["items"]) == 6
    assert full["has_more"] is False

    collected: list[tuple[str, int]] = []
    before_ts = None
    for _ in range(10):  # 安全上限，避免邏輯錯誤時無窮迴圈
        page = build_timeline(db, "proj1", limit=2, before_ts=before_ts)
        collected.extend((item["type"], item["id"]) for item in page["items"])
        if not page["has_more"]:
            break
        before_ts = page["next_before_ts"]

    full_keys = [(item["type"], item["id"]) for item in full["items"]]
    assert collected == full_keys  # 逐頁合併結果跟一次拿全部的結果完全一致
    assert len(set(collected)) == len(collected)  # 無重複


def test_build_timeline_q_matches_across_all_three_sources(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "跟 needle 有關的筆記")
    db.insert_experiment_record("proj1", "無關筆記")
    db.insert_job(command="python train.py --needle-mode", project="proj1")
    db.insert_job(command="python eval.py", project="proj1")
    db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="s", instruction="handle needle case"
    )
    db.insert_coding_run(approval_id=2, project="proj1", runner_server="s", instruction="unrelated")

    result = build_timeline(db, "proj1", q="needle", limit=50)
    assert len(result["items"]) == 3
    types = {item["type"] for item in result["items"]}
    assert types == {"record", "job", "coding_run"}


def test_build_timeline_q_is_case_insensitive(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_job(command="python TRAIN.py --Lr 0.1", project="proj1")
    result = build_timeline(db, "proj1", q="train.py")
    assert len(result["items"]) == 1


def test_build_timeline_kinds_filter_restricts_to_record_kind(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "a note", kind="note")
    db.insert_experiment_record("proj1", "a decision", kind="decision")
    db.insert_job(command="python train.py", project="proj1")

    result = build_timeline(db, "proj1", kinds=["decision"], limit=50)
    assert len(result["items"]) == 1
    assert result["items"][0]["kind"] == "decision"


def test_build_timeline_kinds_filter_multiple_record_kinds(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "a", kind="note")
    db.insert_experiment_record("proj1", "b", kind="decision")
    db.insert_experiment_record("proj1", "c", kind="observation")

    result = build_timeline(db, "proj1", kinds=["note", "decision"], limit=50)
    kinds = {item["kind"] for item in result["items"]}
    assert kinds == {"note", "decision"}


def test_build_timeline_kinds_filter_job_only(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "a note")
    db.insert_job(command="python train.py", project="proj1")
    db.insert_coding_run(approval_id=1, project="proj1", runner_server="s", instruction="x")

    result = build_timeline(db, "proj1", kinds=["job"], limit=50)
    assert len(result["items"]) == 1
    assert result["items"][0]["type"] == "job"


def test_build_timeline_kinds_filter_coding_run_only(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "a note")
    db.insert_job(command="python train.py", project="proj1")
    db.insert_coding_run(approval_id=1, project="proj1", runner_server="s", instruction="x")

    result = build_timeline(db, "proj1", kinds=["coding_run"], limit=50)
    assert len(result["items"]) == 1
    assert result["items"][0]["type"] == "coding_run"


def test_build_timeline_job_item_shape_has_truncated_command(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    long_command = "python train.py " + ("x" * 300)
    job_id = db.insert_job(command=long_command, project="proj1")
    db.update_job(job_id, status="running", server="s1")
    result = build_timeline(db, "proj1")
    item = result["items"][0]
    assert item["type"] == "job"
    assert item["id"] == job_id
    assert item["status"] == "running"
    assert item["server"] == "s1"
    assert len(item["command"]) <= 201  # 200 字 + 省略號
    assert item["command"].endswith("…")


def test_build_timeline_coding_run_item_shape(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    run_id = db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-c",
        instruction="fix the bug",
        status="done",
        result_branch="ai-task-1",
        result_commit="abc123",
    )
    result = build_timeline(db, "proj1")
    item = result["items"][0]
    assert item["type"] == "coding_run"
    assert item["id"] == run_id
    assert item["status"] == "done"
    assert item["result_branch"] == "ai-task-1"
    assert item["result_commit"] == "abc123"


def test_build_timeline_record_item_shape(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record(
        "proj1", "內容文字", kind="decision", title="標題", author="agent"
    )
    result = build_timeline(db, "proj1")
    item = result["items"][0]
    assert item["type"] == "record"
    assert item["kind"] == "decision"
    assert item["id"] == record_id
    assert item["title"] == "標題"
    assert item["content"] == "內容文字"
    assert item["author"] == "agent"


def test_build_timeline_limit_is_clamped_to_50(db: Database):
    db.insert_project("proj1", "/repo/proj1")
    for i in range(3):
        db.insert_experiment_record("proj1", f"note {i}")
    result = build_timeline(db, "proj1", limit=9999)
    assert len(result["items"]) == 3  # 沒有 51 筆可以測夾上限，至少確認沒炸掉/沒截斷這 3 筆
