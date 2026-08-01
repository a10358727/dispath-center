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

import re
from dataclasses import dataclass


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
