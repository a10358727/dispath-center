"""WP-3B: immutable ExecutionPlan derivation (plan §8.4).

An ExecutionPlan is the answer to "exactly what will run, on what code, against
what data, on which machine". Every input it binds is an immutable revision
identifier, never a mutable head:

- code   -> `project_versions.id` (a pinned git commit)
- data   -> `dataset_snapshots.id` in state `published`, or explicitly none
- config -> `run_profiles.id` (a specific revision, not a `(project, name)` head)
- target -> `server_config_revisions.id` with `assignment_eligibility='approved'`
- command -> its own SHA-256

Binding a mutable head anywhere would defeat the whole point: the run could
change meaning between preview, approval and execution without anything
recording that it had.

This module is pure. `derive_plan_draft()` performs no writes and no remote
calls, which is what lets the preview endpoint be a genuine read: a user can
ask "what would happen" without creating anything.

Reproducibility is a *stated* property, never an inferred one. A plan is
`reproducible` only when code, data and profile are all pinned. A legacy
registry dataset makes it `false` with reason `dataset_not_reproducible`
(gate D-5) — the request is rejected rather than silently downgraded, because
a run that claims reproducibility it does not have is worse than a refused one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.execution_contract import canonical_json, utf8_sha256

PLAN_CONTRACT_VERSION = "execution-plan-v1"

#: Closed set. A reason code is recorded when the decision is made; nothing
#: here is re-derived at read time.
PLAN_REASON_CODES = frozenset(
    {
        "plan_ready",
        "project_version_missing",
        "run_profile_missing",
        "run_profile_archived",
        "dataset_not_reproducible",
        "dataset_snapshot_not_published",
        "target_not_approved",
        "target_missing",
        "command_missing",
        "command_dangerous",
        "plan_digest_mismatch",
    }
)

BLOCKING_REASON_CODES = PLAN_REASON_CODES - {"plan_ready"}


@dataclass(frozen=True)
class PlanInputs:
    """What the requester selected. All identifiers, no free-floating paths."""

    project_name: str
    command: str
    project_version_id: Optional[str] = None
    run_profile_id: Optional[str] = None
    dataset_snapshot_id: Optional[str] = None
    #: Explicitly requesting no dataset. Distinct from "forgot to choose one":
    #: the plan records that the absence was intended.
    dataset_none: bool = False
    server_config_revision_id: Optional[str] = None
    require_reproducible: bool = True


@dataclass(frozen=True)
class PlanDraft:
    """A preview. Never persisted by the previewer, never executable by itself."""

    project_name: str
    command_sha256: str
    contract_version: str
    reproducible: bool
    reason_codes: tuple[str, ...]
    missing: tuple[str, ...]
    project_version_id: Optional[str] = None
    run_profile_id: Optional[str] = None
    dataset_snapshot_id: Optional[str] = None
    #: Carried explicitly rather than inferred from a null snapshot id: the
    #: digest covers it, so anything persisting a plan must reproduce it
    #: exactly rather than guessing.
    dataset_none: bool = False
    server_config_revision_id: Optional[str] = None
    plan_digest: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.reason_codes == ("plan_ready",)


def _canonical_plan_body(
    inputs: PlanInputs,
    *,
    command_sha256: str,
    reproducible: bool,
) -> dict[str, Any]:
    """The exact bytes the digest covers.

    `dataset_snapshot_id=None` with `dataset_none=True` is a different plan
    from one where no dataset was selected at all, so both facts are recorded.
    """
    return {
        "contract": PLAN_CONTRACT_VERSION,
        "project_name": inputs.project_name,
        "command_sha256": command_sha256,
        "project_version_id": inputs.project_version_id,
        "run_profile_id": inputs.run_profile_id,
        "dataset_snapshot_id": inputs.dataset_snapshot_id,
        "dataset_none": bool(inputs.dataset_none),
        "server_config_revision_id": inputs.server_config_revision_id,
        "reproducible": reproducible,
    }


def compute_plan_digest(
    inputs: PlanInputs, *, command_sha256: str, reproducible: bool
) -> str:
    return utf8_sha256(
        canonical_json(
            _canonical_plan_body(
                inputs, command_sha256=command_sha256, reproducible=reproducible
            )
        )
    )


@dataclass
class ResolvedInputs:
    """What the caller looked up. Passing these in keeps this module pure and
    makes every rejection reproducible in a test without a database."""

    project_version_exists: bool = False
    run_profile_status: Optional[str] = None  # 'active' | 'archived' | None
    dataset_snapshot_state: Optional[str] = None  # 'published' | other | None
    dataset_is_legacy_registry: bool = False
    target_eligibility: Optional[str] = None  # 'approved' | 'legacy_observed' | None
    command_is_dangerous: bool = False
    extra_reason_codes: tuple[str, ...] = field(default_factory=tuple)


def derive_plan_draft(inputs: PlanInputs, resolved: ResolvedInputs) -> PlanDraft:
    """Derive a draft plan and every reason it is not ready.

    All blocking reasons are collected rather than short-circuiting on the
    first: a requester fixing one missing prerequisite at a time, with a round
    trip each, is a worse experience than being told everything at once.
    """

    reasons: list[str] = []
    missing: list[str] = []

    if not inputs.command or not inputs.command.strip():
        reasons.append("command_missing")
        missing.append("command")
    if resolved.command_is_dangerous:
        reasons.append("command_dangerous")

    if inputs.project_version_id is None:
        missing.append("project_version")
        if inputs.require_reproducible:
            reasons.append("project_version_missing")
    elif not resolved.project_version_exists:
        reasons.append("project_version_missing")

    if inputs.run_profile_id is None:
        missing.append("run_profile")
        if inputs.require_reproducible:
            reasons.append("run_profile_missing")
    elif resolved.run_profile_status is None:
        reasons.append("run_profile_missing")
    elif resolved.run_profile_status == "archived":
        reasons.append("run_profile_archived")

    # Gate D-5. A legacy registry dataset cannot pin bytes, so a plan that
    # claims reproducibility with one is rejected here rather than downgraded.
    if resolved.dataset_is_legacy_registry:
        reasons.append("dataset_not_reproducible")
    elif inputs.dataset_snapshot_id is not None:
        if resolved.dataset_snapshot_state != "published":
            reasons.append("dataset_snapshot_not_published")
    elif not inputs.dataset_none:
        missing.append("dataset")
        if inputs.require_reproducible:
            reasons.append("dataset_snapshot_not_published")

    if inputs.server_config_revision_id is None:
        missing.append("target")
        reasons.append("target_missing")
    elif resolved.target_eligibility != "approved":
        reasons.append("target_not_approved")

    reasons.extend(resolved.extra_reason_codes)

    # Reproducible only when code, data and profile are all pinned, and
    # nothing is blocking. `dataset_none` counts as pinned: "no data" is a
    # reproducible statement, "some directory" is not.
    reproducible = (
        not reasons
        and inputs.project_version_id is not None
        and inputs.run_profile_id is not None
        and (inputs.dataset_snapshot_id is not None or inputs.dataset_none)
        and not resolved.dataset_is_legacy_registry
    )

    command_sha256 = utf8_sha256(inputs.command or "")
    ordered = tuple(sorted(set(reasons))) if reasons else ("plan_ready",)
    for code in ordered:
        if code not in PLAN_REASON_CODES:
            raise ValueError(f"reason code outside the closed set: {code}")

    digest = (
        compute_plan_digest(
            inputs, command_sha256=command_sha256, reproducible=reproducible
        )
        if not reasons
        else None
    )

    return PlanDraft(
        project_name=inputs.project_name,
        command_sha256=command_sha256,
        contract_version=PLAN_CONTRACT_VERSION,
        reproducible=reproducible,
        reason_codes=ordered,
        missing=tuple(sorted(set(missing))),
        project_version_id=inputs.project_version_id,
        run_profile_id=inputs.run_profile_id,
        dataset_snapshot_id=inputs.dataset_snapshot_id,
        dataset_none=bool(inputs.dataset_none),
        server_config_revision_id=inputs.server_config_revision_id,
        plan_digest=digest,
    )


def reverify_persisted_plan(plan_row: dict[str, Any], resolved: ResolvedInputs) -> tuple[bool, tuple[str, ...]]:
    """Approve-time re-verification of a persisted plan.

    The plan stores `command_sha256`, not the command text — storing the text
    twice would create something that could drift from the digest. So the
    canonical body is rebuilt from the persisted fields and re-digested, while
    `resolved` supplies the *current* state of every bound revision.

    Returns `(still_valid, reason_codes)`. A revision that moved, was archived,
    or lost its published/approved state makes the re-derived draft unready,
    which changes the digest and fails verification.
    """

    inputs = PlanInputs(
        project_name=plan_row["project_name"],
        command="",  # not needed: the digest covers the stored command hash
        project_version_id=plan_row["project_version_id"],
        run_profile_id=plan_row["run_profile_id"],
        dataset_snapshot_id=plan_row["dataset_snapshot_id"],
        dataset_none=bool(plan_row["dataset_none"]),
        server_config_revision_id=plan_row["server_config_revision_id"],
    )

    reasons: list[str] = []
    if inputs.project_version_id is not None and not resolved.project_version_exists:
        reasons.append("project_version_missing")
    if inputs.run_profile_id is not None:
        if resolved.run_profile_status is None:
            reasons.append("run_profile_missing")
        elif resolved.run_profile_status == "archived":
            reasons.append("run_profile_archived")
    if inputs.dataset_snapshot_id is not None and (
        resolved.dataset_snapshot_state != "published"
    ):
        reasons.append("dataset_snapshot_not_published")
    if resolved.dataset_is_legacy_registry:
        reasons.append("dataset_not_reproducible")
    if resolved.target_eligibility != "approved":
        reasons.append("target_not_approved")
    if resolved.command_is_dangerous:
        reasons.append("command_dangerous")

    if reasons:
        return False, tuple(sorted(set(reasons)))

    recomputed = compute_plan_digest(
        inputs,
        command_sha256=plan_row["command_sha256"],
        reproducible=bool(plan_row["reproducible"]),
    )
    if recomputed != plan_row["plan_digest"]:
        return False, ("plan_digest_mismatch",)
    return True, ("plan_ready",)


def plan_matches_draft(persisted: dict[str, Any], draft: PlanDraft) -> bool:
    """Approve-time re-verification.

    A plan is approved by its digest. If any bound revision moved between
    request and approval, the recomputed digest differs and the approval must
    be refused rather than silently executing the newer inputs.
    """
    return (
        draft.plan_digest is not None
        and persisted.get("plan_digest") == draft.plan_digest
        and persisted.get("contract_version") == draft.contract_version
    )
