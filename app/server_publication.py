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

import base64
import logging
from dataclasses import dataclass
from typing import Any, Optional

import yaml as yaml_module

from app.execution_contract import utf8_sha256

logger = logging.getLogger(__name__)

SERVER_CONFIG_CONTRACT_VERSION = "server-config-v1"

# Fields that identify *where* work runs. Anything outside this set is
# operator metadata (notes, tags, idle thresholds) and is deliberately not part
# of the target identity digest: changing a note must not invalidate a pinned
# attempt's target.
_TARGET_FIELDS = ("backend", "host", "port", "user", "project_roots", "dataset_roots")


class ServerPublicationRejected(ValueError):
    """The approval no longer matches the file it was reviewed against."""


@dataclass(frozen=True)
class PublicationOutcome:
    mutation_id: Optional[str]
    revision_id: Optional[str]
    state: str  # activated | rolled_back | recovery_hold | skipped_legacy
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


def canonical_yaml_text(document: Any) -> str:
    """Return the exact canonical YAML representation used by publication."""

    return yaml_module.safe_dump(document, allow_unicode=True, sort_keys=False)


def yaml_digest(document: Any) -> str:
    """Digest the serialized YAML document exactly as it lands on disk."""

    return utf8_sha256(canonical_yaml_text(document))


def encode_yaml_document(document: Any) -> str:
    """Pin canonical YAML bytes inside an immutable approval payload.

    Keeping the document as base64 makes the contract JSON value a string,
    even when the YAML contains floats.  Approval-time code therefore writes
    the exact reviewed bytes instead of reinterpreting a legacy ``updates``
    object against whatever happens to be on disk later.
    """

    return base64.b64encode(canonical_yaml_text(document).encode("utf-8")).decode(
        "ascii"
    )


def decode_yaml_document(encoded: str) -> dict[str, Any]:
    """Decode and validate a request-pinned canonical servers document."""

    try:
        raw = base64.b64decode(encoded, validate=True)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid pinned server YAML document") from exc
    document = yaml_module.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError("pinned server YAML document must be a mapping")
    document.setdefault("servers", [])
    if not isinstance(document["servers"], list):
        raise ValueError("pinned server YAML servers must be a list")
    if canonical_yaml_text(document) != text:
        raise ValueError("pinned server YAML document is not canonical")
    return document


