"""Revision-scoped filesystem preflight for attempt-driven SSH.

DG-AMBIGUOUS-LAUNCH-v1 D-5 requires the remote ``agent_jobs`` directory to
live on a local filesystem.  The new launcher relies on an atomic ``mkdir``
claim; treating an NFS/CIFS/FUSE mount as equivalent would reopen the exact
duplicate-launch window the attempt backend exists to close.

The command in this module is fixed and read-only.  It contains no server or
user input, creates no directory, and reports only the filesystem type.  An
unrecognised or malformed observation remains ``unknown`` and therefore is
not eligible for attempt-driven SSH.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION = "attempt-fs-preflight-v1"
ATTEMPT_FILESYSTEM_PREFLIGHT_COMMAND = (
    "set -eu; p=agent_jobs; [ -e \"$p\" ] || p=.; "
    "LC_ALL=C stat -f -c 'DISPATCH_FS_TYPE=%T' -- \"$p\""
)

# ``stat -f -c %T`` names returned by common local Linux filesystems.  The
# list is intentionally explicit: a new/unknown type is evidence we have not
# classified, not permission to launch.
_LOCAL_FILESYSTEM_TYPES = frozenset(
    {
        "aufs",
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "ext2/ext3",
        "ext2/ext3/ext4",
        "f2fs",
        "jfs",
        "nilfs",
        "overlay",
        "overlayfs",
        "reiserfs",
        "xfs",
        "zfs",
    }
)
_NON_LOCAL_FILESYSTEM_TYPES = frozenset(
    {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "gfs",
        "gfs2",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smb2",
    }
)
_OUTPUT_RE = re.compile(r"^DISPATCH_FS_TYPE=([A-Za-z0-9._+/-]{1,64})$")


@dataclass(frozen=True)
class AttemptFilesystemPreflight:
    status: str
    filesystem_type: str | None
    reason_code: str


def classify_attempt_filesystem_preflight(
    stdout: object,
) -> AttemptFilesystemPreflight:
    """Parse the exact fixed-command output and fail closed on ambiguity."""

    if not isinstance(stdout, str):
        return AttemptFilesystemPreflight("unknown", None, "output_missing")
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return AttemptFilesystemPreflight("unknown", None, "output_malformed")
    match = _OUTPUT_RE.fullmatch(lines[0])
    if match is None:
        return AttemptFilesystemPreflight("unknown", None, "output_malformed")

    filesystem_type = match.group(1).lower()
    if filesystem_type in _LOCAL_FILESYSTEM_TYPES:
        return AttemptFilesystemPreflight(
            "eligible", filesystem_type, "local_filesystem_observed"
        )
    if (
        filesystem_type in _NON_LOCAL_FILESYSTEM_TYPES
        or filesystem_type.startswith("fuse")
    ):
        return AttemptFilesystemPreflight(
            "ineligible_non_local_fs",
            filesystem_type,
            "non_local_filesystem_observed",
        )
    return AttemptFilesystemPreflight(
        "unknown", filesystem_type, "filesystem_type_unclassified"
    )


# ---------------------------------------------------------------------------
# Shared request handler body
#
# The legacy `POST /server-config/{name}/attempt-preflight`
# (`app.main.server_attempt_backend_preflight_endpoint`) and the v2 mirror
# `POST /api/v2/server-configs/{name}/attempt-preflight`
# (`dispatch_center.api.routers.infrastructure_v2.attempt_server_config_preflight`)
# both call `run_attempt_filesystem_preflight()` so their behavior cannot
# diverge; each router only translates the exceptions below into its own
# error envelope (`HTTPException` vs `dispatch_center.api.errors.APIError`).
# ---------------------------------------------------------------------------


class AttemptFilesystemPreflightServerNotFoundError(Exception):
    """`name` is not a known server in `app_state.server_configs`."""


class AttemptFilesystemPreflightNoActiveRevisionError(Exception):
    """No active approved revision matches the current target/credential."""


class AttemptFilesystemPreflightUnreadableRevisionError(Exception):
    """The active revision's `normalized_target_json` cannot be parsed."""


