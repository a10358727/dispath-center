"""階段 4：AppState 的背景 hook 追蹤機制（PLAN.md E）。

直接建構 `AppState`（不透過 `TestClient`／lifespan，也不呼叫
`start_background_tasks()`）：這樣整個測試都在同一個 `asyncio.run()` 建立
的 event loop 裡執行，避免跟 `TestClient` 內部另開執行緒/loop 的背景任務
互相干擾。只把 `app.main.local_run` 換成假件（不碰真的 SSH/rsync），SMTP
設定本來就沒填，`send_mail()` 會照它自己的契約直接跳過（回傳 False），
不需要另外假造。
"""

import asyncio
import hashlib
import logging
import uuid

from app.config import AppConfig, ServerConfig
from app.db import Job
from app.main import AppState
from app.sshpool import CommandResult


def make_app_state(tmp_path) -> AppState:
    config = AppConfig(
        servers=[],
        db_path=str(tmp_path / "test.db"),
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    return AppState(config)


async def _wait_until_idle(app_state: AppState, timeout: float = 2.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while app_state._background_tasks and loop.time() < deadline:
        await asyncio.sleep(0.01)


def test_schedule_job_finished_hook_pulls_results_and_writes_audit(tmp_path, monkeypatch):
    import app.main as main_module

    app_state = make_app_state(tmp_path)
    app_state.server_configs["server-a"] = ServerConfig(
        name="server-a", host="10.0.0.5", user="train", key="~/.ssh/id_rsa"
    )

    calls = []

    async def fake_local_run(command, timeout):
        calls.append(command)
        return CommandResult(exit_status=0, stdout="", stderr="")

    monkeypatch.setattr(main_module, "local_run", fake_local_run)

    job = Job(
        id=999,
        type="train",
        project="demo",
        command="python train.py",
        require_tag=None,
        pin_server=None,
        status="done",
        server="server-a",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:05:00+00:00",
        exit_code=0,
        log_tail="training finished\n",
    )

    async def run_and_wait():
        app_state.schedule_job_finished_hook(job)
        # 呼叫的當下不 await 拉結果／寄信，排程輪應該立刻可以繼續往下走。
        assert len(app_state._background_tasks) == 1
        await _wait_until_idle(app_state)

    try:
        asyncio.run(run_and_wait())

        assert len(calls) == 1  # 真的呼叫了（假的）rsync 拉結果指令
        assert app_state._background_tasks == set()  # 跑完後自動從追蹤集合移除

        from app.audit import read_audit

        records = read_audit(app_state.config.audit_path)
        actions = [e["action"] for e in records]
        assert "result_pulled" in actions
        assert "job_notified" in actions
        assert all(
            record["actor"]
            == {"id": "system", "kind": "system", "authentication": "system"}
            for record in records
        )
    finally:
        app_state.db.close()


def test_schedule_job_finished_hook_passes_db_and_backfills_coding_run(tmp_path, monkeypatch):
    """階段 13（PLAN.md N.6，批次 3a 補線）：`schedule_job_finished_hook()`
    要傳 `db=self.db` 給 `handle_job_finished()`，coding job 結束後才會真的
    回填 `coding_runs`（批次 2 已經支援 `db=` 參數，只是 main.py 這裡一直
    沒接線——這個測試釘住接線本身，不是 `_backfill_coding_run()` 的邏輯，
    那部分已經在 tests/test_coding_task.py 覆蓋）。"""
    import json

    import app.main as main_module

    app_state = make_app_state(tmp_path)
    app_state.server_configs["server-a"] = ServerConfig(
        name="server-a", host="10.0.0.5", user="train", key="~/.ssh/id_rsa"
    )

    async def fake_local_run(command, timeout):
        return CommandResult(exit_status=0, stdout="", stderr="")

    monkeypatch.setattr(main_module, "local_run", fake_local_run)

    job = Job(
        id=888,
        type="coding",
        project="proj1",
        command="bash cmd.sh",
        require_tag=None,
        pin_server="server-a",
        status="done",
        server="server-a",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:05:00+00:00",
        exit_code=0,
        log_tail="[coding_task] 完成\n",
    )
    run_id = app_state.db.insert_coding_run(
        approval_id=1,
        project="proj1",
        runner_server="server-a",
        instruction="fix the bug",
        job_id=job.id,
    )
    result_dir = tmp_path / "results" / str(job.id)
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "status": "done",
                "base_commit": "aaa111",
                "result_branch": "ai-task-1",
                "result_commit": "bbb222",
                "no_changes": False,
                "test_command": None,
                "test_exit_code": None,
                "codex_exit": 0,
                "diff_summary": "1 file changed",
                "codex_version": "codex-cli 0.144.1",
                "error_message": None,
            }
        ),
        encoding="utf-8",
    )

    async def run_and_wait():
        app_state.schedule_job_finished_hook(job)
        await _wait_until_idle(app_state)

    try:
        asyncio.run(run_and_wait())
        coding_run = app_state.db.get_coding_run(run_id)
        assert coding_run.status == "done"
        assert coding_run.result_commit == "bbb222"
    finally:
        app_state.db.close()


