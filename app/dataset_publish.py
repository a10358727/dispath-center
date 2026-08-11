"""Canonical Product v2 Dataset publish preview and approval contracts.

The public contract never persists or returns an absolute Server A path.  A
local source is identified by the digest of one operator-configured root plus
its canonical relative path.  Run outputs are identified by immutable control
plane evidence and are always re-derived from ``LOCAL_HOME_DIR/results``.
"""

from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.dataset_assets import (
    DatasetAliasRevisionContract,
    DatasetAssetContract,
    DatasetAssetInput,
    build_dataset_alias_revision,
    build_dataset_asset_contract,
    canonical_snapshot_id,
)
from app.dataset_snapshot import (
    DEFAULT_MAX_BYTES,
    DEFAULT_SHARD_POLICY,
    STORE_REVISION,
    CandidateManifest,
    LocalArtifactStore,
    SnapshotRefused,
    SnapshotVerificationUnknown,
    build_and_publish,
    build_candidate_manifest,
)
from app.execution_contract import canonical_json, utf8_sha256
from app.project_bootstrap import OutputDeclaration, canonical_uuid


DATASET_PUBLISH_PREVIEW_CONTRACT_VERSION = "dataset-publish-preview-v2"
DATASET_PUBLISH_CONTRACT_VERSION = "dataset-publish-v2"
DATASET_PUBLISH_APPROVAL_KIND = "dataset_publish_v2"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_PATH_BYTES = 4096
_MAX_OUTPUT_SCAN_ENTRIES = 100_000
_BROAD_LOCAL_ROOTS = frozenset(
    {"/", "/etc", "/home", "/opt", "/root", "/tmp", "/usr", "/var"}
)


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _canonical_relative_path(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or _CONTROL_RE.search(value)
        or len(value.encode("utf-8")) > _MAX_PATH_BYTES
        or "\\" in value
    ):
        raise ValueError(f"{field_name} is invalid")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"{field_name} must be a canonical relative path")
    normalized = candidate.as_posix()
    if normalized != value:
        raise ValueError(f"{field_name} must be canonical")
    return normalized


def _canonical_absolute_input_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or _CONTROL_RE.search(value)
        or len(value.encode("utf-8")) > _MAX_PATH_BYTES
        or not os.path.isabs(value)
        or os.path.normpath(value) != value
        or any(part == ".." for part in PurePosixPath(value).parts)
    ):
        raise ValueError("local source path must be canonical absolute text")
    return value


class LocalPathPublishSource(_StrictContract):
    kind: Literal["local_path"] = "local_path"
    path: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _canonical_absolute_input_path(value)


class RunOutputPublishSource(_StrictContract):
    kind: Literal["run_output"] = "run_output"
    plan_id: str
    output_declaration_name: str

    @field_validator("plan_id")
    @classmethod
    def _plan_id(cls, value: str) -> str:
        return canonical_uuid(value, "plan_id")

    @field_validator("output_declaration_name")
    @classmethod
    def _declaration_name(cls, value: str) -> str:
        if (
            not isinstance(value, str)
            or value != value.strip()
            or not value
            or _CONTROL_RE.search(value)
            or len(value.encode("utf-8")) > 64
        ):
            raise ValueError("output_declaration_name is invalid")
        return value


DatasetPublishSource = Annotated[
    Union[LocalPathPublishSource, RunOutputPublishSource],
    Field(discriminator="kind"),
]


