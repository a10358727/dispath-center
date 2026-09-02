"""Human-readable presentation of approval cards (整頓 U2, DG-CONSOLIDATION-v1).

Pure presentation: this module never mutates or replaces the authoritative
payload, never raises for a missing/odd payload shape, and carries no secrets
— callers must hand it the review-safe payload (``review_payload`` when one
exists), never raw server-config bytes.
"""

from __future__ import annotations

import re
from typing import Any

#: Every kind in ``VALID_APPROVAL_KINDS`` must have a Chinese title here;
#: ``tests/test_approval_presentation.py`` forbids falling back to the raw
#: kind string.
KIND_TITLES: dict[str, str] = {
    "agent_runner_enroll": "登錄 runner agent",
    "agent_runner_revoke": "撤銷 runner agent",
    "agent_session_checkpoint": "存檔工作區變更",
    "agent_session_open": "開啟 Agent session",
    "apply_patch": "套用修補",
    "auto_placement": "自動放置提案",
    "coding_task": "改碼任務（已退役通道）",
    "dataset_alias_change_v2": "調整資料集別名",
    "dataset_asset_adoption_v2": "採納資料集資產",
    "dataset_grant_revoke_v2": "撤回資料集授權",
    "dataset_prewarm": "資料集預熱",
    "dataset_publish_v2": "發布資料集",
    "dataset_share_accept_v2": "接受資料集分享",
    "dataset_share_offer_v2": "提出資料集分享",
    "dataset_snapshot_build": "建立資料集快照",
    "dispatch_policy_archive": "封存派工政策",
    "dispatch_policy_create": "建立派工政策",
    "dispatch_policy_update": "更新派工政策",
    "engineering_command": "工程指令",
    "engineering_task_discard": "捨棄工程任務",
    "engineering_task_promote": "晉升為正式版本",
    "engineering_task_retry": "重試工程任務",
    "enqueue": "排入任務",
    "environment_change_v2": "環境設定變更",
    "execution_plan_v2": "執行一個 Run",
    "experiment_create_v2": "建立實驗",
    "git_init": "初始化 Git 儲存庫",
    "ignore_nested_candidates": "批次忽略巢狀候選",
    "ignore_project_candidate": "忽略專案候選",
    "import_project": "匯入專案",
    "inventory_scan": "掃描機器",
    "node_enroll": "登錄 Node agent",
    "node_retire": "汰除 Node agent",
    "node_revoke": "撤銷 Node agent",
    "node_rotate": "輪替 Node 憑證",
    "plan_run": "執行計畫 Run",
    "project_bootstrap_v2": "建立新專案",
    "project_defaults_change_v2": "設定預設參數",
    "project_deploy": "部署專案到機器",
    "project_instance_update_v2": "同步執行機器到版本",
    "project_membership_remove": "移除專案成員",
    "project_membership_upsert": "新增／更新專案成員",
    "project_role_change": "變更專案角色",
    "run_profile_archive": "封存 Run profile",
    "run_profile_create": "建立 Run profile",
    "run_profile_update": "更新 Run profile",
    "run_template_change_v2": "執行模板變更",
    "server_add": "新增機器設定",
    "server_bootstrap": "初始化機器",
    "server_delete": "刪除機器設定",
    "server_disable": "停用機器設定",
    "server_update": "更新機器設定",
    "service_account_create": "建立服務帳號",
    "service_token_issue": "簽發服務憑證",
    "service_token_revoke": "撤銷服務憑證",
    "stop": "停止任務",
}


#: Absolute filesystem paths never enter a summary: the Product v2 list is a
#: safe surface (paths are redacted from every list/error projection).
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.])/[^\s'\"]+")


def _text(value: Any, limit: int = 80) -> str:
    if isinstance(value, (str, int, float, bool)):
        text = _ABSOLUTE_PATH_RE.sub("…", str(value)).strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"
    return ""


def _join(parts: list[str]) -> str:
    return " · ".join(part for part in parts if part)


def _get(payload: Any, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _summary(kind: str, payload: Any) -> str:
    p = payload if isinstance(payload, dict) else {}
    if kind in ("enqueue", "stop"):
        return _join([
            _text(_get(p, "project")),
            _text(_get(p, "command")),
            _text(_get(p, "pin_server", "server")),
            _text(_get(p, "job_id")) and f"任務 #{_text(_get(p, 'job_id'))}",
        ])
    if kind == "inventory_scan":
        return _text(_get(p, "server"))
    if kind == "import_project":
        source = _text(_get(p, "server"))
        name = _text(_get(p, "name", "project"))
        return f"{name} ← {source}" if name and source else name or source
    if kind in ("ignore_project_candidate", "ignore_nested_candidates"):
        count = _get(p, "candidate_ids", "candidates")
        counted = f"{len(count)} 筆" if isinstance(count, list) else ""
        return _join([_text(_get(p, "server")), counted])
    if kind == "agent_session_open":
        return _join([_text(_get(p, "project", "project_name")), _text(_get(p, "base_version_id"), 12)])
    if kind == "agent_session_checkpoint":
        return _join([_text(_get(p, "project", "project_name")), _text(_get(p, "workspace_branch"))])
    if kind == "engineering_task_promote":
        return _join([_text(_get(p, "project_name", "project")), _text(_get(p, "git_commit"), 12)])
    if kind in ("environment_change_v2", "run_template_change_v2", "project_defaults_change_v2"):
        target = _get(p, "environment", "template", "defaults", "target_revision")
        name = _text(_get(target, "name")) if isinstance(target, dict) else ""
        return _join([_text(_get(p, "operation")), name])
    if kind == "project_instance_update_v2":
        return _join([_text(_get(p, "server_name", "server")), _text(_get(p, "git_commit", "project_version_id"), 12)])
    if kind == "execution_plan_v2":
        return _join([_text(_get(p, "project_name", "project")), _text(_get(p, "server_name", "server"))])
    if kind == "experiment_create_v2":
        run_count = _get(p, "run_count")
        return _join([
            _text(_get(p, "project_name", "project")),
            f"{run_count} 個 run" if isinstance(run_count, int) else "",
        ])
    if kind.startswith("dataset_"):
        return _join([
            _text(_get(p, "dataset", "dataset_name", "asset_name", "alias", "name")),
            _text(_get(p, "version", "snapshot_id"), 20),
        ])
    if kind.startswith("server_"):
        return _text(_get(p, "name", "server_name", "server"))
    if kind.startswith("node_"):
        return _text(_get(p, "node_name", "node_id", "server_name"))
    if kind in ("project_membership_upsert", "project_membership_remove", "project_role_change"):
        return _join([_text(_get(p, "project", "project_id"), 24), _text(_get(p, "actor_id", "member_actor_id"), 24)])
    # Generic fallback: up to three scalar fields, so an unknown or historical
    # payload still gives the reviewer something better than nothing.
    parts: list[str] = []
    if isinstance(p, dict):
        for key, value in p.items():
            text = _text(value, 40)
            if text and key not in ("yaml_after_utf8_b64", "yaml_before_sha256", "yaml_after_sha256"):
                parts.append(f"{key}={text}")
            if len(parts) >= 3:
                break
    return _join(parts)


def describe_approval(kind: str, payload: Any = None) -> dict[str, str]:
    """Return ``{"title", "summary"}`` for one approval card.

    Never raises; an unknown kind gets the raw kind as its title (tests pin
    that no registered kind takes that branch).
    """

    title = KIND_TITLES.get(kind, kind)
    try:
        summary = _summary(kind, payload)
    except Exception:  # pragma: no cover - presentation must never break a list
        summary = ""
    return {"title": title, "summary": summary}
