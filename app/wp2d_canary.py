"""Operator-only request seam for the WP-2D SSH attempt canary.

The ordinary ``/dispatch`` route intentionally remains legacy-compatible and
therefore cannot create Jobs owned by the generic execution-attempt path.  A
WP-2D canary needs the already-approved ``enqueue-execution-v1`` contract, but
must not require operators to fabricate ProjectVersion or ExecutionPlan data.

This module creates only a pending approval.  It never creates a Job and never
contacts a worker.  The existing human approval endpoint materializes the
single pinned Job after revalidating the exact active SSH revision.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

from app.audit import append_audit, audit_actor_from_request_context
from app.db import Approval, Database
from app.execution_contract import utf8_sha256, validate_execution_contract
from app.identity import RequestContext
from app.security import is_dangerous


CONTRACT_VERSION = "enqueue-execution-v1"
PURPOSE = "wp2d-ssh-canary-v2"
MAX_COMMAND_BYTES = 8192


class Wp2dCanaryRequestError(ValueError):
    """The requested canary cannot safely enter the pinned approval flow."""


def _eligible_active_ssh_revision(
    db: Database,
    server_name: str,
    *,
    expected_revision_id: Optional[str] = None,
) -> dict[str, Any]:
    revision = db.get_active_server_config_revision(server_name)
    if revision is None:
        raise Wp2dCanaryRequestError("target_revision_missing")
    if expected_revision_id is not None and revision["id"] != expected_revision_id:
        raise Wp2dCanaryRequestError("target_revision_changed")
    if (
        revision.get("publication_state") != "active"
        or revision.get("assignment_eligibility") != "approved"
        or revision.get("attempt_backend_preflight") != "eligible"
        or revision.get("attempt_backend_preflight_contract_version")
        != "attempt-fs-preflight-v1"
    ):
        raise Wp2dCanaryRequestError("target_revision_not_attempt_eligible")
    try:
        normalized_target = json.loads(revision["normalized_target_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        raise Wp2dCanaryRequestError("target_identity_mismatch") from None
    if normalized_target.get("backend") != "ssh":
        raise Wp2dCanaryRequestError("target_backend_is_not_ssh")
    if any(
        mutation.get("server_name") == server_name
        for mutation in db.list_server_config_mutations(unresolved_only=True)
    ):
        raise Wp2dCanaryRequestError("target_revision_has_unresolved_publication")
    return revision


def _validate_candidate_commit(candidate_commit: object) -> str:
    candidate_commit = (candidate_commit or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", candidate_commit) is None:
        raise Wp2dCanaryRequestError("candidate_commit must be exactly 40 lowercase hex")
    return candidate_commit


def build_wp2d_canary_contract(
    *,
    server_name: str,
    server_config_revision_id: str,
    candidate_commit: str,
    command: str,
    gpus_needed: Optional[int] = 0,
    priority: str = "low",
) -> dict[str, Any]:
    """Build the closed, single-Job canary execution contract."""

    server_name = (server_name or "").strip()
    command = command.strip() if isinstance(command, str) else ""
    candidate_commit = _validate_candidate_commit(candidate_commit)
    if (
        not server_name
        or len(server_name) > 128
        or any(c in server_name for c in "\r\n\0")
    ):
        raise Wp2dCanaryRequestError("server_name is invalid")
    if not isinstance(server_config_revision_id, str) or not server_config_revision_id:
        raise Wp2dCanaryRequestError("server_config_revision_id is required")
    if (
        not command
        or "\0" in command
        or len(command.encode("utf-8")) > MAX_COMMAND_BYTES
    ):
        raise Wp2dCanaryRequestError("command must be 1..8192 UTF-8 bytes without NUL")
    dangerous, reason = is_dangerous(command)
    if dangerous:
        raise Wp2dCanaryRequestError(f"command rejected: {reason}")
    if gpus_needed is not None and (
        isinstance(gpus_needed, bool)
        or not isinstance(gpus_needed, int)
        or gpus_needed < 0
        or gpus_needed > 64
    ):
        raise Wp2dCanaryRequestError("gpus_needed must be between 0 and 64")
    if priority not in {"low", "normal"}:
        raise Wp2dCanaryRequestError("priority must be low or normal")

    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    contract = {
        "purpose": PURPOSE,
        "candidate_commit": candidate_commit,
        "server_name": server_name,
        "server_config_revision_id": server_config_revision_id,
        "authorized_operations": ["prepare", "launch", "collect"],
        "job_specs": [
            {
                "role": "main",
                "type": "adhoc",
                "command_utf8_b64": encoded,
                "command_sha256": utf8_sha256(command),
                "depends_on_roles": [],
                "pin_server": server_name,
                "gpus_needed": gpus_needed,
                "priority": priority,
            }
        ],
    }
    validate_execution_contract(contract)
    return contract


def validate_wp2d_canary_contract_for_approval(
    db: Database,
    approval: Approval,
) -> dict[str, Any]:
    """Approve-time closed-shape and target-revision revalidation."""

    payload = approval.payload
    if (
        approval.kind != "enqueue"
        or approval.payload_contract_version != CONTRACT_VERSION
    ):
        raise Wp2dCanaryRequestError("approval is not a pinned canary execution request")
    if not isinstance(payload, dict) or set(payload) != {
        "purpose",
        "candidate_commit",
        "server_name",
        "server_config_revision_id",
        "authorized_operations",
        "job_specs",
    }:
        raise Wp2dCanaryRequestError("canary contract shape changed")
    if payload.get("purpose") != PURPOSE:
        raise Wp2dCanaryRequestError("canary purpose mismatch")
    _validate_candidate_commit(payload.get("candidate_commit"))
    specs = payload.get("job_specs")
    if (
        not isinstance(specs, list)
        or len(specs) != 1
        or not isinstance(specs[0], dict)
    ):
        raise Wp2dCanaryRequestError("canary requires exactly one Job")
    spec = specs[0]
    if set(spec) != {
        "role",
        "type",
        "command_utf8_b64",
        "command_sha256",
        "depends_on_roles",
        "pin_server",
        "gpus_needed",
        "priority",
    }:
        raise Wp2dCanaryRequestError("canary Job contract shape changed")
    if (
        spec.get("role") != "main"
        or spec.get("type") != "adhoc"
        or spec.get("depends_on_roles") != []
        or spec.get("pin_server") != payload.get("server_name")
    ):
        raise Wp2dCanaryRequestError("canary Job identity mismatch")
    validate_execution_contract(payload)
    return _eligible_active_ssh_revision(
        db,
        payload["server_name"],
        expected_revision_id=payload["server_config_revision_id"],
    )


def request_wp2d_canary_approval(
    db: Database,
    *,
    server_name: str,
    candidate_commit: str,
    command: str,
    acknowledge_non_production: bool,
    gpus_needed: Optional[int] = 0,
    priority: str = "low",
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """Create one pending canary approval without creating a Job or using SSH."""

    if acknowledge_non_production is not True:
        raise Wp2dCanaryRequestError(
            "explicit acknowledge_non_production=true is required"
        )
    revision = _eligible_active_ssh_revision(db, server_name)
    contract = build_wp2d_canary_contract(
        server_name=server_name,
        server_config_revision_id=revision["id"],
        candidate_commit=candidate_commit,
        command=command,
        gpus_needed=gpus_needed,
        priority=priority,
    )
    approval_id = db.insert_pinned_approval(
        kind="enqueue",
        contract_version=CONTRACT_VERSION,
        payload=contract,
        requester_actor_id=(request_context.actor_id if request_context else None),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "enqueue",
            "purpose": PURPOSE,
            "candidate_commit": contract["candidate_commit"],
            "server": contract["server_name"],
            "server_config_revision_id": contract["server_config_revision_id"],
            "command_sha256": contract["job_specs"][0]["command_sha256"],
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)