def test_engineering_result_collection_requires_exact_approved_runner_identity(
    tmp_path, monkeypatch
):
    import app.main as main_module

    approved = ServerConfig(
        name="server-a",
        host="192.0.2.10",
        user="runner",
        key="/tmp/synthetic-key",
        port=2222,
    )
    config = AppConfig(
        servers=[approved],
        db_path=str(tmp_path / "test.db"),
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    app_state = AppState(config)
    project = "runner-contract-project"
    project_id = app_state.db.insert_project(
        project, "https://example.invalid/runner-contract.git"
    )
    version = app_state.db.get_or_create_project_version(project, "a" * 40)
    task_id = str(uuid.uuid4())
    task_id, approval_id = app_state.db.insert_engineering_task_request(
        task_id=task_id,
        project_id=project_id,
        project_name=project,
        project_version_id=version.id,
        base_commit="a" * 40,
        agent_provider_id="codex",
        provider_capabilities={"adapter": "codex-exec-v1"},
        execution_contract={
            "runner": {
                "name": approved.name,
                "host": approved.host,
                "user": approved.user,
                "port": approved.port,
            }
        },
        contract_version="engineering-task-v1",
        structured_request={"objective": "verify runner identity"},
        instruction="verify runner identity",
        detected_metadata={},
        runner_server=approved.name,
        validation_target=None,
        approval_payload={"engineering_task_id": task_id},
    )
    app_state.db.update_approval(approval_id, status="approved")
    job_id = app_state.db.insert_job(
        command="internal /home/private/runner command",
        type="coding",
        project=project,
        status="done",
        pin_server=approved.name,
        engineering_task_id=task_id,
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )
    app_state.db.update_job(job_id, server=approved.name)
    app_state.db.register_engineering_task_command(
        task_id=task_id,
        attempt_number=1,
        sequence=1,
        command_key=f"synthetic-coding-job-{job_id}",
        job_id=job_id,
        command_role="agent_turn",
        display_command="Run approved synthetic agent turn",
        command_digest=hashlib.sha256(
            "internal /home/private/runner command".encode("utf-8")
        ).hexdigest(),
        execution_location="coding_runner",
        working_directory_label="Approved isolated worktree",
        policy_family="approved_agent_execution",
        policy_disposition="task_approved",
        approval_id=approval_id,
        status_source="job",
        recorded_status="done",
    )
    job = app_state.db.get_job(job_id)
    assert app_state._result_collection_server_config(job) is approved

    # Repointing the same logical server name must not authorize result pulls
    # from the replacement host in either completion or restart recovery.
    app_state.server_configs[approved.name] = ServerConfig(
        name=approved.name,
        host="192.0.2.99",
        user=approved.user,
        key=approved.key,
        port=approved.port,
    )
    assert app_state._result_collection_server_config(job) is None

    completion_configs = []
    recovery_configs = []

    async def fake_handle(_job, **kwargs):
        completion_configs.append(kwargs["server_cfg"])

    async def fake_recover(_job, **kwargs):
        recovery_configs.append(kwargs["server_cfg"])
        return False

    monkeypatch.setattr(main_module, "handle_job_finished", fake_handle)
    monkeypatch.setattr(main_module, "recover_engineering_task_result", fake_recover)

    async def exercise_both_paths():
        app_state.schedule_job_finished_hook(job)
        await _wait_until_idle(app_state)
        await app_state._recover_engineering_task_results_once()

    try:
        asyncio.run(exercise_both_paths())
        assert completion_configs == [None]
        assert recovery_configs == [None]
    finally:
        app_state.db.close()


def test_engineering_recovery_sweep_isolates_each_job_failure(
    tmp_path, monkeypatch, caplog
):
    """One corrupt recovery candidate must not starve later terminal Jobs.

    The sweep is observational: even when the injected collector raises, it
    must leave both canonical Job states unchanged and log only a fixed safe
    diagnostic, never the exception's credential-bearing message.
    """

    import app.main as main_module

    app_state = make_app_state(tmp_path)
    project = "recovery-isolation-project"
    project_id = app_state.db.insert_project(
        project, "https://example.invalid/recovery-isolation.git"
    )
    version = app_state.db.get_or_create_project_version(project, "a" * 40)

    def insert_task(objective):
        task_id = str(uuid.uuid4())
        task_id, approval_id = app_state.db.insert_engineering_task_request(
            task_id=task_id,
            project_id=project_id,
            project_name=project,
            project_version_id=version.id,
            base_commit="a" * 40,
            agent_provider_id="codex",
            provider_capabilities={"adapter": "codex-exec-v1"},
            execution_contract={"runner": {"name": "server-a"}},
            contract_version="engineering-task-v1",
            structured_request={"objective": objective},
            instruction=objective,
            detected_metadata={},
            runner_server="server-a",
            validation_target=None,
            approval_payload={"engineering_task_id": task_id},
        )
        app_state.db.update_approval(approval_id, status="approved")
        app_state.db.update_engineering_task(task_id, status="finalizing")
        return task_id

    first_task_id = insert_task("first recovery candidate")
    second_task_id = insert_task("second recovery candidate")
    first_job_id = app_state.db.insert_job(
        command="internal recovery candidate one",
        type="coding",
        project="first-project",
        status="done",
        engineering_task_id=first_task_id,
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )
    second_job_id = app_state.db.insert_job(
        command="internal recovery candidate two",
        type="coding",
        project="second-project",
        status="failed",
        engineering_task_id=second_task_id,
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )
    original_statuses = {
        first_job_id: app_state.db.get_job(first_job_id).status,
        second_job_id: app_state.db.get_job(second_job_id).status,
    }
    original_task_statuses = {
        first_task_id: app_state.db.get_engineering_task(first_task_id).status,
        second_task_id: app_state.db.get_engineering_task(second_task_id).status,
    }
    secret_sentinel = "Bearer synthetic-recovery-secret /home/private/result"
    recovered_job_ids = []

    async def fake_recover(job, **_kwargs):
        recovered_job_ids.append(job.id)
        if job.id == first_job_id:
            raise RuntimeError(secret_sentinel)
        return True

    monkeypatch.setattr(main_module, "recover_engineering_task_result", fake_recover)
    caplog.set_level(logging.ERROR, logger="app.main")

    try:
        asyncio.run(app_state._recover_engineering_task_results_once())

        assert recovered_job_ids == [first_job_id, second_job_id]
        assert {
            first_job_id: app_state.db.get_job(first_job_id).status,
            second_job_id: app_state.db.get_job(second_job_id).status,
        } == original_statuses
        assert {
            first_task_id: app_state.db.get_engineering_task(first_task_id).status,
            second_task_id: app_state.db.get_engineering_task(second_task_id).status,
        } == original_task_statuses
        assert f"job #{first_job_id}" in caplog.text
        assert "稍後重試" in caplog.text
        assert secret_sentinel not in caplog.text
    finally:
        app_state.db.close()


def test_engineering_stall_notification_uses_semantic_command_only(
    tmp_path, monkeypatch
):
    import app.main as main_module

    app_state = make_app_state(tmp_path)
    sent = []

    async def fake_send_mail(_config, subject, body):
        sent.append((subject, body))
        return True

    monkeypatch.setattr(main_module, "send_mail", fake_send_mail)
    job = Job(
        id=777,
        type="coding",
        project="safe-project",
        command="cd /home/private/worktree && AUTHORIZATION='Bearer synthetic' codex exec",
        require_tag=None,
        pin_server="server-a",
        status="running",
        server="server-a",
        log_tail="/home/private/worktree/task.log",
        engineering_task_id=str(uuid.uuid4()),
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )

    try:
        asyncio.run(app_state._send_stall_mail(job))
        assert len(sent) == 1
        _subject, body = sent[0]
        assert "Run Codex agent in an isolated worktree" in body
        assert "/home/private" not in body
        assert "AUTHORIZATION" not in body
    finally:
        app_state.db.close()


def test_stop_background_tasks_cancels_pending_hook_tasks(tmp_path):
    """服務關閉時，還沒跑完的背景 hook task（例如 rsync 拉很久）要被
    cancel，不留孤兒 task；`stop_background_tasks()` 本身不能因此掛住。"""
    app_state = make_app_state(tmp_path)
    started = asyncio.Event()

    async def never_finishes():
        started.set()
        await asyncio.sleep(3600)

    async def run_and_stop():
        app_state._spawn_tracked_task(never_finishes())
        await started.wait()
        assert len(app_state._background_tasks) == 1
        await app_state.stop_background_tasks()
        assert app_state._background_tasks == set()

    asyncio.run(asyncio.wait_for(run_and_stop(), timeout=5))


# ---------------------------------------------------------------------------
# 階段 7：AppState._summarize_mail() 後備順序 —— anthropic 優先，沒有的話
# （is_llm_available() 為 False）才試本地 vLLM，兩者都沒有 -> None
# （PLAN.md H 節）。直接呼叫 AppState._summarize_mail()，不用起 HTTP 服務。
# ---------------------------------------------------------------------------


def test_summarize_mail_prefers_anthropic_falls_back_to_vllm_then_none(tmp_path, monkeypatch):
    import app.llm as llm_module
    import app.main as main_module

    app_state = make_app_state(tmp_path)
    try:
        calls = {"anthropic": 0, "vllm": 0}

        async def fake_anthropic_summarize(body, config, client=None):
            calls["anthropic"] += 1
            return "anthropic 摘要"

        async def fake_vllm_summarize(body, config, client=None):
            calls["vllm"] += 1
            # 忠實反映 app.llm_local.summarize_mail_body_local() 的真實契約：
            # 沒設定就回 None（這裡刻意不無條件回傳字串，才能驗證「兩者都
            # 沒設定 -> None」這條路徑，而不是被假件蓋掉真實行為）。
            from app.llm_local import is_vllm_available

            if not is_vllm_available(config):
                return None
            return "vllm 摘要"

        monkeypatch.setattr(main_module, "summarize_mail_body", fake_anthropic_summarize)
        monkeypatch.setattr(main_module, "summarize_mail_body_local", fake_vllm_summarize)

        # 兩者都沒設定 -> anthropic 不呼叫、vllm 呼叫了但回 None
        result = asyncio.run(app_state._summarize_mail("內容", app_state.config))
        assert result is None
        assert calls == {"anthropic": 0, "vllm": 1}

        # anthropic 可用 -> 優先用 anthropic，不呼叫本地 vLLM
        monkeypatch.setattr(llm_module, "anthropic", object())
        app_state.config.anthropic_api_key = "sk-test-key"
        result = asyncio.run(app_state._summarize_mail("內容", app_state.config))
        assert result == "anthropic 摘要"
        assert calls == {"anthropic": 1, "vllm": 1}  # vllm 計數維持前一步的值，沒有再被呼叫

        # anthropic 不可用、本地 vLLM 可用 -> 改走本地 vLLM
        app_state.config.anthropic_api_key = None
        app_state.config.vllm_base_url = "http://127.0.0.1:8001/v1"
        app_state.config.vllm_model = "qwen3-coder-30b"
        result = asyncio.run(app_state._summarize_mail("內容", app_state.config))
        assert result == "vllm 摘要"
        assert calls == {"anthropic": 1, "vllm": 2}
    finally:
        app_state.db.close()
