"""任務結束 hook：狀態更新之後依序「拉結果 → 寄信 → 寫稽核」（PLAN.md E
節固定順序）。

狀態更新本身由 `app.jobqueue.apply_reconcile_outcome()` 在呼叫這裡之前就
已經落地完成（見該函式對 `on_job_finished` 的說明），所以這個模組只負責
剩下三步。這裡刻意寫成一個獨立、不管背景排程細節的協調函式：不呼叫
`asyncio.create_task()`、不做例外吞噬與 task 追蹤（那些是
`app.main.AppState.schedule_job_finished_hook()` 的責任），方便直接用假的
`local_run`/`send_mail` 注入單元測試「順序」與「拉結果失敗不影響後續」這
兩件事，不需要真的起背景 task。
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from app.audit import SYSTEM_AUDIT_ACTOR, append_audit
from app.config import AppConfig, ServerConfig
from app.db import Database, Job
from app.engineering_tasks import (
    ENGINEERING_TASK_BINARY_FILE_LIMIT,
    inspect_engineering_result_file,
    load_engineering_result_json,
    redact_engineering_text,
)
from app.engineering_path_policy import (
    CONTRACT_VERSION as ENGINEERING_TASK_CONTRACT_V2,
    EngineeringPathContractError,
    EngineeringPathScopeViolation,
    EngineeringPathSecretViolation,
    validate_engineering_path_policy,
    validate_engineering_path_verifier_contract,
    verify_engineering_git_range,
)
from app.engineering_validation import (
    engineering_validation_job_contract_failure,
    record_engineering_validation_contract_refusal,
)
from app.hub import hub_repo_path
from app.jobqueue import (
    engineering_coding_job_runner_contract_matches,
    engineering_job_command_contract_matches,
)
from app.llm import summarize_mail_body as default_summarize_mail_body
from app.mailer import build_job_mail
from app.mailer import send_mail as default_send_mail
from app.results import local_result_dir, pull_job_results

logger = logging.getLogger(__name__)

_EXACT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_PATH_POLICY_RESULT_REJECTION = "最終 Git 結果違反 approved path policy"
_SECRET_POLICY_RESULT_REJECTION = "最終 Git 結果包含受保護檔案"


def _engineering_job_mail_projection(job: Job) -> Job:
    """Return a notification-only projection without execution internals.

    Immutable Engineering Task Jobs contain deterministic transport commands
    with Server A paths and SSH connection arguments.  Those values are
    necessary for the approved executor, but they are not notification data.
    Legacy Jobs deliberately keep using :func:`build_job_mail` unchanged.
    """

    if job.engineering_validation_request_id is not None:
        display_command = (
            "Push verified Engineering Task bundle to approved worker"
            if job.type == "sync"
            else "Run approved Engineering Task worker validation"
        )
    else:
        display_command = {
            "staging": "Prepare immutable Engineering Task inputs",
            "coding": "Run Codex agent in an isolated worktree",
            "validation": "Run approved Engineering Task validation",
        }.get(job.engineering_task_role or "", "Run Engineering Task step")
    safe_log = redact_engineering_text(job.log_tail or "", max_chars=12_000)
    if safe_log["withheld"]:
        log_tail = "（任務日誌含敏感內容，已隱藏；請在安全任務頁查看遮罩後的狀態）"
    else:
        log_tail = safe_log["content"] or "（無可顯示的任務日誌）"
    return replace(job, command=display_command, log_tail=log_tail)


def _register_engineering_visibility_artifacts(
    *,
    db: Database,
    coding_run,
    job: Job,
    fields: dict[str, Any],
    result_dir: Path,
) -> None:
    """Persist only opaque, verified/redacted artifact metadata.

    Visibility collection is additive and must never change the canonical
    CodingRun result.  Callers therefore catch failures after the result
    transaction has committed.
    """

    if coding_run.engineering_task_id is None or coding_run.attempt_number is None:
        return

    common = {
        "task_id": coding_run.engineering_task_id,
        "attempt_number": coding_run.attempt_number,
        "coding_run_id": coding_run.id,
        "source_job_id": job.id,
        "storage_kind": "local_result",
    }
    specs = (
        ("result-metadata", "result_metadata", "Result metadata", "result.json", False),
        ("final-response", "final_response", "Final response", "final_message.txt", True),
        ("diff", "diff", "Code diff", "diff.patch", True),
        ("bundle", "bundle", "Verified change bundle", "changes.bundle", False),
    )
    for artifact_key, kind, label, filename, include_text in specs:
        inspected = inspect_engineering_result_file(
            result_dir=str(result_dir),
            filename=filename,
            include_text=include_text,
        )
        if kind == "bundle":
            verified = fields.get("status") == "done" and bool(fields.get("bundle_path"))
            verification_status = "verified" if verified else "rejected"
            redaction_status = "not_applicable"
            availability = (
                "available"
                if verified and inspected.get("available")
                else "rejected" if inspected.get("available") else "missing"
            )
        elif kind == "diff" and fields.get("status") in {
            "secret_violation",
            "path_policy_violation",
        }:
            # A refusing Runner should not send a diff.  If an untrusted or old
            # Runner does, record only that it was rejected; never project its
            # paths/content as an available task artifact.
            verification_status = "rejected"
            redaction_status = "withheld"
            availability = (
                "rejected" if inspected.get("available") else "missing"
            )
        elif kind == "result_metadata":
            verification_status = "not_required"
            redaction_status = "withheld"
            availability = "available" if inspected.get("available") else "missing"
        else:
            verification_status = "not_required"
            if inspected.get("withheld"):
                redaction_status = "withheld"
                availability = "withheld"
            elif inspected.get("available"):
                redaction_status = "redacted"
                availability = "available"
            else:
                redaction_status = "pending"
                availability = "missing"
        artifact = db.register_engineering_task_artifact(
            **common,
            artifact_key=artifact_key,
            kind=kind,
            label=label,
            storage_key=filename,
            content_type={
                "result.json": "application/json",
                "final_message.txt": "text/plain",
                "diff.patch": "text/x-diff",
                "changes.bundle": "application/x-git-bundle",
            }[filename],
            verification_status=verification_status,
            redaction_status=redaction_status,
            availability=availability,
        )
        db.record_engineering_task_artifact_collection(
            artifact.id,
            source_sha256=inspected.get("sha256"),
            source_size_bytes=inspected.get("size_bytes"),
            verification_status=verification_status,
            redaction_status=redaction_status,
            availability=availability,
        )

    if fields.get("test_command") is not None:
        db.register_engineering_task_artifact(
            task_id=coding_run.engineering_task_id,
            attempt_number=coding_run.attempt_number,
            artifact_key="test-result",
            kind="test_result",
            label="Validation result",
            storage_kind="recorded_result",
            coding_run_id=coding_run.id,
            source_job_id=job.id,
            verification_status="not_required",
            redaction_status="not_applicable",
            availability="available",
        )


def _verify_pinned_result_bundle(
    *,
    project: str,
    base_commit: str,
    result_commit: str,
    bundle_path: Path,
    local_home_dir: str,
    path_policy: Optional[dict[str, Any]] = None,
    path_policy_sha256: Optional[str] = None,
    path_verifier: Optional[dict[str, str]] = None,
) -> tuple[bool, str]:
    """Verify a Runner bundle against the canonical Hub without importing it.

    A temporary bare repo receives the approved base from Hub, verifies bundle
    prerequisites, imports only the reported result SHA, and proves the base is
    its ancestor.  Commands use argv (no shell); diagnostics stay fixed so local
    paths or Git output are not persisted into task errors.
    """

    if not isinstance(base_commit, str) or not _EXACT_COMMIT_RE.fullmatch(base_commit):
        return False, "approved base commit 格式不合法"
    if not isinstance(result_commit, str) or not _EXACT_COMMIT_RE.fullmatch(result_commit):
        return False, "Runner result commit 格式不合法"
    hub_path = Path(hub_repo_path(project, local_home_dir)).resolve()
    if not hub_path.is_dir():
        return False, "Hub 或 changes.bundle 不存在"

    def run(args: list[str], *, timeout: int = 60) -> bool:
        try:
            completed = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    bundle_fd: int | None = None
    try:
        # Canonical verification and the artifact journal must inspect the same
        # safe file type.  Reject links/devices/FIFOs before Git can follow them,
        # then copy the exact bounded descriptor bytes into the private verify
        # directory so a path swap cannot change what Git consumes.
        path_stat = os.lstat(bundle_path)
        if not stat.S_ISREG(path_stat.st_mode):
            return False, "changes.bundle 不是安全的一般檔案"
        bundle_fd = os.open(
            bundle_path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        source_stat = os.fstat(bundle_fd)
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or source_stat.st_size > ENGINEERING_TASK_BINARY_FILE_LIMIT
        ):
            return False, "changes.bundle 不是安全的一般檔案"
        with tempfile.TemporaryDirectory(prefix="engineering-bundle-verify-") as tmp:
            verified_bundle = Path(tmp) / "changes.bundle"
            remaining = source_stat.st_size
            copied = 0
            with verified_bundle.open("xb") as destination:
                while remaining:
                    chunk = os.read(bundle_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    destination.write(chunk)
                    copied += len(chunk)
                    remaining -= len(chunk)
            final_stat = os.fstat(bundle_fd)
            source_identity = (
                source_stat.st_dev,
                source_stat.st_ino,
                source_stat.st_size,
                source_stat.st_mtime_ns,
                source_stat.st_ctime_ns,
            )
            final_identity = (
                final_stat.st_dev,
                final_stat.st_ino,
                final_stat.st_size,
                final_stat.st_mtime_ns,
                final_stat.st_ctime_ns,
            )
            if copied != source_stat.st_size or final_identity != source_identity:
                return False, "changes.bundle 在驗證期間發生變更"

            verify_repo = str(Path(tmp) / "verify.git")
            git_dir_arg = f"--git-dir={verify_repo}"
            object_format = "sha256" if len(base_commit) == 64 else "sha1"
            if not run(
                [
                    "git",
                    "init",
                    "--bare",
                    f"--object-format={object_format}",
                    verify_repo,
                ]
            ):
                return False, "無法建立 bundle verification repo"
            if not run(
                [
                    "git",
                    git_dir_arg,
                    "fetch",
                    "--force",
                    "--no-tags",
                    str(hub_path),
                    f"{base_commit}:refs/heads/approved-base",
                ]
            ):
                return False, "approved base 已無法從 Hub 驗證"
            if not run(
                ["git", git_dir_arg, "bundle", "verify", str(verified_bundle)]
            ):
                return False, "changes.bundle prerequisite/格式驗證失敗"
            if not run(
                [
                    "git",
                    git_dir_arg,
                    "fetch",
                    "--force",
                    "--no-tags",
                    str(verified_bundle),
                    f"{result_commit}:refs/heads/result",
                ]
            ):
                return False, "changes.bundle 不含 Runner result commit"
            if not run(
                [
                    "git",
                    git_dir_arg,
                    "merge-base",
                    "--is-ancestor",
                    "refs/heads/approved-base",
                    "refs/heads/result",
                ]
            ):
                return False, "result commit 不是 approved base 的後代"
            policy_values = (
                path_policy,
                path_policy_sha256,
                path_verifier,
            )
            if any(value is not None for value in policy_values):
                if any(value is None for value in policy_values):
                    return False, "approved path policy contract 不完整"
                try:
                    validated_policy = validate_engineering_path_policy(
                        path_policy,
                        path_policy_sha256,
                    )
                    validated_verifier = (
                        validate_engineering_path_verifier_contract(
                            path_verifier
                        )
                    )
                    if validated_policy.get("verifier") != validated_verifier:
                        return False, "approved path policy/verifier contract 不一致"
                    verify_engineering_git_range(
                        verify_repo,
                        base_commit,
                        result_commit,
                        validated_policy,
                        path_policy_sha256,
                        bare=True,
                    )
                except EngineeringPathSecretViolation:
                    return False, _SECRET_POLICY_RESULT_REJECTION
                except EngineeringPathScopeViolation:
                    return False, _PATH_POLICY_RESULT_REJECTION
                except EngineeringPathContractError:
                    return False, "approved path policy contract 無法驗證"
    except OSError:
        return False, "bundle verification 暫存空間不可用"
    finally:
        if bundle_fd is not None:
            os.close(bundle_fd)
    return True, ""


def _safe_codex_version(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text or len(text) > 120 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+()-]*", text):
        return None
    return text


def _safe_result_branch(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text if re.fullmatch(r"ai-task-[1-9][0-9]*(?:-[1-9][0-9]*)?", text) else None


def _safe_commit(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[0-9a-fA-F]{40,64}", text) else None


def _safe_test_command(value: object) -> Optional[str]:
    return "python3 -m pytest -q" if value == "python3 -m pytest -q" else None


def _safe_exit_code(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or not -255 <= value <= 255:
        return None
    return value


_SAFE_SKIP_VALIDATION_MARKER = "自動 repository validation 已安全跳過"


def _safe_reported_test_result(
    job: Job, data: dict[str, Any]
) -> tuple[Optional[str], Optional[int]]:
    """Accept test metadata only from an already-approved legacy journal.

    The current deterministic wrapper never runs repository code after the
    Codex sandbox closes and therefore cannot truthfully emit a test result.
    Existing in-flight command journals from before that hardening retain their
    compatibility behavior; a Runner cannot make a new journal display a
    fabricated passing test merely by editing ``result.json``.
    """

    if _SAFE_SKIP_VALIDATION_MARKER in (job.command or ""):
        return None, None
    return _safe_test_command(data.get("test_command")), _safe_exit_code(
        data.get("test_exit_code")
    )


def _backfill_coding_run(job: Job, *, db: Database, config: AppConfig, audit_path: str) -> None:
    """階段 13（PLAN.md N.6，Codex Worker v2）：`job.type == "coding"` 的
    job 結束後，解析本地已回收的 `results/{job_id}/result.json`（見
    `app/approvals.py` 的 `build_coding_task_script()` 的 `write_result()`
    ——結構化結果的唯一來源）回填對應的 `coding_runs` 一列。

    - 找不到對應的 `coding_runs`（`db.get_coding_run_by_job_id()` 回傳
      `None`）：v1 遺留的 coding job（approve() 沒有建過 coding_run）——
      直接跳過，不當成錯誤，呼叫端仍照常往下寄信。
    - `result.json` 不存在或不是合法 JSON（例如 rsync 拉結果失敗、
      任務腳本在寫檔前就整個中止）：`status="failed"`，
      `error_message` 附上 `job.exit_code` 供人到 job log 追查——這正是
      「拉結果條件放寬到 coding 的 failed 任務」的理由（見
      `handle_job_finished()` docstring）：這種情況下 job 本身也是
      `failed`，但我們仍然嘗試過拉一次，只是沒拉到東西。
    - 合法 JSON：直接把 `result.json` 的欄位映射進
      `coding_runs`（`status`/`base_commit`/`result_branch`/
      `result_commit`/`test_command`/`test_exit_code`/`error_message`/
      `codex_version`），`bundle_path` 只在本地確實有
      `results/{job_id}/changes.bundle` 檔案時才填（no_changes／
      secret_violation／codex 失敗這幾種終態不會有這個檔案）。
      `started_at`/`finished_at` 直接抄 `job.started_at`/`job.finished_at`
      （scheduler dispatch 時已經記過，不需要另外記 coding_dispatch）。
    - 不論以上哪一種結果都寫稽核 `coding_finished`
      （`job_id`/`coding_run_id`/`status`/`result_commit`）。
    """
    coding_run = db.get_coding_run_by_job_id(job.id)
    if coding_run is None:
        return

    engineering_task = (
        db.get_engineering_task(coding_run.engineering_task_id)
        if coding_run.engineering_task_id is not None
        else None
    )
    path_policy_kwargs: dict[str, Any] = {}
    native_contract_error: Optional[str] = None
    if coding_run.base_binding == "project_version_pinned":
        if engineering_task is None:
            native_contract_error = "Engineering Task parent 不存在；結果已拒絕"
        elif engineering_task.contract_version == ENGINEERING_TASK_CONTRACT_V2:
            execution_contract = engineering_task.execution_contract
            path_policy_kwargs = {
                "path_policy": execution_contract.get("path_policy"),
                "path_policy_sha256": execution_contract.get(
                    "path_policy_sha256"
                ),
                "path_verifier": execution_contract.get("path_verifier"),
            }
            if any(value is None for value in path_policy_kwargs.values()):
                native_contract_error = (
                    "Engineering Task v2 path policy contract 不完整；結果已拒絕"
                )
        elif engineering_task.contract_version != "engineering-task-v1":
            native_contract_error = (
                "Engineering Task contract version 未受支援；結果已拒絕"
            )

    result_dir = Path(local_result_dir(job.id, config.local_home_dir))
    result_json_path = result_dir / "result.json"
    bundle_path_candidate = result_dir / "changes.bundle"
    pinned_result_data: dict[str, Any] | None = None
    if coding_run.base_binding == "project_version_pinned":
        result_metadata, pinned_result_data = load_engineering_result_json(
            result_dir=str(result_dir)
        )
    else:
        result_metadata = inspect_engineering_result_file(
            result_dir=str(result_dir), filename="result.json", include_text=False
        )

    if (
        coding_run.base_binding == "project_version_pinned"
        and not result_metadata.get("available")
        and result_metadata.get("reason") in {"missing", "unreadable"}
    ):
        # A transport/result-collection gap is not execution failure.  Keep the
        # parent in finalizing so recovery can retry collection after restart.
        db.append_engineering_task_event(
            task_id=coding_run.engineering_task_id,
            attempt_number=coding_run.attempt_number,
            event_key=f"coding-run:{coding_run.id}:collection-pending:{job.finished_at or job.id}",
            event_type="result_collection_pending",
            phase="finalization",
            state="finalizing",
            source_kind="collector",
            source_id=str(job.id),
            summary="執行已結束，但結果尚未成功收集",
            details={"coding_run_id": coding_run.id, "job_id": job.id},
            occurred_at=job.finished_at,
        )
        return

    fields: dict[str, Any] = {
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
    if coding_run.base_binding == "project_version_pinned":
        # Every retry starts by revoking previously derived result pointers.
        # They are attached again only after the current result and bundle pass.
        fields.update(
            {
                "result_branch": None,
                "result_commit": None,
                "bundle_path": None,
                "test_command": None,
                "test_exit_code": None,
            }
        )
    try:
        if coding_run.base_binding == "project_version_pinned":
            if not result_metadata.get("available") or pinned_result_data is None:
                raise ValueError("result.json failed safe file validation")
            data = pinned_result_data
        else:
            data = json.loads(result_json_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("result.json 內容不是 JSON object")
    except (OSError, ValueError) as exc:
        fields["status"] = "failed"
        fields["error_message"] = (
            f"result.json 缺失或不可解析；job exit={job.exit_code}，見 job log"
        )
        logger.warning(
            "coding_run #%s（job #%s）回填失敗，result.json 缺失或不可解析：%s",
            coding_run.id,
            job.id,
            exc,
        )
    else:
        reported_base = data.get("base_commit")
        if (
            coding_run.base_binding != "project_version_pinned"
            and _SAFE_SKIP_VALIDATION_MARKER not in (job.command or "")
        ):
            # Preserve already-approved legacy command journals exactly.  New
            # compatibility requests use the hardened skip-validation wrapper
            # below and receive an allowlisted, pre-DB projection instead.
            fields["status"] = data.get("status") or "failed"
            fields["base_commit"] = reported_base
            fields["result_branch"] = data.get("result_branch")
            fields["result_commit"] = data.get("result_commit")
            fields["test_command"] = data.get("test_command")
            fields["test_exit_code"] = data.get("test_exit_code")
            fields["error_message"] = data.get("error_message")
            fields["codex_version"] = data.get("codex_version")
            if bundle_path_candidate.exists():
                fields["bundle_path"] = str(bundle_path_candidate)
        elif coding_run.base_binding != "project_version_pinned":
            reported_status = data.get("status")
            if reported_status not in {
                "done",
                "no_changes",
                "failed",
                "secret_violation",
            }:
                reported_status = "failed"
            fields["status"] = reported_status
            fields["base_commit"] = _safe_commit(reported_base)
            fields["result_branch"] = _safe_result_branch(
                data.get("result_branch")
            )
            fields["result_commit"] = _safe_commit(data.get("result_commit"))
            fields["test_command"] = None
            fields["test_exit_code"] = None
            fields["codex_version"] = _safe_codex_version(
                data.get("codex_version")
            )
            fields["error_message"] = (
                None
                if reported_status in {"done", "no_changes"}
                else (
                    "Coding Runner 安全檢查拒絕了結果；敏感細節已隱藏"
                    if reported_status == "secret_violation"
                    else "Coding Runner 回報執行失敗；請查看經過遮罩的任務日誌"
                )
            )
            if reported_status == "done" and bundle_path_candidate.exists():
                fields["bundle_path"] = str(bundle_path_candidate)
        elif reported_base != coding_run.base_commit:
            fields["status"] = "failed"
            fields["error_message"] = (
                "Runner 回報的 base_commit 與 approved ProjectVersion 不一致；"
                "結果與 bundle 已拒絕"
            )
            fields["codex_version"] = _safe_codex_version(data.get("codex_version"))
        else:
            reported_status_value = data.get("status")
            reported_status = (
                reported_status_value
                if isinstance(reported_status_value, str)
                else "invalid"
            )
            result_commit = data.get("result_commit")
            fields["codex_version"] = _safe_codex_version(data.get("codex_version"))
            if native_contract_error is not None:
                fields["status"] = "failed"
                fields["error_message"] = native_contract_error
            elif reported_status == "done":
                verified, verify_error = _verify_pinned_result_bundle(
                    project=coding_run.project,
                    base_commit=coding_run.base_commit,
                    result_commit=result_commit,
                    bundle_path=bundle_path_candidate,
                    local_home_dir=config.local_home_dir,
                    **path_policy_kwargs,
                )
                if not verified:
                    if verify_error == _PATH_POLICY_RESULT_REJECTION:
                        fields["status"] = "path_policy_violation"
                        fields["error_message"] = (
                            "Server A 拒絕了超出 approved path policy 的最終結果"
                        )
                    elif verify_error == _SECRET_POLICY_RESULT_REJECTION:
                        fields["status"] = "secret_violation"
                        fields["error_message"] = (
                            "Server A 拒絕了包含受保護檔案的最終結果"
                        )
                    else:
                        fields["status"] = "failed"
                        fields["error_message"] = (
                            f"Runner result bundle 已拒絕：{verify_error}"
                        )
                else:
                    fields["status"] = "done"
                    fields["result_branch"] = _safe_result_branch(data.get("result_branch"))
                    fields["result_commit"] = result_commit
                    (
                        fields["test_command"],
                        fields["test_exit_code"],
                    ) = _safe_reported_test_result(job, data)
                    fields["error_message"] = None
                    fields["bundle_path"] = str(bundle_path_candidate)
            elif reported_status == "no_changes":
                if result_commit != coding_run.base_commit:
                    fields["status"] = "failed"
                    fields["error_message"] = (
                        "Runner no_changes result 並未停在 approved base；結果已拒絕"
                    )
                else:
                    fields["status"] = "no_changes"
                    fields["result_branch"] = _safe_result_branch(data.get("result_branch"))
                    fields["result_commit"] = result_commit
                    (
                        fields["test_command"],
                        fields["test_exit_code"],
                    ) = _safe_reported_test_result(job, data)
                    fields["error_message"] = None
            elif reported_status in {
                "failed",
                "secret_violation",
                "path_policy_violation",
            }:
                if (
                    reported_status == "path_policy_violation"
                    and engineering_task.contract_version
                    != ENGINEERING_TASK_CONTRACT_V2
                ):
                    fields["status"] = "failed"
                    fields["error_message"] = (
                        "legacy Runner 回報未知 coding status；結果已拒絕"
                    )
                    reported_status = "invalid"
                else:
                    fields["status"] = reported_status
                    fields["error_message"] = (
                        "Coding Runner 安全檢查拒絕了結果；敏感細節已隱藏"
                        if reported_status
                        in {"secret_violation", "path_policy_violation"}
                        else "Coding Runner 回報執行失敗；請查看經過遮罩的任務日誌"
                    )
            else:
                fields["status"] = "failed"
                fields["error_message"] = "Runner 回報未知 coding status；結果已拒絕"

    db.update_coding_run(coding_run.id, **fields)
    if coding_run.engineering_task_id is not None:
        try:
            _register_engineering_visibility_artifacts(
                db=db,
                coding_run=coding_run,
                job=job,
                fields=fields,
                result_dir=result_dir,
            )
        except Exception as exc:  # noqa: BLE001 - visibility must not block notification
            logger.warning(
                "engineering task artifact metadata collection failed for coding_run #%s: %s",
                coding_run.id,
                type(exc).__name__,
            )
    audit_params = {
        "job_id": job.id,
        "coding_run_id": coding_run.id,
        "status": fields.get("status"),
        "result_commit": fields.get("result_commit"),
    }
    if coding_run.engineering_task_id is not None:
        audit_params["engineering_task_id"] = coding_run.engineering_task_id
    append_audit(
        "coding_finished",
        audit_params,
        path=audit_path,
        actor=SYSTEM_AUDIT_ACTOR,
    )


async def handle_job_finished(
    job: Job,
    *,
    server_cfg: Optional[ServerConfig],
    local_run,
    config: AppConfig,
    audit_path: str,
    db: Optional[Database] = None,
    send_mail=default_send_mail,
    summarize_mail=default_summarize_mail_body,
) -> dict[str, Any]:
    """任務結束背景 hook 的核心邏輯。

    - 拉結果條件：`job.status == "done"`，**或**（階段 13，PLAN.md N.6）
      `job.type == "coding"` 且 `job.status == "failed"`——coding 任務
      failed 時 `result.json`／`final_message.txt` 往往是唯一的診斷線索
      （codex 執行失敗、secret 檔案違規等任務腳本自己判定的失敗都會先把
      這些檔案複製進 `results/{job_id}/` 才 `exit 1`，見
      `app/approvals.py` 的 `build_coding_task_script()`），不能因為任務
      本身標成 failed 就整個放棄拉結果。其餘 failed/cancelled 任務不拉
      結果，一樣寄信。
    - 拉結果失敗（工作機沒有 `results/{id}/`、rsync 逾時等）**不影響任務
      狀態**——這裡完全不碰 `job.status`，只記稽核 `result_pull_failed`
      後繼續往下寄信；拉成功記稽核 `result_pulled`。
    - 階段 13：拉結果這一步之後（不論拉成功與否），若 `job.type ==
      "coding"` 且呼叫端有傳入 `db`——呼叫 `_backfill_coding_run()` 回填
      對應的 `coding_runs`（見該函式 docstring）。`db` 是選填參數（預設
      `None`）：`app.main.AppState.schedule_job_finished_hook()` 目前還
      沒有接這條線（留給後續批次），沒傳 `db` 時這段整個跳過，不影響
      既有寄信行為，也不會因為缺參數而報錯。
    - 寄信內容的結果路徑：拉成功給本地路徑，否則「無」（`None`，交給
      `build_job_mail()` 轉成「無」字樣）。
    - 階段 5：寄信前若 LLM 可用，把信件內容餵給 `app.llm.summarize_mail_body()`
      產生 ≤3 行摘要，加在信件開頭。`summarize_mail_body()` 本身在 LLM
      不可用時就直接回傳 `None`，這裡再包一層 `try/except` 是防禦性作法
      （就算呼叫端注入的假 `summarize_mail` 意外丟例外，也不能讓寄信這件
      事跟著失敗）——**絕不因為摘要失敗而不寄信**。
    - 最後寫一筆總結稽核 `job_notified`（含是否寄信成功、結果路徑），對應
      PLAN.md「更新狀態→拉結果→寄信→寫稽核」的最後一步。
    """
    protected_job = (
        job.engineering_task_id is not None
        or job.engineering_validation_request_id is not None
    )
    protected_contract_failure: Optional[str] = None
    if job.engineering_validation_request_id is not None:
        server_configs = {server.name: server for server in config.servers}
        validation_failure = (
            "validation_contract_unavailable"
            if db is None
            else engineering_validation_job_contract_failure(
                db,
                job,
                server_configs,
                local_home_dir=config.local_home_dir,
            )
        )
        if validation_failure is not None:
            protected_contract_failure = validation_failure
            if db is not None:
                record_engineering_validation_contract_refusal(
                    db, job, validation_failure
                )
            server_cfg = None
    elif job.engineering_task_id is not None and (
        db is None
        or not engineering_job_command_contract_matches(db, job)
        or not engineering_coding_job_runner_contract_matches(db, job, server_cfg)
    ):
        protected_contract_failure = "approved_execution_contract_mismatch"
        server_cfg = None

    result_path: Optional[str] = None
    should_pull = job.status == "done" or (job.type == "coding" and job.status == "failed")
    result_collection_ok: Optional[bool] = None
    if should_pull and server_cfg is not None:
        pull = await pull_job_results(
            local_run, job.id, server_cfg, config.local_home_dir, config.result_pull_timeout_sec
        )
        if pull.ok:
            result_collection_ok = True
            result_path = pull.path
            pulled_params = {"job_id": job.id, "path": pull.path}
            if protected_job:
                pulled_params = {
                    "job_id": job.id,
                    "engineering_task_id": job.engineering_task_id,
                    "engineering_task_role": job.engineering_task_role,
                    "validation_request_id": (
                        job.engineering_validation_request_id
                    ),
                    "result_location": "managed_local_result",
                }
            append_audit(
                "result_pulled",
                pulled_params,
                path=audit_path,
                actor=SYSTEM_AUDIT_ACTOR,
            )
        else:
            result_collection_ok = False
            failure_params = {"job_id": job.id, "error": pull.error}
            if protected_job:
                failure_params = {
                    "job_id": job.id,
                    "engineering_task_id": job.engineering_task_id,
                    "engineering_task_role": job.engineering_task_role,
                    "validation_request_id": (
                        job.engineering_validation_request_id
                    ),
                    "error_category": "result_transport_failed",
                }
            append_audit(
                "result_pull_failed",
                failure_params,
                result="failed",
                path=audit_path,
                actor=SYSTEM_AUDIT_ACTOR,
            )
    elif should_pull and protected_contract_failure is not None:
        result_collection_ok = False
        # The immutable contract gate deliberately refused transport before
        # any network contact.  Preserve the existing terminal-hook audit
        # shape while exposing only fixed semantic categories—never the raw
        # command, host, path, credential, or verifier exception.
        append_audit(
            "result_pull_failed",
            {
                "job_id": job.id,
                "engineering_task_id": job.engineering_task_id,
                "engineering_task_role": job.engineering_task_role,
                "validation_request_id": job.engineering_validation_request_id,
                "error_category": "result_transport_failed",
                "contract_status": protected_contract_failure,
            },
            result="failed",
            path=audit_path,
            actor=SYSTEM_AUDIT_ACTOR,
        )
    elif should_pull:
        # No exact current server identity means no transport was attempted.
        # Surface this to the durable completion outbox rather than treating a
        # skipped pull as successful collection.
        result_collection_ok = False

    if db is not None and job.type == "coding":
        _backfill_coding_run(job, db=db, config=config, audit_path=audit_path)

    notification_job = (
        _engineering_job_mail_projection(job)
        if protected_job
        else job
    )
    notification_result_path = (
        None if protected_job else result_path
    )
    subject, body = build_job_mail(notification_job, notification_result_path)

    try:
        summary = await summarize_mail(body, config)
    except Exception as exc:  # noqa: BLE001 - 摘要失敗絕不能拖累寄信
        logger.warning("寄信摘要生成失敗，照常寄原信: %s", exc)
        summary = None
    if summary:
        body = f"【AI 摘要】\n{summary}\n\n{body}"

    mailed = await send_mail(config, subject, body)

    notification_params = {
        "job_id": job.id,
        "mailed": mailed,
        "result_path": result_path,
    }
    if protected_job:
        notification_params = {
            "job_id": job.id,
            "engineering_task_id": job.engineering_task_id,
            "engineering_task_role": job.engineering_task_role,
            "validation_request_id": job.engineering_validation_request_id,
            "mailed": mailed,
            "result_available": result_path is not None,
        }
    append_audit(
        "job_notified",
        notification_params,
        path=audit_path,
        actor=SYSTEM_AUDIT_ACTOR,
    )
    return {
        "result_collection_required": should_pull,
        "result_collection_ok": result_collection_ok,
        "result_path_available": result_path is not None,
        "notification_attempted": True,
        "mailed": bool(mailed),
        "owner_projection_attempted": bool(
            db is not None and job.type == "coding"
        ),
    }


async def recover_engineering_task_result(
    job: Job,
    *,
    server_cfg: Optional[ServerConfig],
    local_run,
    config: AppConfig,
    audit_path: str,
    db: Database,
) -> bool:
    """Retry collection for an already-terminal immutable coding Job.

    This recovery path deliberately does not send completion mail again.  It
    only retries the existing approved result pull and idempotent visibility
    ingest; execution state and remote processes are never changed.
    """

    if (
        job.type != "coding"
        or job.engineering_task_id is None
        or job.status not in {"done", "failed"}
    ):
        return False
    run = db.get_coding_run_by_job_id(job.id)
    if run is None or run.base_binding != "project_version_pinned":
        return False
    if run.status in {
        "done",
        "failed",
        "no_changes",
        "secret_violation",
        "path_policy_violation",
    }:
        # The canonical CodingRun result commits before the additive visibility
        # journal.  A transient metadata failure must therefore be repairable
        # without pulling results again or changing execution state.  The
        # registrations are deterministic/idempotent, so a later sweep can fill
        # only the missing rows while leaving completed rows untouched.
        required_artifacts = {
            "result-metadata",
            "final-response",
            "diff",
            "bundle",
        }
        if run.test_command is not None:
            required_artifacts.add("test-result")
        recorded_artifacts = {
            artifact.artifact_key: artifact
            for artifact in db.list_engineering_task_artifacts(
                job.engineering_task_id,
                attempt_number=job.engineering_attempt_number,
            )
        }

        def artifact_is_complete(artifact_key: str) -> bool:
            artifact = recorded_artifacts.get(artifact_key)
            if artifact is None:
                return False
            if (
                artifact.engineering_task_id != job.engineering_task_id
                or artifact.attempt_number != job.engineering_attempt_number
                or artifact.coding_run_id != run.id
                or artifact.source_job_id != job.id
                or artifact.availability == "pending"
                or artifact.verification_status == "pending"
                or (
                    artifact.redaction_status == "pending"
                    and artifact.availability not in {"missing", "cleaned"}
                )
            ):
                return False
            # File-backed rows receive ``collected_at`` even when a legitimate
            # file is missing/withheld.  A NULL value identifies a crash between
            # row registration and collection metadata commit.  ``test-result``
            # is DB-derived and has no separate collection step.
            return artifact_key == "test-result" or artifact.collected_at is not None

        if all(artifact_is_complete(key) for key in required_artifacts):
            return True
        try:
            _register_engineering_visibility_artifacts(
                db=db,
                coding_run=run,
                job=job,
                fields={
                    "status": run.status,
                    "bundle_path": run.bundle_path,
                    "test_command": run.test_command,
                },
                result_dir=Path(local_result_dir(job.id, config.local_home_dir)),
            )
        except Exception as exc:  # noqa: BLE001 - retry on the next recovery sweep
            # Never log exception text: DB/filesystem failures can include
            # credential-bearing values or private local paths.
            logger.warning(
                "engineering task artifact metadata recovery failed for coding_run #%s (%s)",
                run.id,
                type(exc).__name__,
            )
            return False
        repaired_artifacts = {
            artifact.artifact_key: artifact
            for artifact in db.list_engineering_task_artifacts(
                job.engineering_task_id,
                attempt_number=job.engineering_attempt_number,
            )
        }
        recorded_artifacts = repaired_artifacts
        return all(artifact_is_complete(key) for key in required_artifacts)
    if server_cfg is None:
        db.append_engineering_task_event(
            task_id=job.engineering_task_id,
            attempt_number=job.engineering_attempt_number,
            event_key=f"job:{job.id}:recovery-runner-unconfigured",
            event_type="result_collection_pending",
            phase="finalization",
            state="finalizing",
            source_kind="collector",
            source_id=str(job.id),
            summary="結果等待收集；原 Coding Runner 設定目前不存在",
            details={"job_id": job.id, "coding_run_id": run.id},
            occurred_at=job.finished_at,
        )
        return False
    pull = await pull_job_results(
        local_run,
        job.id,
        server_cfg,
        config.local_home_dir,
        config.result_pull_timeout_sec,
    )
    if not pull.ok:
        db.append_engineering_task_event(
            task_id=job.engineering_task_id,
            attempt_number=job.engineering_attempt_number,
            event_key=f"job:{job.id}:recovery-pending:{job.finished_at or job.id}",
            event_type="result_collection_pending",
            phase="finalization",
            state="finalizing",
            source_kind="collector",
            source_id=str(job.id),
            summary="結果收集暫時無法完成；平台將稍後重試",
            details={"job_id": job.id, "coding_run_id": run.id},
            occurred_at=job.finished_at,
        )
        return False
    _backfill_coding_run(job, db=db, config=config, audit_path=audit_path)
    refreshed = db.get_coding_run_by_job_id(job.id)
    return bool(
        refreshed
        and refreshed.status
        in {
            "done",
            "failed",
            "no_changes",
            "secret_violation",
            "path_policy_violation",
        }
    )
