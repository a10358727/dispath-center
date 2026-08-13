"""app/main.py：專案詳情頁計畫（PLAN.md「專案詳情頁：可點入、調派、實驗
紀錄時間軸、目標／方法／進度管理」）第 3 節的六支端點：

    GET    /projects/{name}/detail
    PATCH  /projects/{name}
    GET    /projects/{name}/timeline
    POST   /projects/{name}/records
    PATCH  /projects/{name}/records/{id}
    DELETE /projects/{name}/records/{id}
"""

from __future__ import annotations

from app.audit import read_audit


class RecordingSSH:
    """同 `tests/test_project_delete.py` 的既有寫法：只記錄有沒有被呼叫
    過，`GET /projects/{name}/detail` 絕不該對任何機器發起 SSH。"""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        return None


# ---------------------------------------------------------------------------
# GET /projects/{name}/detail
# ---------------------------------------------------------------------------


def test_get_project_detail_shape_and_no_ssh_calls(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1", require_tag="gpu")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = RecordingSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.get("/projects/proj1/detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["name"] == "proj1"
    assert body["project"]["require_tag"] == "gpu"
    assert body["project"]["goal"] is None
    assert len(body["instances"]) == 1
    assert body["instances"][0]["server"] == "server-a"
    #: PLAN.md 2026-07-11 版 §14 切片 1/2:instance 序列化帶 state（切片 2
    #: reconcile 之前一律 'unknown'）。
    assert body["instances"][0]["state"] == "unknown"
    assert "server-a" in body["server_states"]
    assert body["hub"] == {"exists": False, "head": None, "last_sync": None}
    #: 切片 4/5:版本歷史欄位一定存在,還沒同步過 hub 時是空清單(不是
    #: 缺欄位、不是 null)。
    assert body["versions"] == []

    # 詳情頁的 GET 不該碰任何機器（跟 GET /activity 刻意分開）。
    assert ssh.calls == []


def test_get_project_detail_includes_version_history(api_client):
    """PLAN.md 2026-07-11 版 §14 切片 5:`versions` 帶出
    `db.get_or_create_project_version()` 登記過的版本歷史,新到舊。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.get_or_create_project_version("proj1", "commit1", git_ref="main")
    db.get_or_create_project_version("proj1", "commit2", git_ref="main")

    resp = client.get("/projects/proj1/detail")
    versions = resp.json()["versions"]
    assert len(versions) == 2
    assert versions[0]["git_commit"] == "commit2"  # 新到舊
    assert versions[1]["git_commit"] == "commit1"


# ---------------------------------------------------------------------------
# 前端 smoke：PLAN.md 2026-07-11 版 §14 切片 5——instance state 徽章與
# 版本歷史區塊的渲染關鍵字（static/index.html 本批新增）。
# ---------------------------------------------------------------------------


def test_index_page_renders_instance_state_badge_helper(api_client):
    client, _main = api_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "instanceStateBadgeHtml" in resp.text
    assert "badge diverged" in resp.text or "diverged" in resp.text
    assert "版本歷史" in resp.text


def test_get_project_detail_not_found_404(api_client):
    client, _main_module = api_client
    resp = client.get("/projects/nope/detail")
    assert resp.status_code == 404


def test_get_project_detail_does_not_write_activity_audit(api_client):
    """`GET /projects/{name}/activity` 每次呼叫都會寫稽核 `project_activity`
    ——`/detail` 刻意不是那條路徑，不該留下這個稽核事件。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    client.get("/projects/proj1/detail")

    records = read_audit(main_module.app_state.config.audit_path)
    assert not any(r["action"] == "project_activity" for r in records)


# ---------------------------------------------------------------------------
# PATCH /projects/{name}
# ---------------------------------------------------------------------------


def test_patch_project_updates_only_given_fields(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.patch("/projects/proj1", json={"goal": "把準確率衝到 95%"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["goal"] == "把準確率衝到 95%"
    assert body["optimization_notes"] is None
    assert body["progress"] is None

    resp2 = client.patch("/projects/proj1", json={"progress": "第 3 輪訓練中"})
    assert resp2.status_code == 200
    body2 = resp2.json()
    # 前一次 PATCH 的 goal 不受這次影響（exclude_unset：只更新有帶的欄位）。
    assert body2["goal"] == "把準確率衝到 95%"
    assert body2["progress"] == "第 3 輪訓練中"


def test_patch_project_can_update_summary_too(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.patch("/projects/proj1", json={"summary": "新的摘要"})
    assert resp.status_code == 200
    assert resp.json()["summary"] == "新的摘要"


def test_patch_project_empty_body_400(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.patch("/projects/proj1", json={})
    assert resp.status_code == 400


def test_patch_project_not_found_404(api_client):
    client, _main_module = api_client
    resp = client.patch("/projects/nope", json={"goal": "x"})
    assert resp.status_code == 404


def test_patch_project_writes_audit_with_field_names_not_full_text(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    long_text = "x" * 5000
    resp = client.patch("/projects/proj1", json={"goal": long_text, "progress": "y"})
    assert resp.status_code == 200

    records = main_module.app_state.db.list_durable_audit_events(limit=100)
    updated = [r for r in records if r["action"] == "project_updated"]
    assert len(updated) == 1
    assert updated[0]["resource_type"] == "project"
    assert updated[0]["params"]["field_names"] == ["goal", "progress"]
    assert updated[0]["params"]["field_count"] == 2
    # 不把整段文字塞進稽核紀錄。
    assert long_text not in str(updated[0]["params"])


# ---------------------------------------------------------------------------
# GET /projects/{name}/timeline
# ---------------------------------------------------------------------------


def test_get_timeline_returns_merged_items(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "手動筆記")
    db.insert_job(command="python train.py", project="proj1")

    resp = client.get("/projects/proj1/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and "next_before_ts" in body and "has_more" in body
    types = {item["type"] for item in body["items"]}
    assert types == {"record", "job"}


def test_timeline_hides_engineering_executor_command_and_does_not_search_it(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    secret = "synthetic-timeline-secret-123456789"
    private_path = "/home/runner/private/task-42"
    db.insert_job(
        command=f"cd {private_path} && AUTHORIZATION='Bearer {secret}' codex exec",
        type="coding",
        project="proj1",
        engineering_task_id="8da8c173-f0f5-4e0b-b67b-3aad07155182",
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )

    body = client.get("/projects/proj1/timeline").json()
    encoded = repr(body)
    assert "Run Codex agent in an isolated worktree" in encoded
    assert secret not in encoded
    assert private_path not in encoded

    searched = client.get(
        "/projects/proj1/timeline", params={"q": secret}
    ).json()
    assert searched["items"] == []


def test_get_timeline_respects_limit_and_pagination(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    for i in range(5):
        db.insert_experiment_record("proj1", f"note {i}")

    resp = client.get("/projects/proj1/timeline", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True

    resp2 = client.get(
        "/projects/proj1/timeline",
        params={"limit": 2, "before_ts": body["next_before_ts"]},
    )
    body2 = resp2.json()
    assert len(body2["items"]) == 2
    ids_page1 = {item["id"] for item in body["items"]}
    ids_page2 = {item["id"] for item in body2["items"]}
    assert ids_page1.isdisjoint(ids_page2)


def test_get_timeline_q_filters_across_sources(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "跟 needle 相關")
    db.insert_experiment_record("proj1", "無關內容")
    db.insert_job(command="python train.py", project="proj1")

    resp = client.get("/projects/proj1/timeline", params={"q": "needle"})
    body = resp.json()
    assert len(body["items"]) == 1
    assert "needle" in body["items"][0]["content"]


def test_get_timeline_kinds_filter_comma_separated(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.insert_experiment_record("proj1", "a note", kind="note")
    db.insert_experiment_record("proj1", "a decision", kind="decision")
    db.insert_job(command="python train.py", project="proj1")

    resp = client.get("/projects/proj1/timeline", params={"kinds": "decision,job"})
    body = resp.json()
    kinds = {item["kind"] for item in body["items"]}
    assert kinds == {"decision", "job"}


def test_get_timeline_not_found_404(api_client):
    client, _main_module = api_client
    resp = client.get("/projects/nope/timeline")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /projects/{name}/records
# ---------------------------------------------------------------------------


def test_create_experiment_record_success(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.post(
        "/projects/proj1/records",
        json={"content": "觀察到 loss 下降", "kind": "observation", "title": "第一次觀察"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"] == "proj1"
    assert body["kind"] == "observation"
    assert body["title"] == "第一次觀察"
    assert body["author"] == "user"  # 網頁呼叫預設 user

    assert len(db.list_experiment_records("proj1")) == 1


def test_create_experiment_record_default_kind_is_note(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.post("/projects/proj1/records", json={"content": "隨手筆記"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "note"


def test_create_experiment_record_invalid_kind_400(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.post("/projects/proj1/records", json={"content": "x", "kind": "bogus"})
    assert resp.status_code == 400
    assert db.list_experiment_records("proj1") == []


def test_create_experiment_record_not_found_404(api_client):
    client, _main_module = api_client
    resp = client.post("/projects/nope/records", json={"content": "x"})
    assert resp.status_code == 404


def test_create_experiment_record_writes_audit(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.post("/projects/proj1/records", json={"content": "x"})
    record_id = resp.json()["id"]

    records = db.list_durable_audit_events(limit=100)
    created = [r for r in records if r["action"] == "experiment_record_created"]
    assert len(created) == 1
    assert created[0]["params"]["project"] == "proj1"
    assert created[0]["params"]["record_id"] == record_id
    assert created[0]["params"]["author"] == "user"
    assert created[0]["params"]["job_linked"] is False
    assert created[0]["params"]["coding_run_linked"] is False
    assert "content" not in created[0]["params"]
    assert "title" not in created[0]["params"]
    assert created[0]["resource_type"] == "experiment_record"
    assert created[0]["resource_id"] == str(record_id)


def test_create_experiment_record_accepts_agent_author_override(api_client):
    """給 `app/mcp_bridge.py` 的 `add_experiment_record` 代理工具用
    （PLAN.md 專案詳情頁計畫第 4 節：帶 `author="agent:chatgpt"`）——這支
    端點本身是 AUTH_TOKEN 保護的可信呼叫端，跟網頁前端走同一支端點。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.post(
        "/projects/proj1/records", json={"content": "x", "author": "agent:chatgpt"}
    )
    assert resp.status_code == 200
    assert resp.json()["author"] == "agent:chatgpt"


def test_create_experiment_record_invalid_author_400(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.post("/projects/proj1/records", json={"content": "x", "author": "robot"})
    assert resp.status_code == 400
    assert db.list_experiment_records("proj1") == []


def test_create_experiment_record_with_job_id_link(api_client):
    """修正 2（Fable 最終審查）：POST /records 開放 job_id 弱關聯——不驗證
    所指 job 是否存在，回傳與 DB 內容都要帶正確 job_id。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    job_id = db.insert_job(command="python train.py", project="proj1")

    resp = client.post(
        "/projects/proj1/records", json={"content": "針對這次執行的補充", "job_id": job_id}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["coding_run_id"] is None

    record = db.get_experiment_record(body["id"])
    assert record.job_id == job_id
    assert record.coding_run_id is None


def test_create_experiment_record_with_coding_run_id_link(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    run_id = db.insert_coding_run(
        approval_id=1, project="proj1", runner_server="server-c", instruction="fix bug"
    )

    resp = client.post(
        "/projects/proj1/records",
        json={"content": "Codex 這次改碼的補充說明", "coding_run_id": run_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["coding_run_id"] == run_id
    assert body["job_id"] is None

    record = db.get_experiment_record(body["id"])
    assert record.coding_run_id == run_id


def test_create_experiment_record_with_unrelated_job_id_not_validated(api_client):
    """弱關聯，不驗證所指 job 是否存在／屬於這個專案（跟 DB 沒有 FK 的
    設計一致，計畫第 1 節）。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.post(
        "/projects/proj1/records", json={"content": "x", "job_id": 99999, "coding_run_id": 88888}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == 99999
    assert body["coding_run_id"] == 88888


# ---------------------------------------------------------------------------
# PATCH /projects/{name}/records/{id}
# ---------------------------------------------------------------------------


def test_patch_experiment_record_success(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "原內容", title="原標題")

    resp = client.patch(
        f"/projects/proj1/records/{record_id}",
        json={"content": "新內容", "kind": "decision"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "新內容"
    assert body["kind"] == "decision"
    assert body["title"] == "原標題"  # 沒帶的欄位不動


def test_patch_experiment_record_empty_body_400(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")

    resp = client.patch(f"/projects/proj1/records/{record_id}", json={})
    assert resp.status_code == 400


def test_patch_experiment_record_invalid_kind_400(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")

    resp = client.patch(f"/projects/proj1/records/{record_id}", json={"kind": "bogus"})
    assert resp.status_code == 400


def test_patch_experiment_record_not_found_404(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.patch("/projects/proj1/records/999", json={"content": "x"})
    assert resp.status_code == 404


def test_patch_experiment_record_project_mismatch_404(api_client):
    """紀錄存在，但屬於別的專案——用這個專案的名字去改，一律 404（不能用
    猜 id 的方式跨專案改到別人的紀錄）。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project("proj2", "/repo/proj2")
    record_id = db.insert_experiment_record("proj2", "屬於 proj2 的紀錄")

    resp = client.patch(f"/projects/proj1/records/{record_id}", json={"content": "x"})
    assert resp.status_code == 404
    assert db.get_experiment_record(record_id).content == "屬於 proj2 的紀錄"


def test_patch_experiment_record_writes_audit(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")

    client.patch(f"/projects/proj1/records/{record_id}", json={"content": "新內容"})

    records = db.list_durable_audit_events(limit=100)
    updated = [r for r in records if r["action"] == "experiment_record_updated"]
    assert len(updated) == 1
    assert updated[0]["params"]["record_id"] == record_id
    assert updated[0]["params"]["fields"] == ["content"]
    assert "新內容" not in updated[0]["params"]


# ---------------------------------------------------------------------------
# DELETE /projects/{name}/records/{id}
# ---------------------------------------------------------------------------


def test_delete_experiment_record_success(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")

    resp = client.delete(f"/projects/proj1/records/{record_id}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": record_id}
    assert db.get_experiment_record(record_id) is None


def test_delete_experiment_record_not_found_404(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")

    resp = client.delete("/projects/proj1/records/999")
    assert resp.status_code == 404


def test_delete_experiment_record_project_mismatch_404(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.insert_project("proj2", "/repo/proj2")
    record_id = db.insert_experiment_record("proj2", "屬於 proj2 的紀錄")

    resp = client.delete(f"/projects/proj1/records/{record_id}")
    assert resp.status_code == 404
    assert db.get_experiment_record(record_id) is not None


def test_delete_experiment_record_writes_audit(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    record_id = db.insert_experiment_record("proj1", "內容")

    client.delete(f"/projects/proj1/records/{record_id}")

    records = db.list_durable_audit_events(limit=100)
    deleted = [r for r in records if r["action"] == "experiment_record_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["params"]["project"] == "proj1"
    assert deleted[0]["params"]["record_id"] == record_id


# ---------------------------------------------------------------------------
# GET /projects/{name}/activity 既有端點：確認新三欄自動帶到（計畫第 3 節
# 「`_project_to_dict` 加三欄後 `/activity` 自動帶到」）。
# ---------------------------------------------------------------------------


def test_project_activity_endpoint_includes_new_doc_fields(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "/repo/proj1")
    db.update_project("proj1", goal="目標文字")

    resp = client.get("/projects/proj1/activity")
    assert resp.status_code == 200
    assert resp.json()["project"]["goal"] == "目標文字"
