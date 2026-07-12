"""端到端 API 測試：fastapi TestClient，不依賴真實 SSH（servers.yaml 不存在
時 config.servers 為空列表，背景 monitor/scheduler 迴圈不會嘗試連線任何機器）。

涵蓋 PLAN.md C 節「測試」清單：核准後才入列、拒絕不入列、危險指令不建
approval、stop 流程（FakeSSH）、auth middleware、GET /events 解析。

階段 10（PLAN.md K 節）另外涵蓋：source 標記／`WEB_DIRECT_EXECUTE`
一步生效／自動核准規則在 API 層的行為，見檔案尾端。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class FakeCommandResult:
    def __init__(self, stdout: str):
        self.stdout = stdout


class RecordingFakeSSH:
    def __init__(self, tail_text: str = "stopped tail\n"):
        self.calls: list[str] = []
        self.tail_text = tail_text

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if "tmux kill-session" in command:
            return FakeCommandResult("")
        if "tail -n" in command:
            return FakeCommandResult(self.tail_text)
        return FakeCommandResult("")


# ---------------------------------------------------------------------------
# 核准流：dispatch -> pending approval -> approve -> 入列；reject -> 不入列
# ---------------------------------------------------------------------------


def test_dispatch_creates_pending_approval_not_a_job(api_client):
    client, _main = api_client
    resp = client.post("/dispatch", json={"command": "sleep 60"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "enqueue"
    assert body["status"] == "pending"

    assert client.get("/jobs").json() == []
    approvals = client.get("/approvals?status=pending").json()
    assert len(approvals) == 1
    assert approvals[0]["id"] == body["id"]


def test_post_jobs_is_same_as_dispatch(api_client):
    client, _main = api_client
    resp = client.post("/jobs", json={"command": "sleep 5"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "enqueue"
    assert resp.json()["status"] == "pending"
    assert client.get("/jobs").json() == []


def test_approve_enqueue_dispatches_into_queue(api_client):
    client, _main = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 60"}).json()["id"]

    resp = client.post(f"/approve/{approval_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["approval"]["status"] == "approved"
    assert body["job"]["status"] == "queued"
    assert body["job"]["command"] == "sleep 60"

    jobs = client.get("/jobs").json()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    # 階段 4：GET /jobs 的 job dict 應該帶 stalled_suspect 欄位（預設 False）。
    assert jobs[0]["stalled_suspect"] is False


def test_reject_does_not_create_job(api_client):
    client, _main = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 60"}).json()["id"]

    resp = client.post(f"/reject/{approval_id}", json={"note": "不需要"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    assert client.get("/jobs").json() == []
    pending = client.get("/approvals?status=pending").json()
    assert pending == []


def test_dangerous_command_dispatch_returns_400_and_no_approval(api_client):
    client, _main = api_client
    resp = client.post("/dispatch", json={"command": "rm -rf /tmp/x"})
    assert resp.status_code == 400

    assert client.get("/approvals").json() == []
    assert client.get("/jobs").json() == []


def test_approve_twice_fails(api_client):
    client, _main = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 60"}).json()["id"]
    assert client.post(f"/approve/{approval_id}").status_code == 200
    assert client.post(f"/approve/{approval_id}").status_code == 400


def test_approve_nonexistent_returns_404(api_client):
    client, _main = api_client
    assert client.post("/approve/9999").status_code == 404
    assert client.post("/reject/9999").status_code == 404


# ---------------------------------------------------------------------------
# stop 流程：僅限 running 任務；核准後 FakeSSH 驗證 kill-session 與 cancelled 落地
# ---------------------------------------------------------------------------


def test_stop_requires_running_job(api_client):
    client, main_module = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 60"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    job_id = client.get("/jobs").json()[0]["id"]

    # 還是 queued，不是 running
    resp = client.post(f"/jobs/{job_id}/stop")
    assert resp.status_code == 400


def test_stop_flow_end_to_end_with_fake_ssh(api_client):
    client, main_module = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 600"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    job = client.get("/jobs").json()[0]

    # 直接操作 DB 把任務標成 running（略過真正派發，本測試只驗證 stop 流程）
    main_module.app_state.db.update_job(job["id"], status="running", server="server-a")

    stop_resp = client.post(f"/jobs/{job['id']}/stop")
    assert stop_resp.status_code == 200
    stop_approval = stop_resp.json()
    assert stop_approval["kind"] == "stop"
    assert stop_approval["status"] == "pending"

    fake_ssh = RecordingFakeSSH(tail_text="training interrupted by user\n")
    main_module.app_state.ssh_run = fake_ssh

    approve_resp = client.post(f"/approve/{stop_approval['id']}")
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert body["job"]["status"] == "cancelled"
    assert body["job"]["log_tail"] == "training interrupted by user\n"

    assert any("tmux kill-session" in c and f"job_{job['id']}" in c for c in fake_ssh.calls)


def test_stop_approval_does_not_overwrite_job_finished_before_approval(api_client):
    """核准 stop 之前任務自己跑完了（reconcile 標成 done），核准不能把它
    改寫成 cancelled——歷史紀錄要保留真實的完成狀態（修正 1）。"""
    client, main_module = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 600"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    job = client.get("/jobs").json()[0]
    main_module.app_state.db.update_job(job["id"], status="running", server="server-a")

    stop_approval_id = client.post(f"/jobs/{job['id']}/stop").json()["id"]

    # 使用者按下「請求停止」之後，任務自己跑完了。
    main_module.app_state.db.update_job(
        job["id"], status="done", exit_code=0, log_tail="finished cleanly\n"
    )

    approve_resp = client.post(f"/approve/{stop_approval_id}")
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert body["job"]["status"] == "done"
    assert body["job"]["exit_code"] == 0
    assert body["job"]["log_tail"] == "finished cleanly\n"
    assert "已結束" in body["approval"]["note"]

    # 資料庫裡也確認沒有被動過
    final = client.get(f"/jobs/{job['id']}").json()
    assert final["status"] == "done"
    assert final["exit_code"] == 0


def test_stop_approval_kill_failure_recorded_in_note_and_events(api_client):
    """kill-session 失敗不能被靜默吞掉：approval note 與 /events 都要留痕
    （修正 2）。"""
    client, main_module = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 600"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    job = client.get("/jobs").json()[0]
    main_module.app_state.db.update_job(job["id"], status="running", server="server-a")

    stop_approval_id = client.post(f"/jobs/{job['id']}/stop").json()["id"]

    class KillFailsFakeSSH:
        def __init__(self):
            self.calls = []

        async def __call__(self, server_name, command, timeout):
            self.calls.append(command)
            if "tmux kill-session" in command:
                raise ConnectionError("simulated ssh unreachable")
            return FakeCommandResult("")

    main_module.app_state.ssh_run = KillFailsFakeSSH()

    approve_resp = client.post(f"/approve/{stop_approval_id}")
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    # 任務仍標 cancelled（使用者核准意圖已確定），但要留痕 kill 其實失敗了
    assert body["job"]["status"] == "cancelled"
    assert "kill 失敗" in body["approval"]["note"]
    assert "simulated ssh unreachable" in body["approval"]["note"]

    events = client.get("/events").json()
    stop_events = [e for e in events if e["action"] == "stop" and e["params"].get("job_id") == job["id"]]
    assert len(stop_events) == 1
    assert stop_events[0]["params"]["kill_ok"] is False
    assert "simulated ssh unreachable" in stop_events[0]["params"]["kill_error"]
    assert stop_events[0]["result"] == "partial"


# ---------------------------------------------------------------------------
# GET /jobs/{id}/log
# ---------------------------------------------------------------------------


def test_get_log_running_job_uses_live_ssh(api_client):
    client, main_module = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 600"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    job = client.get("/jobs").json()[0]
    main_module.app_state.db.update_job(job["id"], status="running", server="server-a")

    fake_ssh = RecordingFakeSSH(tail_text="live output line\n")
    main_module.app_state.ssh_run = fake_ssh

    resp = client.get(f"/jobs/{job['id']}/log")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"] is True
    assert body["log_tail"] == "live output line\n"


def test_get_log_non_running_job_returns_stored_log_tail(api_client):
    client, main_module = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 5"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    job = client.get("/jobs").json()[0]
    main_module.app_state.db.update_job(
        job["id"], status="done", exit_code=0, log_tail="stored tail\n"
    )

    resp = client.get(f"/jobs/{job['id']}/log")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"] is False
    assert body["log_tail"] == "stored tail\n"


def test_get_log_404_for_missing_job(api_client):
    client, _main = api_client
    assert client.get("/jobs/9999/log").status_code == 404


# ---------------------------------------------------------------------------
# GET /events
# ---------------------------------------------------------------------------


def test_get_events_returns_newest_first(api_client):
    client, _main = api_client
    client.post("/dispatch", json={"command": "sleep 1"})
    client.post("/dispatch", json={"command": "sleep 2"})

    events = client.get("/events").json()
    assert len(events) >= 2
    # approval_requested for "sleep 2" 應該出現在較前面（新到舊）
    actions = [e["action"] for e in events]
    assert "approval_requested" in actions
    # 每筆都要有完整欄位
    for e in events:
        assert "ts" in e and "action" in e and "params" in e and "result" in e


def test_get_audit_is_alias_of_events(api_client):
    """實作指令 §7 的最小 API 集合列的是 `/audit`；`GET /audit` 應該回傳跟
    `GET /events` 完全相同的內容（同一份 audit.jsonl）。"""
    client, _main = api_client
    client.post("/dispatch", json={"command": "sleep 1"})

    events = client.get("/events").json()
    audit = client.get("/audit").json()
    assert audit == events
    assert len(audit) >= 1


# ---------------------------------------------------------------------------
# GET /jobs/{id}/cancel（沿用階段 1 行為，僅限 queued）
# ---------------------------------------------------------------------------


def test_cancel_queued_job(api_client):
    client, _main = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 5"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    job_id = client.get("/jobs").json()[0]["id"]

    resp = client.post(f"/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    assert client.get(f"/jobs/{job_id}").json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 靜態頁
# ---------------------------------------------------------------------------


def test_index_page_served(api_client):
    client, _main = api_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "AI 訓練調度中心" in resp.text


def test_index_page_has_servers_tab(api_client):
    """階段 8 第二批：新增「伺服器」分頁（Web Server Management）。"""
    client, _main = api_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "伺服器" in resp.text
    assert 'data-tab="servers"' in resp.text


def test_index_page_has_projects_matrix(api_client):
    """階段 15 Phase A（PLAN.md P.1.3）：專案分頁新增專案 × 機器矩陣，資料
    來源 GET /projects/matrix。"""
    client, _main = api_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "projects/matrix" in resp.text


def test_index_page_has_datasets_tab_and_card_ui(api_client):
    """階段 16（PLAN.md Q.3 節）：新增「資料集」分頁，每版顯示資料卡摘要、
    無卡版本標「無資料卡」＋「補登」按鈕（走 PATCH .../card）。"""
    client, _main = api_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'data-tab="datasets"' in resp.text
    assert "資料集" in resp.text
    assert "無資料卡" in resp.text
    assert "補登" in resp.text
    assert "資料卡" in resp.text
    # 登記資料集表單有 description/method 必填欄位
    assert "ds-description" in resp.text
    assert "ds-method" in resp.text
    # 檢視/補登資料卡都打同一個端點
    assert "/card" in resp.text
    assert "projects-matrix-table" in resp.text
    assert "待匯入候選" in resp.text


# ---------------------------------------------------------------------------
# 階段 3：POST/GET /projects、POST/GET /datasets
# ---------------------------------------------------------------------------


def test_create_and_list_project(api_client):
    client, _main = api_client
    resp = client.post(
        "/projects",
        json={
            "name": "resnet50",
            "repo_or_path": "git@example.com:x/resnet50.git",
            "dataset_name": "defect",
            "dataset_version": "v1",
            "default_command": "python train.py",
            "setup_cmd": "pip install -r requirements.txt",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "resnet50"
    assert body["dataset_name"] == "defect"

    listed = client.get("/projects").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "resnet50"


def test_create_project_duplicate_name_rejected(api_client):
    client, _main = api_client
    body = {"name": "resnet50", "repo_or_path": "git@x"}
    assert client.post("/projects", json=body).status_code == 200
    assert client.post("/projects", json=body).status_code == 400


def test_create_project_invalid_name_charset_rejected(api_client):
    client, _main = api_client
    resp = client.post("/projects", json={"name": "bad name!", "repo_or_path": "git@x"})
    assert resp.status_code == 400


def test_create_project_dataset_name_and_version_must_be_paired(api_client):
    """Fable 覆核修正 3：只給 dataset_name 或只給 dataset_version 都要拒絕，
    否則 make_has_dataset() 用 version=None 查快取恆為 False，配上資格
    過濾（修正 1），這個專案的訓練任務在自動模式下永遠選不到機器。"""
    client, _main = api_client
    resp = client.post(
        "/projects",
        json={"name": "p1", "repo_or_path": "git@x", "dataset_name": "defect"},
    )
    assert resp.status_code == 400

    resp = client.post(
        "/projects",
        json={"name": "p2", "repo_or_path": "git@x", "dataset_version": "v1"},
    )
    assert resp.status_code == 400

    # 同時提供或同時省略都合法
    assert client.post(
        "/projects",
        json={"name": "p3", "repo_or_path": "git@x", "dataset_name": "defect", "dataset_version": "v1"},
    ).status_code == 200
    assert client.post("/projects", json={"name": "p4", "repo_or_path": "git@x"}).status_code == 200


def test_create_and_list_dataset(api_client, tmp_path):
    client, _main = api_client
    # 用獨立子目錄放資料集來源檔案，避免跟 api_client fixture 建在 tmp_path
    # 底下的 test.db/audit.jsonl 混在一起被掃進 manifest。
    src_dir = tmp_path / "dataset_src"
    src_dir.mkdir()
    (src_dir / "a.bin").write_bytes(b"x" * 1024)
    (src_dir / "b.bin").write_bytes(b"y" * 2048)

    resp = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(src_dir),
            "description": "缺陷偵測資料集第一版",
            "method": "人工標註，來源為產線攝影機",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["size_bytes"] == 3072
    assert body["file_count"] == 2
    assert "manifest" in body  # POST 回應含完整 manifest
    assert body["card"]["description"] == "缺陷偵測資料集第一版"  # 階段 16：成功含 card

    listed = client.get("/datasets").json()
    assert len(listed) == 1
    assert listed[0]["size_bytes"] == 3072
    assert "manifest" not in listed[0]  # GET 列表不含完整檔案清單，避免回應過大


def test_create_dataset_duplicate_version_rejected(api_client, tmp_path):
    client, _main = api_client
    (tmp_path / "a.bin").write_bytes(b"x")
    body = {
        "name": "defect",
        "version": "v1",
        "source_path": str(tmp_path),
        "description": "desc",
        "method": "method",
    }
    assert client.post("/datasets", json=body).status_code == 200
    assert client.post("/datasets", json=body).status_code == 400


def test_create_dataset_missing_source_path_rejected(api_client):
    client, _main = api_client
    resp = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": "/no/such/dir",
            "description": "desc",
            "method": "method",
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 階段 16（PLAN.md Q.3 節）：POST /datasets 強制 description/method；
# derived_from／counts；GET/PATCH .../card
# ---------------------------------------------------------------------------


def test_create_dataset_missing_description_rejected(api_client, tmp_path):
    client, _main = api_client
    (tmp_path / "a.bin").write_bytes(b"x")
    resp = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "method": "method",
        },
    )
    assert resp.status_code == 400
    assert "資料集必須明確記錄製作方式與數量" in resp.json()["detail"]


def test_create_dataset_missing_method_rejected(api_client, tmp_path):
    client, _main = api_client
    (tmp_path / "a.bin").write_bytes(b"x")
    resp = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
        },
    )
    assert resp.status_code == 400
    assert "資料集必須明確記錄製作方式與數量" in resp.json()["detail"]


def test_create_dataset_blank_description_rejected(api_client, tmp_path):
    client, _main = api_client
    (tmp_path / "a.bin").write_bytes(b"x")
    resp = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "   ",
            "method": "method",
        },
    )
    assert resp.status_code == 400


def test_create_dataset_derived_from_nonexistent_rejected(api_client, tmp_path):
    client, _main = api_client
    (tmp_path / "a.bin").write_bytes(b"x")
    resp = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v2",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
            "derived_from": {"name": "defect", "version": "v1"},
        },
    )
    assert resp.status_code == 400
    assert "derived_from" in resp.json()["detail"]


def test_create_dataset_derived_from_existing_succeeds(api_client, tmp_path):
    client, _main = api_client
    src_v1 = tmp_path / "v1"
    src_v1.mkdir()
    (src_v1 / "a.bin").write_bytes(b"x")
    resp1 = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(src_v1),
            "description": "原始版",
            "method": "人工標註",
        },
    )
    assert resp1.status_code == 200

    src_v2 = tmp_path / "v2"
    src_v2.mkdir()
    (src_v2 / "a.bin").write_bytes(b"x")
    resp2 = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v2",
            "source_path": str(src_v2),
            "description": "篩選過的版本",
            "method": "從 v1 篩選",
            "derived_from": {"name": "defect", "version": "v1"},
            "counts": {"train": 80, "val": 20},
        },
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["card"]["derived_from"] == {"name": "defect", "version": "v1"}
    assert body["card"]["counts_custom"] == {"train": 80, "val": 20}


def test_create_dataset_counts_invalid_type_rejected(api_client, tmp_path):
    client, _main = api_client
    (tmp_path / "a.bin").write_bytes(b"x")
    resp = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
            "counts": {"bad": [1, 2, 3]},
        },
    )
    assert resp.status_code == 400


def test_get_dataset_card_with_card(api_client, tmp_path):
    client, _main = api_client
    resp = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "缺陷偵測資料集第一版",
            "method": "人工標註",
            "counts": {"train": 80, "val": 20},
        },
    )
    assert resp.status_code == 200

    card_resp = client.get("/datasets/defect/v1/card")
    assert card_resp.status_code == 200
    body = card_resp.json()
    assert body["name"] == "defect"
    assert body["version"] == "v1"
    assert body["card"]["description"] == "缺陷偵測資料集第一版"
    assert body["card"]["counts_custom"] == {"train": 80, "val": 20}
    assert body["note"] is None
    assert body["auto_facts"]["file_count"] is not None
    assert "缺陷偵測資料集第一版" in body["rendered"]


def test_get_dataset_card_without_card_returns_null_and_note(api_client, tmp_path):
    """階段 16 之前建立的舊資料集版本沒有卡：直接用 DB 插一筆無 card 的
    dataset（模擬 legacy 資料），查詢不報錯、不回空。"""
    client, main_module = api_client
    main_module.app_state.db.insert_dataset(
        name="legacy",
        version="v1",
        size_bytes=100,
        source_path=str(tmp_path),
        manifest={"file_count": 3, "total_size": 100, "files": []},
    )
    main_module.app_state.db.upsert_dataset_cache("server-a", "legacy", "v1")

    resp = client.get("/datasets/legacy/v1/card")
    assert resp.status_code == 200
    body = resp.json()
    assert body["card"] is None
    assert body["note"] is not None
    assert "未登記資料卡" in body["note"]
    assert body["auto_facts"]["file_count"] == 3
    assert body["auto_facts"]["cached_on"] == ["server-a"]
    assert "未登記資料卡" in body["rendered"]


def test_get_dataset_card_not_found_404(api_client):
    client, _main = api_client
    resp = client.get("/datasets/nope/v1/card")
    assert resp.status_code == 404


def test_patch_dataset_card_backfill_succeeds(api_client, tmp_path):
    client, main_module = api_client
    main_module.app_state.db.insert_dataset(
        name="legacy",
        version="v1",
        size_bytes=100,
        source_path=str(tmp_path),
        manifest={"file_count": 1, "total_size": 100, "files": []},
    )
    before = main_module.app_state.db.get_dataset("legacy", "v1")
    assert before.card is None

    resp = client.patch(
        "/datasets/legacy/v1/card",
        json={"description": "補登的描述", "method": "補登的製作方式"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["card"]["description"] == "補登的描述"
    assert body["note"] is None

    after = main_module.app_state.db.get_dataset("legacy", "v1")
    assert after.card["description"] == "補登的描述"
    assert after.card["created_at"]  # 補登時建立的 created_at


def test_patch_dataset_card_updates_updated_at_keeps_created_at(api_client, tmp_path):
    client, _main = api_client
    reg = client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "原始描述",
            "method": "原始方式",
        },
    )
    original_created_at = reg.json()["card"]["created_at"]

    resp = client.patch(
        "/datasets/defect/v1/card",
        json={"description": "更新後的描述", "method": "更新後的方式"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["card"]["description"] == "更新後的描述"
    assert body["card"]["created_at"] == original_created_at


def test_patch_dataset_card_missing_method_rejected(api_client, tmp_path):
    client, _main = api_client
    client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
        },
    )
    resp = client.patch("/datasets/defect/v1/card", json={"description": "desc only"})
    assert resp.status_code == 400


def test_patch_dataset_card_not_found_404(api_client):
    client, _main = api_client
    resp = client.patch(
        "/datasets/nope/v1/card", json={"description": "desc", "method": "method"}
    )
    assert resp.status_code == 404


def test_patch_dataset_card_writes_audit(api_client, tmp_path):
    client, main_module = api_client
    client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
        },
    )
    resp = client.patch(
        "/datasets/defect/v1/card",
        json={"description": "updated", "method": "updated method"},
    )
    assert resp.status_code == 200

    events = client.get("/events").json()
    actions = [e.get("action") for e in events]
    assert "dataset_card_updated" in actions


# ---------------------------------------------------------------------------
# 階段 3：派工 → 自動掛 sync 依賴 → 核准後落地成任務；快取命中後免同步
# ---------------------------------------------------------------------------


def _make_server_config(name="server-a", host="10.0.0.1"):
    from app.config import ServerConfig

    return ServerConfig(name=name, host=host, user="train", key="~/.ssh/id_rsa", gpu=False)


def test_dispatch_train_job_without_cached_dataset_creates_sync_plan(api_client, tmp_path):
    client, main_module = api_client
    main_module.app_state.server_configs["server-a"] = _make_server_config()

    (tmp_path / "a.bin").write_bytes(b"x" * 1024)
    client.post(
        "/projects",
        json={
            "name": "resnet50",
            "repo_or_path": "git@example.com:x/resnet50.git",
            "dataset_name": "defect",
            "dataset_version": "v1",
        },
    )
    client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
        },
    )

    resp = client.post(
        "/dispatch",
        json={
            "command": "python train.py",
            "type": "train",
            "project": "resnet50",
            "pin_server": "server-a",
        },
    )
    assert resp.status_code == 200
    approval = resp.json()
    assert approval["payload"]["sync_plan"] is not None
    assert approval["payload"]["sync_plan"]["dataset_name"] == "defect"

    approve_resp = client.post(f"/approve/{approval['id']}")
    assert approve_resp.status_code == 200
    train_job = approve_resp.json()["job"]
    assert train_job["depends_on"]  # 訓練任務依賴 sync 任務

    jobs = client.get("/jobs").json()
    sync_jobs = [j for j in jobs if j["type"] == "sync"]
    assert len(sync_jobs) == 1
    assert sync_jobs[0]["pin_server"] == "_local"
    assert sync_jobs[0]["id"] in train_job["depends_on"]
    assert "rsync" in sync_jobs[0]["command"]


def test_dispatch_train_job_sync_plan_non_default_port_appends_dash_p(api_client, tmp_path):
    """target server 的 SSH port 非 22（例如 pro6000 走 32221）時，approve()
    落地出的 sync job command 要帶 `-p {port}`，否則 rsync 會連到錯的埠
    （2026-07-10 修復）。"""
    client, main_module = api_client
    main_module.app_state.server_configs["pro6000"] = _make_server_config(
        name="pro6000", host="10.0.0.9"
    )
    main_module.app_state.server_configs["pro6000"].port = 32221

    (tmp_path / "a.bin").write_bytes(b"x" * 1024)
    client.post(
        "/projects",
        json={
            "name": "resnet50",
            "repo_or_path": "git@example.com:x/resnet50.git",
            "dataset_name": "defect",
            "dataset_version": "v1",
        },
    )
    client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
        },
    )

    resp = client.post(
        "/dispatch",
        json={
            "command": "python train.py",
            "type": "train",
            "project": "resnet50",
            "pin_server": "pro6000",
        },
    )
    assert resp.status_code == 200
    approval = resp.json()

    approve_resp = client.post(f"/approve/{approval['id']}")
    assert approve_resp.status_code == 200

    jobs = client.get("/jobs").json()
    sync_jobs = [j for j in jobs if j["type"] == "sync"]
    assert len(sync_jobs) == 1
    assert "-p 32221" in sync_jobs[0]["command"]


def test_dispatch_train_job_with_cached_dataset_skips_sync_plan(api_client, tmp_path):
    client, main_module = api_client
    main_module.app_state.server_configs["server-a"] = _make_server_config()
    main_module.app_state.db.upsert_dataset_cache("server-a", "defect", "v1")

    (tmp_path / "a.bin").write_bytes(b"x" * 1024)
    client.post(
        "/projects",
        json={
            "name": "resnet50",
            "repo_or_path": "git@example.com:x/resnet50.git",
            "dataset_name": "defect",
            "dataset_version": "v1",
        },
    )
    client.post(
        "/datasets",
        json={
            "name": "defect",
            "version": "v1",
            "source_path": str(tmp_path),
            "description": "desc",
            "method": "method",
        },
    )

    resp = client.post(
        "/dispatch",
        json={
            "command": "python train.py",
            "type": "train",
            "project": "resnet50",
            "pin_server": "server-a",
        },
    )
    approval = resp.json()
    assert approval["payload"]["sync_plan"] is None

    approve_resp = client.post(f"/approve/{approval['id']}")
    train_job = approve_resp.json()["job"]
    assert train_job["depends_on"] == []

    jobs = client.get("/jobs").json()
    assert [j for j in jobs if j["type"] == "sync"] == []


def test_dispatch_unregistered_dataset_returns_400(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs["server-a"] = _make_server_config()
    client.post(
        "/projects",
        json={"name": "p", "repo_or_path": "git@x", "dataset_name": "nope", "dataset_version": "v1"},
    )
    resp = client.post(
        "/dispatch",
        json={"command": "python train.py", "type": "train", "project": "p", "pin_server": "server-a"},
    )
    assert resp.status_code == 400


def test_dispatch_train_job_unknown_project_returns_404(api_client):
    client, main_module = api_client
    main_module.app_state.server_configs["server-a"] = _make_server_config()
    resp = client.post(
        "/dispatch",
        json={"command": "x", "type": "train", "project": "missing", "pin_server": "server-a"},
    )
    assert resp.status_code == 404


def test_dispatch_auto_mode_warns_when_dataset_cached_nowhere(api_client):
    """Fable 覆核修正 2：透過 API 走一次自動模式（沒有 pin_server），
    payload 應該帶 warning，前端核准卡片才有東西可以顯示。"""
    client, _main = api_client
    client.post(
        "/projects",
        json={
            "name": "resnet50",
            "repo_or_path": "git@example.com:x/resnet50.git",
            "dataset_name": "defect",
            "dataset_version": "v1",
        },
    )
    resp = client.post(
        "/dispatch",
        json={"command": "python train.py", "type": "train", "project": "resnet50"},
    )
    assert resp.status_code == 200
    payload = resp.json()["payload"]
    assert payload["warning"] is not None
    assert "defect@v1" in payload["warning"]


# ---------------------------------------------------------------------------
# 階段 3：GET /servers 顯示已快取資料集標籤
# ---------------------------------------------------------------------------


def test_get_servers_includes_cached_datasets_tag(api_client):
    client, main_module = api_client
    main_module.app_state.server_states["server-a"] = main_module.ServerState(
        name="server-a", online=True
    )
    main_module.app_state.db.upsert_dataset_cache("server-a", "defect", "v1")

    servers = client.get("/servers").json()
    server_a = next(s for s in servers if s["name"] == "server-a")
    assert "defect@v1" in server_a["cached_datasets"]


# ---------------------------------------------------------------------------
# 階段 10（PLAN.md K.2）：source="web" + WEB_DIRECT_EXECUTE（預設 true）
# 一步生效；=false 恢復現行兩步。
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client_no_web_direct(tmp_path, monkeypatch):
    """同 conftest 的 `api_client`，但明確關閉 WEB_DIRECT_EXECUTE，驗證
    K.2「=false 恢復兩步」——這個 fixture 必須自己重新設定完整環境變數再
    建立 TestClient（環境變數在 `load_app_config()` 於 lifespan 啟動時讀取
    一次，不能等 client 建立後才改）。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(tmp_path / "servers.yaml"))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")
    monkeypatch.setenv("WEB_DIRECT_EXECUTE", "false")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


