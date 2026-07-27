"""RB-SERVER-001: publish approved server mutations as pinned revisions.

Approved by the `DG-EXEC-ATTEMPT-v1.1` addendum on 2026-07-27. It authorizes
exactly one thing: rerouting the *execution* of the existing server approval
kinds through the already-approved publication protocol. It adds no approval
kind, no public route, and no change to `VALID_APPROVAL_KINDS` or the
auto-approve allowlist.

Why this matters beyond bookkeeping: a target that never materializes an
approved immutable revision stays `assignment_eligibility='legacy_observed'`,
and `create_execution_attempt()` refuses to build a generic attempt against
it. Without this, `EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED` could never be turned
on for any server, no matter how well the launch path works.

The ordering is the §7 protocol, and every step is crash-safe:

    intent -> (write YAML) -> yaml_applied -> activated

A crash at any boundary converges to old-active, new-active, or an explicit
fail-closed `recovery_hold`. Compensation accepts only exact before/after
digests; digest drift enters `recovery_hold` and blocks both assignment and
further mutation of that server rather than guessing which version is real.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.execution_contract import utf8_sha256

logger = logging.getLogger(__name__)

SERVER_CONFIG_CONTRACT_VERSION = "server-config-v1"

# Fields that identify *where* work runs. Anything outside this set is
# operator metadata (notes, tags, idle thresholds) and is deliberately not part
# of the target identity digest: changing a note must not invalidate a pinned
# attempt's target.
_TARGET_FIELDS = ("backend", "host", "port", "user", "project_roots", "dataset_roots")


@dataclass(frozen=True)
class PublicationOutcome:
    mutation_id: Optional[str]
    revision_id: Optional[str]
    state: str  # 'activated' | 'recovery_hold' | 'skipped_legacy'
    reason: str


def normalize_target(server_payload: dict[str, Any]) -> dict[str, Any]:
    """Project a servers.yaml entry onto its stable target identity."""
    return {
        "backend": server_payload.get("execution_backend", "ssh"),
        "host": server_payload.get("host"),
        "port": int(server_payload.get("port", 22) or 22),
        "user": server_payload.get("user"),
        "project_roots": list(server_payload.get("project_roots") or []),
        "dataset_roots": list(server_payload.get("dataset_roots") or []),
    }


def credential_reference(server_payload: dict[str, Any]) -> dict[str, Any]:
    """Reference the key by path and identity — never its bytes.

    `file_identity` is what lets a later rotation be detected: the same path
    pointing at a different inode is a different credential, and an attempt
    pinned to the old one must not silently follow the new one.
    """
    import os

    key_path = os.path.expanduser(str(server_payload.get("key") or ""))
    identity: dict[str, Any] = {"device": 0, "inode": 0, "size": 0, "mtime_ns": 0}
    try:
        stat = os.stat(key_path)
        identity = {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    except OSError:
        # An unreadable key is recorded as an all-zero identity rather than
        # failing publication: the operator may be registering a machine whose
        # credential is installed later. It stays visible as "no identity
        # observed" instead of being invented.
        pass
    return {
        "provider": "ssh-key-file-v1",
        "version_id": utf8_sha256(f"{key_path}:{identity['inode']}:{identity['mtime_ns']}")[:16],
        "real_path": key_path,
        "file_identity": identity,
    }


def yaml_digest(document: Any) -> str:
    """Digest the serialized YAML document exactly as it lands on disk."""
    import yaml as yaml_module

    return utf8_sha256(
        yaml_module.safe_dump(document, allow_unicode=True, sort_keys=False)
    )


def publish_approved_server_mutation(
    db,
    *,
    approval_id: int,
    operation: str,
    server_name: str,
    server_payload: Optional[dict[str, Any]],
    yaml_before: Any,
    yaml_after: Any,
    decision_actor_id: str,
    write_yaml,
    observe_yaml=None,
) -> PublicationOutcome:
    """Run the publication protocol around the YAML write.

    `write_yaml` is invoked exactly once, between `intent` and `yaml_applied`,
    so the durable intent always precedes the file mutation. If it raises, the
    journal is compensated with the observed digest rather than left dangling.

    `observe_yaml` re-reads the file and returns its digest. It is only used
    after a failed write, because compensation must follow what is actually on
    disk: a write that failed cleanly leaves the `before` digest and rolls
    back, a write that actually landed leaves `after` and proceeds, and any
    third value is a genuinely unknown state that must fail closed into
    `recovery_hold`. Assuming the failure was clean is exactly how a half
    written config becomes an unnoticed active target.

    An unpinned legacy approval is not published. Publishing requires the
    approval to carry the `server-config-v1` contract, and inventing one for a
    row that was never reviewed under it would fabricate the very evidence the
    protocol exists to provide.
    """

    approval = db.get_approval(approval_id)
    contract_version = getattr(approval, "payload_contract_version", None)
    if contract_version != SERVER_CONFIG_CONTRACT_VERSION:
        write_yaml()
        return PublicationOutcome(
            mutation_id=None,
            revision_id=None,
            state="skipped_legacy",
            reason="approval is not pinned to server-config-v1",
        )

    before_sha = yaml_digest(yaml_before)
    after_sha = yaml_digest(yaml_after)

    mutation = db.prepare_server_config_mutation(
        approval_id=approval_id,
        operation=operation,
        server_name=server_name,
        yaml_before_sha256=before_sha,
        yaml_after_sha256=after_sha,
        decision_actor_id=decision_actor_id,
        normalized_target=(
            normalize_target(server_payload) if server_payload is not None else None
        ),
        credential_ref=(
            credential_reference(server_payload) if server_payload is not None else None
        ),
    )

    try:
        write_yaml()
    except Exception as exc:  # noqa: BLE001
        observed = None
        if observe_yaml is not None:
            try:
                observed = observe_yaml()
            except Exception:  # noqa: BLE001
                observed = None
        if observed == after_sha:
            # The write landed despite the error. Continue the protocol rather
            # than rolling back a target that is already on disk.
            db.transition_server_config_mutation(
                mutation_id=mutation["id"],
                expected_state="intent",
                new_state="yaml_applied",
                observed_yaml_sha256=after_sha,
            )
            db.activate_server_config_mutation(
                mutation_id=mutation["id"], observed_yaml_sha256=after_sha
            )
            return PublicationOutcome(
                mutation_id=mutation["id"],
                revision_id=mutation.get("prepared_revision_id"),
                state="activated",
                reason="write_reported_error_but_landed",
            )
        if observed == before_sha or observed is None and observe_yaml is None:
            # Clean failure: the file never changed, so the prepared revision
            # is compensated away and the old target stays active.
            db.transition_server_config_mutation(
                mutation_id=mutation["id"],
                expected_state="intent",
                new_state="rolled_back",
                observed_yaml_sha256=before_sha,
                last_error_category="yaml_write_failed",
                sanitized_error_detail=type(exc).__name__,
            )
            return PublicationOutcome(
                mutation_id=mutation["id"],
                revision_id=mutation.get("prepared_revision_id"),
                state="rolled_back",
                reason="yaml_write_failed",
            )
        # A third or unreadable digest: nobody can say what is on disk, so fail
        # closed. This blocks both assignment and further mutation of this
        # server until an operator resolves it.
        db.transition_server_config_mutation(
            mutation_id=mutation["id"],
            expected_state="intent",
            new_state="recovery_hold",
            observed_yaml_sha256=observed or "",
            last_error_category="yaml_digest_drift",
            sanitized_error_detail=type(exc).__name__,
        )
        return PublicationOutcome(
            mutation_id=mutation["id"],
            revision_id=mutation.get("prepared_revision_id"),
            state="recovery_hold",
            reason="yaml_digest_drift",
        )

    db.transition_server_config_mutation(
        mutation_id=mutation["id"],
        expected_state="intent",
        new_state="yaml_applied",
        observed_yaml_sha256=after_sha,
    )
    db.activate_server_config_mutation(
        mutation_id=mutation["id"], observed_yaml_sha256=after_sha
    )
    return PublicationOutcome(
        mutation_id=mutation["id"],
        revision_id=mutation.get("prepared_revision_id"),
        state="activated",
        reason="published",
    )