class AttemptFilesystemPreflightNonSSHBackendError(Exception):
    """The active revision's pinned backend is not `ssh`."""


class AttemptFilesystemPreflightRevisionChangedError(Exception):
    """The active revision's identity changed during the SSH round trip."""


async def run_attempt_filesystem_preflight(
    app_state: Any,
    name: str,
    *,
    request_context: Any = None,
) -> dict[str, Any]:
    """Run and record the fixed D-5 filesystem observation for one revision.

    The remote command is read-only and contains no caller-controlled bytes.
    Recording is a CAS against the exact active approved revision.  A config,
    target or credential change during the SSH round trip refuses the write;
    every new revision starts with NULL evidence and remains ineligible.

    `app_state` is accepted duck-typed (not type-hinted as `AppState`) so this
    leaf module never imports `app.main` -- the same convention
    `dispatch_center.api.routers.jobs_v2._runtime()` already uses to reach the
    one running `AppState` instance.  Raises the typed errors above, or the
    `ValueError` `app_state.db.record_server_attempt_backend_preflight()`
    raises on an invalid/CAS-losing write; callers map both to their
    transport's error envelope.
    """

    # Local import: `app.audit` is a leaf module already safe to import here,
    # but importing it at module scope would run before `app.main` finishes
    # its own top-level imports of this module (see `ATTEMPT_FILESYSTEM_*`
    # imports in `app/main.py`).
    from app.audit import append_audit, audit_actor_from_request_context

    cfg = app_state.server_configs.get(name)
    if cfg is None:
        raise AttemptFilesystemPreflightServerNotFoundError(name)
    revision = app_state._matching_active_server_revision(
        name, require_ssh_preflight=False
    )
    if revision is None:
        raise AttemptFilesystemPreflightNoActiveRevisionError(name)
    try:
        pinned_target = json.loads(revision["normalized_target_json"])
    except (TypeError, json.JSONDecodeError):
        raise AttemptFilesystemPreflightUnreadableRevisionError(name) from None
    if pinned_target.get("backend") != "ssh":
        raise AttemptFilesystemPreflightNonSSHBackendError(name)

    try:
        result = await app_state.ssh_pool.run(
            cfg,
            ATTEMPT_FILESYSTEM_PREFLIGHT_COMMAND,
            15,
        )
        observation = classify_attempt_filesystem_preflight(result.stdout)
    except Exception:  # noqa: BLE001 - transport/error text may contain secrets
        observation = classify_attempt_filesystem_preflight(None)

    # Revalidate target/key identity after the remote observation.  A config
    # publication or key replacement during the round trip invalidates it.
    current_revision = app_state._matching_active_server_revision(
        name, require_ssh_preflight=False
    )
    if current_revision is None or current_revision["id"] != revision["id"]:
        raise AttemptFilesystemPreflightRevisionChangedError(name)

    audit_actor = audit_actor_from_request_context(request_context)
    recorded = app_state.db.record_server_attempt_backend_preflight(
        server_name=name,
        revision_id=revision["id"],
        status=observation.status,
        contract_version=ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
        filesystem_type=observation.filesystem_type,
        reason_code=observation.reason_code,
        audit_actor=audit_actor,
    )

    append_audit(
        "server_attempt_backend_preflight",
        {
            "server_name": name,
            "server_config_revision_id": revision["id"],
            "status": observation.status,
            "filesystem_type": observation.filesystem_type,
            "reason_code": observation.reason_code,
            "contract_version": ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
        },
        result=(
            "ok"
            if observation.status == "eligible"
            else "unknown"
            if observation.status == "unknown"
            else "ineligible"
        ),
        path=app_state.config.audit_path,
        actor=audit_actor,
    )
    return {
        "ok": observation.status == "eligible",
        "server_name": name,
        "server_config_revision_id": revision["id"],
        "status": observation.status,
        "filesystem_type": observation.filesystem_type,
        "reason_code": observation.reason_code,
        "contract_version": ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION,
        "observed_at": recorded["attempt_backend_preflight_observed_at"],
    }
