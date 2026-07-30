"""DG-CODE-PROMOTE-v1 local verification and Hub publication.

This module never contacts GitHub and never deletes a worktree, bundle, or
staging repository.  Every shell command is deterministic and all values are
quoted; callers provide the existing Server-A ``local_run`` adapter.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.hub import hub_repo_path
from app.results import local_result_dir

CONTRACT_VERSION = "code-promotion-v1"
PROMOTABLE_TASK_STATUS = "done"


class PromotionCandidateError(ValueError):
    """The task/bundle no longer satisfies the reviewed promotion contract."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class PromotionPublishError(ValueError):
    """A local Git verification/publication command failed."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class PromotionCandidate:
    task_id: str
    project_name: str
    base_project_version_id: str
    base_commit: str
    result_commit: str
    bundle_path: str
    bundle_sha256: str
    bundle_size_bytes: int
    coding_run_id: int
    attempt_number: int


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise PromotionCandidateError(
            "bundle_missing", "bundle is missing or unreadable"
        ) from exc
    return digest.hexdigest(), size


def resolve_promotion_candidate(db, config, task_id: str) -> PromotionCandidate:
    """Resolve only durable, verified native Engineering Task output."""

    task = db.get_engineering_task(task_id)
    if task is None:
        raise PromotionCandidateError(
            "engineering_task_missing", "engineering task does not exist"
        )
    if task.status == "discarded":
        raise PromotionCandidateError(
            "engineering_task_discarded", "discarded task output cannot be promoted"
        )
    if task.status != PROMOTABLE_TASK_STATUS or task.coding_run_id is None:
        raise PromotionCandidateError(
            "engineering_task_not_promotable",
            "engineering task must be successfully terminal before promotion",
        )

    run = db.get_coding_run(task.coding_run_id)
    if (
        run is None
        or run.engineering_task_id != task.id
        or run.status != "done"
        or run.project != task.project_name
        or run.project_version_id != task.project_version_id
        or run.base_binding != "project_version_pinned"
        or run.base_commit != task.base_commit
        or run.attempt_number is None
        or run.attempt_number < 1
        or not isinstance(run.result_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", run.result_commit) is None
        or run.job_id is None
    ):
        raise PromotionCandidateError(
            "coding_run_not_promotable",
            "task coding run is missing or no longer matches the immutable task",
        )

    base_version = db.get_project_version(task.project_version_id)
    if (
        base_version is None
        or base_version.project_name != task.project_name
        or base_version.git_commit.lower() != task.base_commit.lower()
    ):
        raise PromotionCandidateError(
            "base_version_missing", "base project version no longer exists"
        )

    expected_path = Path(
        local_result_dir(run.job_id, config.local_home_dir)
    ) / "changes.bundle"
    if run.bundle_path != str(expected_path):
        raise PromotionCandidateError(
            "bundle_path_mismatch", "coding run bundle path is not canonical"
        )

    artifact = next(
        (
            item
            for item in db.list_engineering_task_artifacts(
                task.id, attempt_number=run.attempt_number
            )
            if item.kind == "bundle"
            and item.coding_run_id == run.id
            and item.storage_kind == "local_result"
            and item.storage_key == "changes.bundle"
            and item.verification_status == "verified"
            and item.availability == "available"
        ),
        None,
    )
    if (
        artifact is None
        or not isinstance(artifact.source_sha256, str)
        or not isinstance(artifact.source_size_bytes, int)
    ):
        raise PromotionCandidateError(
            "bundle_not_verified",
            "promotion requires an available verified bundle artifact",
        )

    digest, size = _sha256_file(expected_path)
    if digest != artifact.source_sha256 or size != artifact.source_size_bytes:
        raise PromotionCandidateError(
            "bundle_artifact_mismatch",
            "bundle bytes no longer match collected artifact evidence",
        )

    return PromotionCandidate(
        task_id=task.id,
        project_name=task.project_name,
        base_project_version_id=task.project_version_id,
        base_commit=task.base_commit.lower(),
        result_commit=run.result_commit,
        bundle_path=str(expected_path),
        bundle_sha256=digest,
        bundle_size_bytes=size,
        coding_run_id=run.id,
        attempt_number=run.attempt_number,
    )


def promotion_staging_repo_path(approval_id: int, local_home_dir: str) -> str:
    base = Path(local_home_dir or ".").resolve()
    return str(base / "promotion_staging" / f"{approval_id}.git")


def promotion_hub_ref(version_id: str) -> str:
    return f"refs/heads/codex-promoted/{version_id}"


def _stderr_tail(result: Any) -> str:
    text = (
        getattr(result, "stderr", "")
        or getattr(result, "stdout", "")
        or ""
    ).strip()
    return text[-500:] or f"exit_status={getattr(result, 'exit_status', '?')}"


async def _run_checked(local_run, command: str, timeout: int, reason_code: str):
    result = await local_run(command, timeout)
    if getattr(result, "exit_status", None) != 0:
        raise PromotionPublishError(
            reason_code, f"{reason_code}: {_stderr_tail(result)}"
        )
    return result


async def verify_bundle_in_staging(
    *,
    candidate: PromotionCandidate,
    approval_id: int,
    local_home_dir: str,
    local_run,
) -> None:
    """Verify advertised heads and the exact result commit in an isolated repo."""

    staging = promotion_staging_repo_path(approval_id, local_home_dir)
    hub = hub_repo_path(candidate.project_name, local_home_dir)
    q_staging = shlex.quote(staging)
    q_hub = shlex.quote(hub)
    q_bundle = shlex.quote(candidate.bundle_path)
    q_base = shlex.quote(candidate.base_commit)
    q_result = shlex.quote(candidate.result_commit)

    await _run_checked(
        local_run,
        f"mkdir -p {shlex.quote(str(Path(staging).parent))} && "
        f"git init --bare {q_staging}",
        30,
        "promotion_staging_init_failed",
    )
    await _run_checked(
        local_run,
        f"git --git-dir={q_staging} fetch --force --no-tags {q_hub} "
        f"{q_base}:refs/promotion-base/approved",
        60,
        "promotion_base_fetch_failed",
    )
    await _run_checked(
        local_run,
        f"git --git-dir={q_staging} bundle verify {q_bundle}",
        30,
        "bundle_verify_failed",
    )
    heads = await _run_checked(
        local_run,
        f"git bundle list-heads {q_bundle}",
        30,
        "bundle_heads_failed",
    )
    advertised = {
        line.split()[0].lower()
        for line in (getattr(heads, "stdout", "") or "").splitlines()
        if line.split()
    }
    if candidate.result_commit not in advertised:
        raise PromotionPublishError(
            "bundle_commit_mismatch",
            "bundle does not advertise the reviewed result commit",
        )
    await _run_checked(
        local_run,
        f"git --git-dir={q_staging} fetch --force --no-tags {q_bundle} "
        "'+refs/heads/*:refs/promotion-verified/*'",
        60,
        "bundle_staging_fetch_failed",
    )
    await _run_checked(
        local_run,
        f"git --git-dir={q_staging} cat-file -e {q_result}^{{commit}}",
        15,
        "bundle_commit_missing",
    )


async def publish_bundle_to_hub(
    *,
    candidate: PromotionCandidate,
    approval_id: int,
    version_id: str,
    local_home_dir: str,
    local_run,
) -> str:
    """Import objects, then atomically publish one deterministic Hub ref."""

    hub = hub_repo_path(candidate.project_name, local_home_dir)
    ref = promotion_hub_ref(version_id)
    q_hub = shlex.quote(hub)
    q_bundle = shlex.quote(candidate.bundle_path)
    q_result = shlex.quote(candidate.result_commit)
    q_ref = shlex.quote(ref)

    await _run_checked(
        local_run,
        f"git --git-dir={q_hub} fetch --force --no-tags {q_bundle} "
        f"'+refs/heads/*:refs/promotion-imports/{approval_id}/*'",
        60,
        "hub_bundle_fetch_failed",
    )
    await _run_checked(
        local_run,
        f"git --git-dir={q_hub} cat-file -e {q_result}^{{commit}}",
        15,
        "hub_commit_missing",
    )
    await _run_checked(
        local_run,
        f"git --git-dir={q_hub} update-ref {q_ref} {q_result}",
        15,
        "hub_ref_publish_failed",
    )
    resolved = await _run_checked(
        local_run,
        f"git --git-dir={q_hub} rev-parse --verify {q_ref}^{{commit}}",
        15,
        "hub_ref_verify_failed",
    )
    if (getattr(resolved, "stdout", "") or "").strip().lower() != (
        candidate.result_commit
    ):
        raise PromotionPublishError(
            "hub_ref_mismatch", "published Hub ref does not match result commit"
        )
    return ref