def build_server_config_contract(
    *,
    operation: str,
    server_name: str,
    yaml_before: dict[str, Any],
    yaml_after: dict[str, Any],
    server_payload: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Build the public ``server-config-v1`` immutable approval contract."""

    contract: dict[str, Any] = {
        "operation": operation,
        "server_name": server_name,
        "yaml_before_sha256": yaml_digest(yaml_before),
        "yaml_after_sha256": yaml_digest(yaml_after),
        "yaml_after_utf8_b64": encode_yaml_document(yaml_after),
    }
    if server_payload is not None:
        contract["normalized_target"] = normalize_target(server_payload)
        contract["credential_ref"] = credential_reference(server_payload)
    return contract


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
    reload_yaml=None,
    compensate_yaml=None,
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
        if reload_yaml is not None:
            reload_yaml()
        return PublicationOutcome(
            mutation_id=None,
            revision_id=None,
            state="skipped_legacy",
            reason="approval is not pinned to server-config-v1",
        )

    before_sha = yaml_digest(yaml_before)
    after_sha = yaml_digest(yaml_after)

    # Every operation except `add` must name the revision it supersedes, so a
    # mutation raced against a concurrent publication fails closed instead of
    # retiring a revision someone else already replaced.
    prior_revision_id = None
    if operation != "add":
        active = db.get_active_server_config_revision(server_name)
        if active is None:
            # A legacy server that was never published has no revision to
            # supersede. The YAML mutation must still happen — otherwise
            # disabling or deleting a legacy machine would silently do
            # nothing — it simply produces no journal entry.
            write_yaml()
            if reload_yaml is not None:
                reload_yaml()
            return PublicationOutcome(
                mutation_id=None,
                revision_id=None,
                state="skipped_legacy",
                reason="no active pinned revision to supersede",
            )
        prior_revision_id = active["id"]

    try:
        mutation = db.prepare_server_config_mutation(
            approval_id=approval_id,
            operation=operation,
            server_name=server_name,
            prior_revision_id=prior_revision_id,
            yaml_before_sha256=before_sha,
            yaml_after_sha256=after_sha,
            decision_actor_id=decision_actor_id,
            normalized_target=(
                normalize_target(server_payload) if server_payload is not None else None
            ),
            credential_ref=(
                credential_reference(server_payload)
                if server_payload is not None
                else None
            ),
        )
    except ValueError as exc:
        # The approval pinned the digests it was reviewed against. If the file
        # moved since then, publishing would activate a target nobody reviewed,
        # so fail closed and leave both the YAML and the journal untouched.
        # The operator re-requests against the current state.
        raise ServerPublicationRejected(
            f"server config changed since approval: {exc}"
        ) from exc

    def finalize_exact_after(*, reason: str) -> PublicationOutcome:
        db.transition_server_config_mutation(
            mutation_id=mutation["id"],
            expected_state="intent",
            new_state="yaml_applied",
            observed_yaml_sha256=after_sha,
        )
        if reload_yaml is not None:
            try:
                reload_yaml()
            except Exception as exc:  # noqa: BLE001
                if compensate_yaml is None:
                    # The durable yaml_applied intent remains unresolved and
                    # blocks all mutation/assignment. Never activate bytes the
                    # running process could not load.
                    raise ServerPublicationRejected(
                        f"server config reload failed: {type(exc).__name__}"
                    ) from exc
                try:
                    compensate_yaml()
                    observed_compensation = (
                        observe_yaml() if observe_yaml is not None else before_sha
                    )
                except Exception as compensation_exc:  # noqa: BLE001
                    observed_compensation = ""
                    if observe_yaml is not None:
                        try:
                            observed_compensation = observe_yaml()
                        except Exception:  # noqa: BLE001
                            pass
                    if observed_compensation not in {before_sha, after_sha}:
                        db.transition_server_config_mutation(
                            mutation_id=mutation["id"],
                            expected_state="yaml_applied",
                            new_state="recovery_hold",
                            observed_yaml_sha256=observed_compensation,
                            last_error_category="reload_compensation_failed",
                            sanitized_error_detail=type(
                                compensation_exc
                            ).__name__,
                        )
                    raise ServerPublicationRejected(
                        "server config reload and compensation failed"
                    ) from compensation_exc
                if observed_compensation != before_sha:
                    if observed_compensation not in {before_sha, after_sha}:
                        db.transition_server_config_mutation(
                            mutation_id=mutation["id"],
                            expected_state="yaml_applied",
                            new_state="recovery_hold",
                            observed_yaml_sha256=observed_compensation,
                            last_error_category="reload_compensation_drift",
                            sanitized_error_detail=type(exc).__name__,
                        )
                    raise ServerPublicationRejected(
                        "server config compensation did not restore reviewed bytes"
                    ) from exc
                db.transition_server_config_mutation(
                    mutation_id=mutation["id"],
                    expected_state="yaml_applied",
                    new_state="rolled_back",
                    observed_yaml_sha256=before_sha,
                    last_error_category="yaml_reload_failed",
                    sanitized_error_detail=type(exc).__name__,
                )
                return PublicationOutcome(
                    mutation_id=mutation["id"],
                    revision_id=mutation.get("prepared_revision_id"),
                    state="rolled_back",
                    reason="yaml_reload_failed",
                )
        db.activate_server_config_mutation(
            mutation_id=mutation["id"], observed_yaml_sha256=after_sha
        )
        return PublicationOutcome(
            mutation_id=mutation["id"],
            revision_id=mutation.get("prepared_revision_id"),
            state="activated",
            reason=reason,
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
            return finalize_exact_after(
                reason="write_reported_error_but_landed"
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

    if observe_yaml is not None:
        try:
            observed_after_write = observe_yaml()
        except Exception:  # noqa: BLE001
            observed_after_write = ""
        if observed_after_write != after_sha:
            if observed_after_write == before_sha:
                state = "rolled_back"
                reason = "yaml_write_did_not_land"
            else:
                state = "recovery_hold"
                reason = "yaml_digest_drift"
            db.transition_server_config_mutation(
                mutation_id=mutation["id"],
                expected_state="intent",
                new_state=state,
                observed_yaml_sha256=observed_after_write,
                last_error_category=reason,
                sanitized_error_detail="post_write_digest_mismatch",
            )
            return PublicationOutcome(
                mutation_id=mutation["id"],
                revision_id=mutation.get("prepared_revision_id"),
                state=state,
                reason=reason,
            )

    return finalize_exact_after(reason="published")
