"""階段 15 Phase B（PLAN.md P.2.4 節）：`DELETE /projects/{name}`——直接
執行＋稽核 `project_deleted`。只刪 DB 列（projects＋該專案的
project_instances），絕不動任何機器上的檔案；queued/running job 引用時
409；imported 候選狀態不回溯。
"""

from __future__ import annotations

from app.audit import read_audit
from app.db import Database


def test_delete_project_removes_project_and_instances(db: Database, audit_path):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    db.insert_project_instance(project_name="proj1", server="server-b", path="/data/proj1")

    db.delete_project("proj1")

    assert db.get_project("proj1") is None
    assert db.list_project_instances("proj1") == []


def test_delete_project_does_not_touch_other_projects(db: Database):
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    db.insert_project("proj2", "/local/proj2")
    db.insert_project_instance(project_name="proj2", server="server-a", path="/data/proj2")

    db.delete_project("proj1")

    assert db.get_project("proj2") is not None
    assert len(db.list_project_instances("proj2")) == 1


# ---------------------------------------------------------------------------
# main.py：DELETE /projects/{name}
# ---------------------------------------------------------------------------


class RecordingSSH:
    """只記錄是否曾經被呼叫過——DELETE 端點絕不該對任何機器發起 SSH。刻意
    不在 `__call__` 裡拋例外（拋例外會被端點的例外處理吞掉、變成不好懂的
    500），呼叫端測試直接斷言 `calls == []` 即可。"""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, server, command, timeout):
        self.calls.append(command)
        return None


def test_delete_project_endpoint_success_no_ssh_calls(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    ssh = RecordingSSH()
    main_module.app_state.ssh_run = ssh

    resp = client.delete("/projects/proj1")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "name": "proj1"}

    assert db.get_project("proj1") is None
    assert db.list_project_instances("proj1") == []
    # 檔案不動：無任何 SSH 呼叫。
    assert ssh.calls == []

    records = read_audit(main_module.app_state.config.audit_path)
    deleted_events = [r for r in records if r["action"] == "project_deleted"]
    assert len(deleted_events) == 1
    assert deleted_events[0]["params"]["name"] == "proj1"
    assert deleted_events[0]["params"]["instance_count"] == 1


def test_delete_project_endpoint_not_found_404(api_client):
    client, _main_module = api_client
    resp = client.delete("/projects/nope")
    assert resp.status_code == 404


def test_delete_project_endpoint_running_job_blocks_409(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    job_id = db.insert_job(command="python train.py", type="train", project="proj1")
    db.update_job(job_id, status="running", server="server-a")

    resp = client.delete("/projects/proj1")
    assert resp.status_code == 409
    assert db.get_project("proj1") is not None


def test_delete_project_endpoint_queued_job_blocks_409(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    db.insert_job(command="python train.py", type="train", project="proj1", status="queued")

    resp = client.delete("/projects/proj1")
    assert resp.status_code == 409
    assert db.get_project("proj1") is not None


def test_delete_project_endpoint_done_job_does_not_block(api_client):
    """已完成/失敗/取消的 job 不擋刪除——只有 queued/running 才擋。"""
    client, main_module = api_client
    db = main_module.app_state.db
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")
    job_id = db.insert_job(command="python train.py", type="train", project="proj1")
    db.update_job(job_id, status="done", server="server-a")

    resp = client.delete("/projects/proj1")
    assert resp.status_code == 200
    assert db.get_project("proj1") is None


def test_delete_project_endpoint_does_not_rollback_imported_candidate(api_client):
    """imported 候選狀態不回溯：刪掉專案後，原本 imported 的候選仍然是
    imported（不會變回 pending），避免使用者重複匯入同一個候選。"""
    client, main_module = api_client
    db = main_module.app_state.db
    cand_id = db.upsert_project_candidate(server="server-a", path="/data/proj1", name_guess="proj1")
    db.update_project_candidate_status(cand_id, "imported")
    db.insert_project("proj1", "https://github.com/x/proj1.git")
    db.insert_project_instance(project_name="proj1", server="server-a", path="/data/proj1")

    resp = client.delete("/projects/proj1")
    assert resp.status_code == 200

    candidate = db.get_project_candidate(cand_id)
    assert candidate.status == "imported"
