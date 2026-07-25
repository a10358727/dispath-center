"""Goal 3 D-3：`engineering_command` approval kind。

2026-07-16 D3 裁定把這個 kind 留到「D1 adapter 存在之後才定義」，因為它的
payload 必須繫結 app-server 的 command-approval callback。adapter 已經存在
（`app/codex_app_server.py` + `CodingAgentCommandApprovalHandle`），所以
payload 形狀是**被決定的**，不是臆測的——本檔用真實的 handle 型別產生
payload，證明兩邊一致。

範圍：D1 裁定只核准在 `CONTROLLED_CODING_RUNNER_V1=false` 下實作＋假協議
測試；**正式啟用仍需另一次獨立簽核**。因此所有測試都自己開旗標，不碰真實
provider、不起 session、不連線（INV-TEST-2）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.approvals import (
    ControlledCodingRunnerDisabledError,
    InvalidEngineeringCommandRequestError,
    approve,
    maybe_auto_approve,
    request_engineering_command_approval,
)
from app.coding_agents import CodingAgentCommandApprovalHandle
from app.config import AppConfig
from app.db import VALID_APPROVAL_KINDS, Database


DIGEST = "a" * 64
WORKDIR = "/home/runner/codex_workspaces/task-1/worktree"


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def _config(enabled=True) -> AppConfig:
    return AppConfig(
        servers=[], controlled_coding_runner_v1=enabled, audit_path="/dev/null"
    )


class _State:
    def __init__(self, config):
        self.config = config


TASK_ID = "11111111-1111-4111-8111-111111111111"


def _task(db, task_id=TASK_ID):
    """建立一個最小的 engineering task 供 payload 綁定（用既有的
    `insert_engineering_task_request()`，不另造第二條建立路徑）。"""
    created_id, approval_id = db.insert_engineering_task_request(
        project_id="proj-1",
        project_name="p",
        project_version_id="ver-1",
        base_commit="c" * 40,
        agent_provider_id="codex-app-server-v1",
        provider_capabilities={},
        execution_contract={},
        contract_version="v1",
        structured_request={},
        instruction="do the thing",
        detected_metadata={},
        runner_server="server-a",
        validation_target=None,
        approval_payload={"project": "p", "engineering_task_id": task_id},
        task_id=task_id,
    )
    return created_id, approval_id


def _handle(task_id, parent_approval_id, **kw):
    defaults = dict(
        request_id=1,
        engineering_task_id=task_id,
        attempt_number=1,
        parent_approval_id=parent_approval_id,
        thread_id="th-1",
        turn_id="tu-1",
        item_id="it-1",
        provider_approval_id=None,
        command_digest=DIGEST,
        working_directory=WORKDIR,
    )
    defaults.update(kw)
    return CodingAgentCommandApprovalHandle(**defaults)


# ---------------------------------------------------------------------------
# 1. kind 註冊與旗標邊界
# ---------------------------------------------------------------------------


def test_kind_is_registered():
    assert "engineering_command" in VALID_APPROVAL_KINDS


def test_request_is_fail_closed_when_runner_flag_disabled(db):
    """D1 裁定：預設關閉，啟用需另一次獨立簽核。"""
    task_id, parent = _task(db)
    with pytest.raises(ControlledCodingRunnerDisabledError):
        request_engineering_command_approval(
            db, _handle(task_id, parent), config=_config(enabled=False)
        )
    assert db.list_approvals(kind="engineering_command") == []


def test_approve_is_fail_closed_when_runner_flag_disabled(db):
    task_id, parent = _task(db)
    approval = request_engineering_command_approval(
        db, _handle(task_id, parent), config=_config()
    )
    with pytest.raises(ControlledCodingRunnerDisabledError):
        asyncio.run(
            approve(
                db,
                approval.id,
                app_state=_State(_config(enabled=False)),
                audit_path="/dev/null",
            )
        )
    assert db.get_approval(approval.id).status == "pending"


# ---------------------------------------------------------------------------
# 2. payload 綁定真實 handle 的不可變欄位
# ---------------------------------------------------------------------------


def test_payload_binds_every_immutable_handle_field(db):
    """payload 形狀由 adapter 的 handle 決定，不是臆測。"""
    task_id, parent = _task(db)
    approval = request_engineering_command_approval(
        db, _handle(task_id, parent), config=_config()
    )
    assert approval.payload == {
        "engineering_task_id": task_id,
        "attempt_number": 1,
        "parent_approval_id": parent,
        "thread_id": "th-1",
        "turn_id": "tu-1",
        "item_id": "it-1",
        "command_digest": DIGEST,
        "working_directory": WORKDIR,
    }


def test_payload_never_contains_raw_command_text(db):
    """只綁 digest——指令原文不進 approval payload。"""
    task_id, parent = _task(db)
    approval = request_engineering_command_approval(
        db, _handle(task_id, parent), config=_config()
    )
    body = str(approval.payload)
    assert "command" not in {k for k in approval.payload if k != "command_digest"}
    assert DIGEST in body


@pytest.mark.parametrize("bad_digest", ["", "abc", "z" * 64, "a" * 63])
def test_malformed_digest_cannot_even_form_a_handle(db, bad_digest):
    """digest 的把關在 handle 型別本身（`__post_init__`）——壞值連 handle
    都構造不出來，根本走不到 approval 層。這是比在我這層檢查更強的位置。"""
    task_id, parent = _task(db)
    with pytest.raises(ValueError, match="command digest"):
        _handle(task_id, parent, command_digest=bad_digest)


@pytest.mark.parametrize("bad_dir", ["relative/path", "/a/../b", ""])
def test_unsafe_working_directory_cannot_even_form_a_handle(db, bad_dir):
    """working_directory 同理：絕對路徑與無 `..` 由 handle 型別強制。"""
    task_id, parent = _task(db)
    with pytest.raises(ValueError):
        _handle(task_id, parent, working_directory=bad_dir)


def test_request_still_defends_against_a_non_handle_object(db):
    """呼叫端若傳了形狀不對的東西（不是真的 handle），仍然 fail-closed。"""

    class _Bogus:
        engineering_task_id = TASK_ID
        attempt_number = 1
        parent_approval_id = 1
        thread_id = "th"
        turn_id = "tu"
        item_id = "it"
        command_digest = "not-a-digest"
        working_directory = "/abs"

    _task(db)
    with pytest.raises(InvalidEngineeringCommandRequestError):
        request_engineering_command_approval(db, _Bogus(), config=_config())


def test_request_rejects_a_handle_missing_a_required_field(db):
    class _Partial:
        engineering_task_id = TASK_ID
        attempt_number = 1
        #: 少了 parent_approval_id 等欄位

    _task(db)
    with pytest.raises(InvalidEngineeringCommandRequestError):
        request_engineering_command_approval(db, _Partial(), config=_config())


def test_unknown_task_is_rejected(db):
    _task(db)
    with pytest.raises(InvalidEngineeringCommandRequestError):
        request_engineering_command_approval(
            db, _handle("22222222-2222-4222-8222-222222222222", 1), config=_config()
        )


# ---------------------------------------------------------------------------
# 3. 去重與決定落地
# ---------------------------------------------------------------------------


def test_resent_provider_request_does_not_create_a_second_card(db):
    """app-server 重送同一個 command/approve 時不得長出第二張卡。"""
    task_id, parent = _task(db)
    first = request_engineering_command_approval(
        db, _handle(task_id, parent), config=_config()
    )
    second = request_engineering_command_approval(
        db, _handle(task_id, parent), config=_config()
    )
    assert first.id == second.id
    assert len(db.list_approvals(kind="engineering_command")) == 1


def test_different_item_gets_its_own_card(db):
    task_id, parent = _task(db)
    request_engineering_command_approval(
        db, _handle(task_id, parent, item_id="it-1"), config=_config()
    )
    request_engineering_command_approval(
        db, _handle(task_id, parent, item_id="it-2"), config=_config()
    )
    assert len(db.list_approvals(kind="engineering_command")) == 2


def test_approval_records_the_decision_without_contacting_a_provider(db):
    """核准只落地決定＋稽核；送回 app-server 是 runtime 的事。"""
    task_id, parent = _task(db)
    approval = request_engineering_command_approval(
        db, _handle(task_id, parent), config=_config()
    )
    result = asyncio.run(
        approve(db, approval.id, app_state=_State(_config()), audit_path="/dev/null")
    )
    assert result["approval"].status == "approved"
    #: 回傳裡沒有任何 provider/session 物件——這個分支不碰 provider。
    assert set(result) == {"approval"}


def test_approve_revalidates_that_the_task_still_exists(db):
    task_id, parent = _task(db)
    approval = request_engineering_command_approval(
        db, _handle(task_id, parent), config=_config()
    )
    with db.cursor() as cur:
        cur.execute("DELETE FROM engineering_tasks WHERE id = ?", (task_id,))

    result = asyncio.run(
        approve(db, approval.id, app_state=_State(_config()), audit_path="/dev/null")
    )
    assert result["approval"].status == "rejected"


# ---------------------------------------------------------------------------
# 4. 邊界：永不自動核准
# ---------------------------------------------------------------------------


def test_engineering_command_is_never_auto_approved(db):
    """INV-APPROVAL-4：白名單恰好 enqueue|stop，不因 D-3 擴大。"""
    task_id, parent = _task(db)
    approval = request_engineering_command_approval(
        db, _handle(task_id, parent), config=_config()
    )
    decided = asyncio.run(
        maybe_auto_approve(
            db,
            approval,
            source="api",
            rules=[{"kind": "any", "action": "approve"}],
            app_state=_State(_config()),
            audit_path="/dev/null",
        )
    )
    assert decided is None
    assert db.get_approval(approval.id).status == "pending"


# ---------------------------------------------------------------------------
# 5. D-4：三個 kind 全部刻意未定義
# ---------------------------------------------------------------------------


def test_all_three_d4_kinds_are_deliberately_absent():
    """D-4 的三個 kind 目前**全部**不存在，而且是刻意的：

    - `engineering_task_pr`：形狀雖然可由
      `app.github_publication.DraftPullRequestRequest` 決定，但
      `tests/test_github_publication.py` 的邊界測試明文要求那個模組
      「deliberately unreachable from any approval/API/execution code path」。
      建立一個綁定它的 approval kind 會讓它變成可達，正是該測試禁止的事。
      D6 裁定本身也寫「本輪**未實作**——排入下一輪」。
    - `engineering_task_finalize` / `_promote`：repo 裡沒有任何型別或流程
      定義 finalize/promote 的語意，定義它們等於臆造語意。

    這個測試把「刻意不做」釘住，避免日後被誤認為漏掉——要做的話必須先有
    D6 預告的具名操作設定裁定，並同時處理那個 unwired 邊界測試。
    """
    for kind in (
        "engineering_task_pr",
        "engineering_task_finalize",
        "engineering_task_promote",
    ):
        assert kind not in VALID_APPROVAL_KINDS