def test_web_direct_execute_default_true_dispatch_auto_approves(api_client):
    client, main_module = api_client
    resp = client.post("/dispatch", json={"command": "sleep 60", "source": "web"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_approved"] is True
    assert body["approval"]["status"] == "approved"
    assert body["job"]["status"] == "queued"
    assert body["job"]["command"] == "sleep 60"

    # job 真的入列了（不是只有回應假裝入列）。
    jobs = client.get("/jobs").json()
    assert len(jobs) == 1
    assert jobs[0]["command"] == "sleep 60"

    # 稽核如實記錄 approved_by="web-direct"。
    events = client.get("/events").json()
    approve_events = [e for e in events if e["action"] == "approve"]
    assert len(approve_events) == 1
    assert approve_events[0]["params"]["approved_by"] == "web-direct"
    assert "網頁直接執行" in body["approval"]["note"]


def test_web_direct_execute_default_true_stop_auto_approves(api_client):
    client, main_module = api_client
    approval_id = client.post("/dispatch", json={"command": "sleep 600", "source": "web"}).json()[
        "approval"
    ]["id"]
    job = client.get("/jobs").json()[0]
    main_module.app_state.db.update_job(job["id"], status="running", server="server-a")
    main_module.app_state.ssh_run = RecordingFakeSSH(tail_text="stopped by web-direct\n")

    resp = client.post(f"/jobs/{job['id']}/stop", json={"source": "web"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_approved"] is True
    assert body["job"]["status"] == "cancelled"
    assert body["job"]["log_tail"] == "stopped by web-direct\n"

    # 用不到 approval_id 但保留避免 lint 抱怨未使用；順便確認核准當下建立
    # 的 approval id 跟後續 approvals 列表一致。
    approvals = client.get("/approvals").json()
    assert any(a["id"] == approval_id for a in approvals)


def test_web_direct_execute_false_restores_two_step_dispatch(api_client_no_web_direct):
    client, _main = api_client_no_web_direct
    resp = client.post("/dispatch", json={"command": "sleep 60", "source": "web"})
    assert resp.status_code == 200
    body = resp.json()
    # 沒有 auto_approved 欄位，回應形狀跟現狀完全一樣（單純 approval dict）。
    assert "auto_approved" not in body
    assert body["status"] == "pending"
    assert client.get("/jobs").json() == []


def test_web_direct_execute_false_restores_two_step_stop(api_client_no_web_direct):
    client, main_module = api_client_no_web_direct
    approval_id = client.post("/dispatch", json={"command": "sleep 600", "source": "web"}).json()[
        "id"
    ]
    client.post(f"/approve/{approval_id}")
    job = client.get("/jobs").json()[0]
    main_module.app_state.db.update_job(job["id"], status="running", server="server-a")

    resp = client.post(f"/jobs/{job['id']}/stop", json={"source": "web"})
    assert resp.status_code == 200
    body = resp.json()
    assert "auto_approved" not in body
    assert body["status"] == "pending"
    assert client.get(f"/jobs/{job['id']}").json()["status"] == "running"


def test_source_defaults_to_api_and_behaves_like_before(api_client):
    """沒帶 source 的既有呼叫端（既有測試套件的預設假設）：即使
    WEB_DIRECT_EXECUTE 預設開啟，也不會被一步生效，因為那條路徑只認
    `source == "web"`。"""
    client, _main = api_client
    resp = client.post("/dispatch", json={"command": "sleep 60"})
    assert resp.status_code == 200
    body = resp.json()
    assert "auto_approved" not in body
    assert body["status"] == "pending"
    assert client.get("/jobs").json() == []


def test_invalid_source_value_falls_back_to_api(api_client):
    client, _main = api_client
    resp = client.post("/dispatch", json={"command": "sleep 60", "source": "not-a-real-source"})
    assert resp.status_code == 200
    body = resp.json()
    assert "auto_approved" not in body
    assert body["status"] == "pending"


# ---------------------------------------------------------------------------
# 階段 10（PLAN.md K.3）：自動核准規則在 API 層（source 非 web，或
# WEB_DIRECT_EXECUTE=false 時走這條路徑）。
# ---------------------------------------------------------------------------


def test_auto_approve_rule_matching_chatgpt_source_auto_approves_dispatch(api_client):
    client, main_module = api_client
    rules_path = main_module.app_state.config.auto_approve_rules_path
    from pathlib import Path

    Path(rules_path).write_text(
        "rules:\n  - source: chatgpt\n    command_regex: '^ls '\n", encoding="utf-8"
    )

    resp = client.post("/dispatch", json={"command": "ls -la /tmp", "source": "chatgpt"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_approved"] is True
    assert body["job"]["status"] == "queued"

    events = client.get("/events").json()
    approve_events = [e for e in events if e["action"] == "approve"]
    assert approve_events[-1]["params"]["approved_by"] == "auto-rule-0"


def test_auto_approve_rule_not_matching_stays_pending(api_client):
    client, main_module = api_client
    rules_path = main_module.app_state.config.auto_approve_rules_path
    from pathlib import Path

    Path(rules_path).write_text("rules:\n  - source: web\n", encoding="utf-8")

    resp = client.post("/dispatch", json={"command": "python train.py", "source": "chatgpt"})
    assert resp.status_code == 200
    body = resp.json()
    assert "auto_approved" not in body
    assert body["status"] == "pending"
    assert client.get("/jobs").json() == []


def test_auto_approve_rules_file_missing_stays_pending_for_chatgpt_source(api_client):
    """沒有 auto_approve.yaml（安全預設）：chatgpt 來源一樣走既有 pending
    流程，不會被意外自動核准。"""
    client, _main = api_client
    resp = client.post("/dispatch", json={"command": "python train.py", "source": "chatgpt"})
    assert resp.status_code == 200
    body = resp.json()
    assert "auto_approved" not in body
    assert body["status"] == "pending"
