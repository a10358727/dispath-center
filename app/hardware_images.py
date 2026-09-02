"""Hardware image registration (DG-HARDWARE-EXECUTION v1 H-3, P2).

A ``build`` run declares which of its outputs are programmable images
(``OutputDeclaration.artifact_class`` ∈ {bitstream, firmware}). After the
job-finish hook has pulled ``results/{job_id}/`` back to Server A, this
module hashes each declared image, copies it content-addressed into
``{local_home_dir}/images/{sha256}`` (immutable, de-duplicated) and records
one ``hardware_images`` row per new digest. A later ``program`` action (P3)
pins ``image_sha256`` and Server A pushes that exact file to the worker; the
image is never fetched from a workspace, GitHub or the worker itself.

Like metrics-v1 this step is purely additive and local: it reads bytes that
already arrived through the existing rsync pull, issues no remote command,
and never influences ``job.status`` (INV-SSH-4/6 unchanged). Every failure
mode is absorbed and reported through the audit log only.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from app.audit import SYSTEM_AUDIT_ACTOR, append_audit, now_iso
from app.results import local_result_dir

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig
    from app.db import Database, Job

logger = logging.getLogger(__name__)

IMAGE_STORE_DIRNAME = "images"
#: A glob in one declaration may not fan out into an unbounded registration.
MAX_MATCHES_PER_DECLARATION = 16
_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class RegisteredImage:
    image_id: str
    sha256: str
    size_bytes: int
    kind: str
    output_name: str
    relative_path: str
    already_registered: bool


def image_store_path(local_home_dir: str, sha256: str) -> Path:
    return Path(local_home_dir) / IMAGE_STORE_DIRNAME / sha256


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _declared_files(result_dir: Path, path_pattern: str) -> list[Path]:
    """Regular files under ``result_dir`` matching one declared pattern.

    Symlinks and anything resolving outside the result directory are
    ignored (the pattern is validated as relative and glob-safe by the
    template contract, this is defence in depth on already-local bytes).
    """

    root = result_dir.resolve()
    matches: list[Path] = []
    for candidate in sorted(result_dir.glob(path_pattern)):
        if candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if root not in resolved.parents or not stat.S_ISREG(resolved.lstat().st_mode):
            continue
        matches.append(candidate)
    return matches


def _store_image(source: Path, destination: Path) -> bool:
    """Copy ``source`` to the content-addressed ``destination``; True if new."""

    if destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".incoming-", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as target, open(source, "rb") as origin:
            for chunk in iter(lambda: origin.read(_HASH_CHUNK), b""):
                target.write(chunk)
        os.chmod(tmp_name, 0o444)
        os.replace(tmp_name, destination)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True


def _audit_failure(audit_path: str, job_id: int, *, output_name: str, reason: str, **extra: object) -> None:
    append_audit(
        "hardware_image_registration_failed",
        {"job_id": job_id, "output_name": output_name, "reason": reason, **extra},
        result="failed",
        path=audit_path,
        actor=SYSTEM_AUDIT_ACTOR,
    )


def register_declared_images(
    *,
    db: "Database",
    job_id: int,
    project_id: str,
    project_version_id: str,
    build_plan_id: str,
    target_device_kind: Optional[str],
    declarations: Iterable[dict],
    local_home_dir: str,
    max_bytes: int,
    audit_path: str,
) -> list[RegisteredImage]:
    """Register every declared image of one finished build job.

    ``declarations`` are the template's ``output_declarations`` dumps; only
    ``kind == "file"`` entries carrying ``artifact_class`` are considered.
    Returns the registered (or already known) images; failures are audited.
    """

    result_dir = Path(local_result_dir(job_id, local_home_dir))
    registered: list[RegisteredImage] = []
    for declaration in declarations:
        artifact_class = declaration.get("artifact_class")
        output_name = str(declaration.get("name") or "")
        if declaration.get("kind") != "file" or artifact_class not in {"bitstream", "firmware"}:
            continue
        pattern = str(declaration.get("path_pattern") or "")
        matches = _declared_files(result_dir, pattern) if result_dir.is_dir() else []
        if not matches:
            if declaration.get("required", True):
                _audit_failure(audit_path, job_id, output_name=output_name, reason="missing")
            continue
        if len(matches) > MAX_MATCHES_PER_DECLARATION:
            _audit_failure(
                audit_path, job_id, output_name=output_name, reason="too_many_matches",
                match_count=len(matches),
            )
            continue
        for path in matches:
            relative_path = path.relative_to(result_dir).as_posix()
            size_bytes = path.lstat().st_size
            if size_bytes > max_bytes:
                _audit_failure(
                    audit_path, job_id, output_name=output_name, reason="oversize",
                    relative_path=relative_path, size_bytes=size_bytes, max_bytes=max_bytes,
                )
                continue
            sha256 = _sha256_of_file(path)
            _store_image(path, image_store_path(local_home_dir, sha256))
            existing = db.get_hardware_image_by_sha256(sha256)
            if existing is not None:
                registered.append(
                    RegisteredImage(
                        image_id=str(existing["id"]), sha256=sha256, size_bytes=size_bytes,
                        kind=str(existing["kind"]), output_name=output_name,
                        relative_path=relative_path, already_registered=True,
                    )
                )
                append_audit(
                    "hardware_image_registered",
                    {
                        "job_id": job_id, "image_id": str(existing["id"]), "sha256": sha256,
                        "output_name": output_name, "already_registered": True,
                    },
                    path=audit_path,
                    actor=SYSTEM_AUDIT_ACTOR,
                )
                continue
            image_id = str(uuid.uuid4())
            db.insert_hardware_image(
                image_id=image_id,
                project_id=project_id,
                project_version_id=project_version_id,
                build_plan_id=build_plan_id,
                job_id=job_id,
                kind=str(artifact_class),
                output_name=output_name,
                relative_path=relative_path,
                sha256=sha256,
                size_bytes=size_bytes,
                target_device_kind=target_device_kind,
                registered_at=now_iso(),
            )
            registered.append(
                RegisteredImage(
                    image_id=image_id, sha256=sha256, size_bytes=size_bytes, kind=str(artifact_class),
                    output_name=output_name, relative_path=relative_path, already_registered=False,
                )
            )
            append_audit(
                "hardware_image_registered",
                {
                    "job_id": job_id, "image_id": image_id, "sha256": sha256,
                    "size_bytes": size_bytes, "kind": artifact_class, "output_name": output_name,
                    "already_registered": False,
                },
                path=audit_path,
                actor=SYSTEM_AUDIT_ACTOR,
            )
    return registered


def register_build_images(
    job: "Job",
    *,
    db: Optional["Database"],
    config: "AppConfig",
    audit_path: str,
) -> None:
    """Job-finish hook: register the images a finished ``build`` run declared.

    Resolves the job's ExecutionPlan v2 → typed Run Template; anything that is
    not a typed ``build`` template is a no-op. Never raises.
    """

    if db is None:
        return
    try:
        resolved = db.build_output_declarations_for_job(job.id)
        if resolved is None:
            return
        register_declared_images(
            db=db,
            job_id=job.id,
            project_id=str(resolved["project_id"]),
            project_version_id=str(resolved["project_version_id"]),
            build_plan_id=str(resolved["plan_id"]),
            target_device_kind=resolved.get("target_device_kind"),
            declarations=resolved["declarations"],
            local_home_dir=config.local_home_dir,
            max_bytes=int(config.hardware_image_max_bytes),
            audit_path=audit_path,
        )
    except Exception as exc:  # noqa: BLE001 - never let registration affect finalization
        logger.warning(
            "hardware image registration failed for job %s (%s)", job.id, type(exc).__name__
        )