class DatasetPublishPreviewRequest(_StrictContract):
    source: DatasetPublishSource
    asset: DatasetAssetInput
    initial_alias: str | None = None

    @field_validator("initial_alias")
    @classmethod
    def _alias(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Reuse the complete alias validator without inventing a second regex.
        return build_dataset_alias_revision(
            revision_id=str(uuid.uuid4()),
            project_id=str(uuid.uuid4()),
            asset_id=str(uuid.uuid4()),
            alias_name=value,
            revision=1,
            snapshot_id="preview",
        ).alias_name


class DatasetPublishRequest(DatasetPublishPreviewRequest):
    expected_preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class LocalPathSourceIdentity(_StrictContract):
    kind: Literal["local_path"] = "local_path"
    root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    entry_kind: Literal["file", "directory"]

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        if value == ".":
            return value
        return _canonical_relative_path(value, "relative_path")


class RunOutputSourceIdentity(_StrictContract):
    kind: Literal["run_output"] = "run_output"
    plan_id: str
    job_id: int = Field(ge=1, le=9_223_372_036_854_775_807)
    attempt_id: str
    completion_operation_id: str
    completion_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_profile_id: str
    run_profile_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_declaration: OutputDeclaration
    relative_path: str
    input_asset_id: str
    input_snapshot_id: str

    @field_validator(
        "plan_id",
        "attempt_id",
        "completion_operation_id",
        "run_profile_id",
        "input_asset_id",
    )
    @classmethod
    def _uuid_fields(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return _canonical_relative_path(value, "relative_path")

    @field_validator("input_snapshot_id")
    @classmethod
    def _snapshot_id(cls, value: str) -> str:
        return canonical_snapshot_id(value, "input_snapshot_id")


DatasetPublishSourceIdentity = Annotated[
    Union[LocalPathSourceIdentity, RunOutputSourceIdentity],
    Field(discriminator="kind"),
]


class DatasetPublishPreview(_StrictContract):
    contract_version: Literal["dataset-publish-preview-v2"] = (
        "dataset-publish-preview-v2"
    )
    project_id: str
    source: DatasetPublishSourceIdentity
    source_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset: DatasetAssetInput
    initial_alias: str | None = None
    source_candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=0, le=9_223_372_036_854_775_807)
    total_bytes: int = Field(ge=0, le=9_223_372_036_854_775_807)
    store_revision: Literal["local-artifact-store-v1"] = "local-artifact-store-v1"
    shard_policy: dict[str, int]
    max_bytes: int = Field(ge=1, le=9_223_372_036_854_775_807)
    expected_snapshot_identity: str
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("project_id")
    @classmethod
    def _project_id(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")

    @field_validator("expected_snapshot_identity")
    @classmethod
    def _snapshot_identity(cls, value: str) -> str:
        if not isinstance(value, str) or not value.startswith("sha256:"):
            raise ValueError("expected_snapshot_identity is invalid")
        if not _SHA256_RE.fullmatch(value.removeprefix("sha256:")):
            raise ValueError("expected_snapshot_identity is invalid")
        return value

    @model_validator(mode="after")
    def _digests(self) -> "DatasetPublishPreview":
        source_body = self.source.model_dump(mode="json")
        if utf8_sha256(canonical_json(source_body)) != self.source_identity_digest:
            raise ValueError("dataset publish source identity digest mismatch")
        body = self.model_dump(mode="json", exclude={"preview_digest"})
        if utf8_sha256(canonical_json(body)) != self.preview_digest:
            raise ValueError("dataset publish preview digest mismatch")
        if self.expected_snapshot_identity != f"sha256:{self.manifest_digest}":
            raise ValueError("dataset publish expected snapshot identity mismatch")
        if set(self.shard_policy) != {"max_shard_bytes", "max_shard_files"}:
            raise ValueError("dataset publish shard policy is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in self.shard_policy.values()
        ):
            raise ValueError("dataset publish shard policy is invalid")
        return self


class DatasetPublishLineageContract(_StrictContract):
    edge_id: str
    input_asset_id: str
    input_snapshot_id: str
    output_asset_id: str
    output_snapshot_id: str
    producing_execution_plan_id: str
    output_declaration_name: str

    @field_validator(
        "edge_id",
        "input_asset_id",
        "output_asset_id",
        "producing_execution_plan_id",
    )
    @classmethod
    def _uuid_fields(cls, value: str, info: Any) -> str:
        return canonical_uuid(value, info.field_name)

    @field_validator("input_snapshot_id", "output_snapshot_id")
    @classmethod
    def _snapshot_fields(cls, value: str, info: Any) -> str:
        return canonical_snapshot_id(value, info.field_name)

    @field_validator("output_declaration_name")
    @classmethod
    def _output_name(cls, value: str) -> str:
        return RunOutputPublishSource(
            plan_id=str(uuid.uuid4()),
            output_declaration_name=value,
        ).output_declaration_name


class DatasetPublishPayload(_StrictContract):
    contract_version: Literal["dataset-publish-v2"] = "dataset-publish-v2"
    project_id: str
    preview: DatasetPublishPreview
    snapshot_id: str
    target_asset: DatasetAssetContract
    target_alias: DatasetAliasRevisionContract | None = None
    lineage: DatasetPublishLineageContract | None = None

    @field_validator("project_id")
    @classmethod
    def _project_id(cls, value: str) -> str:
        return canonical_uuid(value, "project_id")

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id(cls, value: str) -> str:
        return canonical_snapshot_id(value)

    @model_validator(mode="after")
    def _references(self) -> "DatasetPublishPayload":
        if self.preview.project_id != self.project_id:
            raise ValueError("dataset publish preview project mismatch")
        if self.target_asset.owning_project_id != self.project_id:
            raise ValueError("dataset publish asset project mismatch")
        if self.target_alias is not None and (
            self.target_alias.project_id != self.project_id
            or self.target_alias.asset_id != self.target_asset.asset_id
            or self.target_alias.snapshot_id != self.snapshot_id
            or self.target_alias.revision != 1
            or self.target_alias.alias_name != self.preview.initial_alias
        ):
            raise ValueError("dataset publish initial alias mismatch")
        if (self.target_alias is None) != (self.preview.initial_alias is None):
            raise ValueError("dataset publish initial alias presence mismatch")
        if self.preview.source.kind == "run_output":
            source = self.preview.source
            if self.lineage is None or (
                self.lineage.input_asset_id != source.input_asset_id
                or self.lineage.input_snapshot_id != source.input_snapshot_id
                or self.lineage.output_asset_id != self.target_asset.asset_id
                or self.lineage.output_snapshot_id != self.snapshot_id
                or self.lineage.producing_execution_plan_id != source.plan_id
                or self.lineage.output_declaration_name
                != source.output_declaration.name
            ):
                raise ValueError("dataset publish lineage mismatch")
        elif self.lineage is not None:
            raise ValueError("local dataset publish must not fabricate lineage")
        return self


def dataset_publish_payload_digest(payload: DatasetPublishPayload) -> str:
    return utf8_sha256(canonical_json(payload.model_dump(mode="json")))


def parse_dataset_publish_payload(value: object) -> DatasetPublishPayload:
    return DatasetPublishPayload.model_validate(value)


def _contains(parent: str, child: str) -> bool:
    try:
        return os.path.commonpath((parent, child)) == parent
    except ValueError:
        return False


def _path_has_symlink(path: str) -> bool:
    absolute = os.path.abspath(path)
    current = os.path.sep
    for component in PurePosixPath(absolute).parts[1:]:
        current = os.path.join(current, component)
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except OSError:
            return False
    return False


def _safe_local_roots(config: Any) -> tuple[tuple[str, str], ...]:
    roots = getattr(config, "dataset_publish_local_roots", ()) or ()
    resolved: list[tuple[str, str]] = []
    for configured in roots:
        if not isinstance(configured, str) or not configured or not os.path.isabs(configured):
            continue
        root = os.path.realpath(configured)
        try:
            mode = os.lstat(configured).st_mode
        except OSError:
            continue
        if (
            not stat.S_ISDIR(mode)
            or stat.S_ISLNK(mode)
            or _path_has_symlink(configured)
            # Explicit subdirectories under conventional top-level paths are
            # valid operator-owned allowlist roots (and make isolated test /
            # ephemeral deployments possible).  Only the broad directories
            # themselves are too powerful to accept.
            or root in _BROAD_LOCAL_ROOTS
        ):
            continue
        resolved.append((root, utf8_sha256(root)))
    return tuple(sorted(set(resolved)))


def _store_root(config: Any) -> str:
    root = getattr(config, "dataset_snapshot_store_root", "dataset_store")
    if not isinstance(root, str) or not root.strip():
        raise ValueError("dataset publish store root is invalid")
    if not os.path.isabs(root):
        local_home = getattr(config, "local_home_dir", None)
        root = os.path.join(local_home or os.getcwd(), root)
    return os.path.realpath(root)


def _require_source_store_separation(source_path: str, config: Any) -> None:
    store = _store_root(config)
    if _contains(source_path, store) or _contains(store, source_path):
        raise ValueError("dataset publish source and store roots overlap")


def resolve_local_source(
    source: LocalPathPublishSource,
    *,
    config: Any,
) -> tuple[LocalPathSourceIdentity, str]:
    roots = _safe_local_roots(config)
    if not roots:
        raise ValueError("dataset publish local source roots are unavailable")
    if _path_has_symlink(source.path):
        raise ValueError("dataset publish local source contains a symlink traversal")
    resolved_source = os.path.realpath(source.path)
    matches = [item for item in roots if _contains(item[0], resolved_source)]
    if len(matches) != 1:
        raise ValueError("dataset publish local source is outside the allowlist")
    root, root_sha256 = matches[0]
    _require_source_store_separation(resolved_source, config)
    try:
        mode = os.lstat(resolved_source).st_mode
    except OSError as exc:
        raise ValueError("dataset publish source is unavailable") from exc
    if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ValueError("dataset publish source must be a regular file or directory")
    relative_path = os.path.relpath(resolved_source, root).replace(os.sep, "/")
    if relative_path != ".":
        relative_path = _canonical_relative_path(relative_path, "relative_path")
    identity = LocalPathSourceIdentity(
        root_sha256=root_sha256,
        relative_path=relative_path,
        entry_kind="file" if stat.S_ISREG(mode) else "directory",
    )
    return identity, resolved_source


def materialize_local_source_path(identity: LocalPathSourceIdentity, *, config: Any) -> str:
    roots = [root for root, digest in _safe_local_roots(config) if digest == identity.root_sha256]
    if len(roots) != 1:
        raise ValueError("dataset publish local source root identity is unavailable")
    root = roots[0]
    candidate = root if identity.relative_path == "." else os.path.join(
        root, *identity.relative_path.split("/")
    )
    resolved = os.path.realpath(candidate)
    if not _contains(root, resolved) or _path_has_symlink(candidate):
        raise ValueError("dataset publish local source containment changed")
    _require_source_store_separation(resolved, config)
    try:
        mode = os.lstat(resolved).st_mode
    except OSError as exc:
        raise ValueError("dataset publish source is unavailable") from exc
    actual_kind = "file" if stat.S_ISREG(mode) else "directory" if stat.S_ISDIR(mode) else None
    if actual_kind != identity.entry_kind:
        raise ValueError("dataset publish local source kind changed")
    return resolved


def _output_matches(pattern: str, relative_path: str) -> bool:
    # ``PurePath.match`` keeps ``*`` within one path segment and supports the
    # reviewed glob syntax already accepted by OutputDeclaration.
    return PurePosixPath(relative_path).match(pattern)


def _secure_output_matches(
    result_root: str,
    declaration: OutputDeclaration,
) -> list[str]:
    try:
        root_before = os.lstat(result_root)
    except OSError as exc:
        raise ValueError("run result collection directory is unavailable") from exc
    if not stat.S_ISDIR(root_before.st_mode) or stat.S_ISLNK(root_before.st_mode):
        raise ValueError("run result collection directory is invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        root_fd = os.open(result_root, directory_flags)
    except OSError as exc:
        raise ValueError("run result collection directory is unavailable") from exc
    matches: list[str] = []
    visited = 0

    def walk(directory_fd: int, prefix: str) -> None:
        nonlocal visited
        before = os.fstat(directory_fd)
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise ValueError("run result collection directory is unreadable") from exc
        for name in names:
            visited += 1
            if visited > _MAX_OUTPUT_SCAN_ENTRIES:
                raise ValueError("run output declaration scan exceeded its bound")
            if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
                raise ValueError("run output contains an invalid path component")
            relative = f"{prefix}/{name}" if prefix else name
            try:
                entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise ValueError("run output changed during declaration resolution") from exc
            if stat.S_ISLNK(entry.st_mode):
                if _output_matches(declaration.path_pattern, relative):
                    raise ValueError("run output declaration resolves through a symlink")
                continue
            if stat.S_ISDIR(entry.st_mode):
                if declaration.kind == "directory" and _output_matches(
                    declaration.path_pattern, relative
                ):
                    matches.append(relative)
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise ValueError("run output changed during declaration resolution") from exc
                try:
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(entry.st_mode):
                if declaration.kind == "file" and _output_matches(
                    declaration.path_pattern, relative
                ):
                    matches.append(relative)
            elif _output_matches(declaration.path_pattern, relative):
                raise ValueError("run output declaration resolves to a special file")
        if (
            os.fstat(directory_fd).st_mtime_ns != before.st_mtime_ns
            or os.fstat(directory_fd).st_ctime_ns != before.st_ctime_ns
        ):
            raise ValueError("run output changed during declaration resolution")

    try:
        if os.fstat(root_fd).st_ino != root_before.st_ino:
            raise ValueError("run result collection directory changed")
        walk(root_fd, "")
    finally:
        os.close(root_fd)
    return matches


def resolve_run_output_source(
    source: RunOutputPublishSource,
    *,
    project_id: str,
    database: Any,
    config: Any,
) -> tuple[RunOutputSourceIdentity, str]:
    evidence = database.get_dataset_publish_run_output_evidence(
        project_id=project_id,
        plan_id=source.plan_id,
        output_declaration_name=source.output_declaration_name,
    )
    if evidence is None:
        raise LookupError("run output source is unavailable")
    state = evidence.get("state")
    if state != "eligible":
        raise ValueError(str(state or "run_output_evidence_incomplete"))
    declaration = OutputDeclaration.model_validate(evidence["output_declaration"])
    local_home = getattr(config, "local_home_dir", ".")
    collected_results_root = os.path.realpath(
        os.path.join(local_home or ".", "results")
    )
    raw_result_root = os.path.join(
        local_home or ".", "results", str(evidence["job_id"])
    )
    result_root = os.path.realpath(raw_result_root)
    if (
        not _contains(collected_results_root, result_root)
        or _path_has_symlink(raw_result_root)
    ):
        raise ValueError("run result collection directory escapes its managed root")
    matches = _secure_output_matches(result_root, declaration)
    if len(matches) != 1:
        raise ValueError("run output declaration must resolve exactly one result")
    relative_path = _canonical_relative_path(matches[0], "relative_path")
    source_path = os.path.realpath(
        os.path.join(result_root, *relative_path.split("/"))
    )
    if not _contains(result_root, source_path):
        raise ValueError("run output path escapes the collected result directory")
    _require_source_store_separation(source_path, config)
    identity = RunOutputSourceIdentity(
        plan_id=source.plan_id,
        job_id=evidence["job_id"],
        attempt_id=evidence["attempt_id"],
        completion_operation_id=evidence["completion_operation_id"],
        completion_evidence_sha256=evidence["completion_evidence_sha256"],
        run_profile_id=evidence["run_profile_id"],
        run_profile_spec_digest=evidence["run_profile_spec_digest"],
        output_declaration=declaration,
        relative_path=relative_path,
        input_asset_id=evidence["input_asset_id"],
        input_snapshot_id=evidence["input_snapshot_id"],
    )
    return identity, source_path


def materialize_run_output_source_path(
    identity: RunOutputSourceIdentity,
    *,
    project_id: str,
    database: Any,
    config: Any,
) -> str:
    source = RunOutputPublishSource(
        plan_id=identity.plan_id,
        output_declaration_name=identity.output_declaration.name,
    )
    current, source_path = resolve_run_output_source(
        source,
        project_id=project_id,
        database=database,
        config=config,
    )
    if current != identity:
        raise ValueError("run output source evidence changed")
    return source_path


def resolve_publish_source(
    source: DatasetPublishSource,
    *,
    project_id: str,
    database: Any,
    config: Any,
) -> tuple[LocalPathSourceIdentity | RunOutputSourceIdentity, str]:
    if isinstance(source, LocalPathPublishSource):
        return resolve_local_source(source, config=config)
    return resolve_run_output_source(
        source,
        project_id=project_id,
        database=database,
        config=config,
    )


def materialize_publish_source_path(
    source: LocalPathSourceIdentity | RunOutputSourceIdentity,
    *,
    project_id: str,
    database: Any,
    config: Any,
) -> str:
    if isinstance(source, LocalPathSourceIdentity):
        return materialize_local_source_path(source, config=config)
    return materialize_run_output_source_path(
        source,
        project_id=project_id,
        database=database,
        config=config,
    )


def build_dataset_publish_preview(
    request: DatasetPublishPreviewRequest,
    *,
    project_id: str,
    database: Any,
    config: Any,
) -> tuple[DatasetPublishPreview, str, CandidateManifest]:
    project_id = canonical_uuid(project_id, "project_id")
    request = DatasetPublishPreviewRequest.model_validate(request)
    source, source_path = resolve_publish_source(
        request.source,
        project_id=project_id,
        database=database,
        config=config,
    )
    max_bytes = getattr(config, "dataset_snapshot_max_bytes", DEFAULT_MAX_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("dataset publish max bytes is invalid")
    try:
        candidate = build_candidate_manifest(
            source_path,
            max_bytes=max_bytes,
            reject_symlinks=True,
        )
    except (SnapshotVerificationUnknown, SnapshotRefused) as exc:
        raise ValueError(str(exc)) from exc
    policy = dict(getattr(config, "dataset_snapshot_shard_policy", DEFAULT_SHARD_POLICY))
    body = {
        "contract_version": DATASET_PUBLISH_PREVIEW_CONTRACT_VERSION,
        "project_id": project_id,
        "source": source.model_dump(mode="json"),
        "source_identity_digest": utf8_sha256(
            canonical_json(source.model_dump(mode="json"))
        ),
        "asset": request.asset.model_dump(mode="json"),
        "initial_alias": request.initial_alias,
        "source_candidate_digest": candidate.source_candidate_digest,
        "manifest_digest": candidate.manifest_digest,
        "file_count": candidate.file_count,
        "total_bytes": candidate.total_bytes,
        "store_revision": STORE_REVISION,
        "shard_policy": policy,
        "max_bytes": max_bytes,
        "expected_snapshot_identity": f"sha256:{candidate.manifest_digest}",
    }
    body["preview_digest"] = utf8_sha256(canonical_json(body))
    return DatasetPublishPreview.model_validate(body), source_path, candidate


def build_dataset_publish_payload(
    preview: DatasetPublishPreview,
) -> DatasetPublishPayload:
    preview = DatasetPublishPreview.model_validate(preview)
    snapshot_id = str(uuid.uuid4())
    target_asset = build_dataset_asset_contract(
        preview.asset,
        asset_id=str(uuid.uuid4()),
        owning_project_id=preview.project_id,
    )
    target_alias = (
        build_dataset_alias_revision(
            revision_id=str(uuid.uuid4()),
            project_id=preview.project_id,
            asset_id=target_asset.asset_id,
            alias_name=preview.initial_alias,
            revision=1,
            snapshot_id=snapshot_id,
        )
        if preview.initial_alias is not None
        else None
    )
    lineage = None
    if isinstance(preview.source, RunOutputSourceIdentity):
        lineage = DatasetPublishLineageContract(
            edge_id=str(uuid.uuid4()),
            input_asset_id=preview.source.input_asset_id,
            input_snapshot_id=preview.source.input_snapshot_id,
            output_asset_id=target_asset.asset_id,
            output_snapshot_id=snapshot_id,
            producing_execution_plan_id=preview.source.plan_id,
            output_declaration_name=preview.source.output_declaration.name,
        )
    return DatasetPublishPayload(
        project_id=preview.project_id,
        preview=preview,
        snapshot_id=snapshot_id,
        target_asset=target_asset,
        target_alias=target_alias,
        lineage=lineage,
    )


def dataset_publish_store(config: Any) -> LocalArtifactStore:
    """Return the existing local content-addressed snapshot store."""

    return LocalArtifactStore(_store_root(config))


def revalidate_dataset_publish_source(
    payload: DatasetPublishPayload,
    *,
    database: Any,
    config: Any,
) -> tuple[str, CandidateManifest]:
    """Re-materialize and rescan an approved path without exposing it."""

    payload = DatasetPublishPayload.model_validate(payload)
    source_path = materialize_publish_source_path(
        payload.preview.source,
        project_id=payload.project_id,
        database=database,
        config=config,
    )
    try:
        candidate = build_candidate_manifest(
            source_path,
            max_bytes=payload.preview.max_bytes,
            reject_symlinks=True,
        )
    except (SnapshotVerificationUnknown, SnapshotRefused) as exc:
        raise ValueError("dataset publish source could not be verified") from exc
    if (
        candidate.source_candidate_digest
        != payload.preview.source_candidate_digest
        or candidate.manifest_digest != payload.preview.manifest_digest
        or candidate.file_count != payload.preview.file_count
        or candidate.total_bytes != payload.preview.total_bytes
    ):
        raise ValueError("dataset publish source drifted")
    return source_path, candidate


def execute_dataset_publish_build(
    database: Any,
    *,
    approval_id: int,
    config: Any,
) -> dict[str, Any]:
    """Build/resume one approved Product v2 publish using its pinned IDs.

    Content-addressed filesystem publication may complete before the SQLite
    finalization transaction.  A filesystem or finalization interruption
    therefore intentionally leaves the exact snapshot ``building`` so the
    same approval and IDs can be resumed without inventing a second contract.
    """

    approval = database.get_verified_product_approval(approval_id)
    if (
        approval is None
        or approval.kind != DATASET_PUBLISH_APPROVAL_KIND
        or approval.status != "approved"
        or not isinstance(approval.payload, dict)
    ):
        raise ValueError("dataset publish approval evidence is unavailable")
    payload = parse_dataset_publish_payload(approval.payload)
    materialized = database.get_dataset_publish_materialization(approval_id)
    if materialized is None:
        raise ValueError("dataset publish building reservation is unavailable")
    if materialized.get("state") == "published":
        return materialized
    if materialized.get("state") != "building":
        raise ValueError("dataset publish snapshot is not resumable")

    source_path, _candidate = revalidate_dataset_publish_source(
        payload,
        database=database,
        config=config,
    )
    try:
        store = dataset_publish_store(config)
    except OSError:
        return {
            **materialized,
            "resume_required": True,
            "build_error": "artifact_store_interrupted",
        }
    if store.preflight() != "eligible":
        return {
            **materialized,
            "resume_required": True,
            "build_error": "artifact_store_unavailable",
        }
    try:
        result = build_and_publish(
            store,
            snapshot_id=payload.snapshot_id,
            source_path=source_path,
            approved_candidate_digest=payload.preview.source_candidate_digest,
            shard_policy=payload.preview.shard_policy,
            max_bytes=payload.preview.max_bytes,
            reject_symlinks=True,
        )
    except (OSError, SnapshotRefused, SnapshotVerificationUnknown):
        return {
            **materialized,
            "resume_required": True,
            "build_error": "artifact_store_interrupted",
        }
    if result.state == "published":
        if (
            result.manifest_digest != payload.preview.manifest_digest
            or result.file_count != payload.preview.file_count
            or result.total_bytes != payload.preview.total_bytes
        ):
            raise ValueError("dataset publish builder result drifted")
        return database.complete_dataset_publish_v2(
            approval_id=approval_id,
            snapshot_id=payload.snapshot_id,
            manifest_digest=result.manifest_digest or "",
            manifest_path=result.manifest_path or "",
            descriptor_path=result.descriptor_path or "",
            file_count=result.file_count or 0,
            total_bytes=result.total_bytes or 0,
            shards=[
                {
                    "index": shard.index,
                    "sha256": shard.sha256,
                    "size": shard.size,
                    "file_count": shard.file_count,
                }
                for shard in result.shards
            ],
        )
    failure_state = (
        "verification_unknown"
        if result.state == "verification_unknown"
        else "aborted"
    )
    failed = database.fail_dataset_snapshot_build(
        snapshot_id=payload.snapshot_id,
        state=failure_state,
        last_error_category=(
            "source_verification_unknown"
            if failure_state == "verification_unknown"
            else "snapshot_build_refused"
        ),
        # Builder reasons are fixed internal strings in this workflow; never
        # include a caught OSError because it may contain an absolute path.
        sanitized_error_detail=result.reason,
    )
    return {
        "approval_id": approval_id,
        "project_id": payload.project_id,
        "asset_id": payload.target_asset.asset_id,
        "snapshot_id": payload.snapshot_id,
        "state": failed.state,
        "payload_digest": approval.payload_sha256,
    }


def resume_dataset_publish(
    database: Any,
    *,
    snapshot_id: str,
    config: Any,
) -> dict[str, Any]:
    """Resume only the dedicated v2 approval/building reservation."""

    snapshot_id = canonical_snapshot_id(snapshot_id)
    snapshot = database.get_dataset_snapshot(snapshot_id)
    if (
        snapshot is None
        or snapshot.state != "building"
        or snapshot.build_approval_id is None
    ):
        raise ValueError("dataset publish snapshot is not resumable")
    approval = database.get_verified_product_approval(snapshot.build_approval_id)
    if (
        approval is None
        or approval.kind != DATASET_PUBLISH_APPROVAL_KIND
        or approval.status != "approved"
        or not isinstance(approval.payload, dict)
    ):
        raise ValueError("dataset publish approval evidence is unavailable")
    payload = parse_dataset_publish_payload(approval.payload)
    if payload.snapshot_id != snapshot_id:
        raise ValueError("dataset publish snapshot binding drifted")
    return execute_dataset_publish_build(
        database,
        approval_id=approval.id,
        config=config,
    )


__all__ = [
    "DATASET_PUBLISH_APPROVAL_KIND",
    "DATASET_PUBLISH_CONTRACT_VERSION",
    "DATASET_PUBLISH_PREVIEW_CONTRACT_VERSION",
    "DatasetPublishPayload",
    "DatasetPublishPreview",
    "DatasetPublishPreviewRequest",
    "DatasetPublishRequest",
    "LocalPathSourceIdentity",
    "RunOutputSourceIdentity",
    "build_dataset_publish_payload",
    "build_dataset_publish_preview",
    "dataset_publish_store",
    "dataset_publish_payload_digest",
    "execute_dataset_publish_build",
    "materialize_publish_source_path",
    "parse_dataset_publish_payload",
    "revalidate_dataset_publish_source",
    "resume_dataset_publish",
]
