"""Goal 2 Slice 4 — policy-driven placement proposals
(docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md §Slice 4, DG-1 approved,
docs/DECISIONS.md 2026-07-18).

Covers: pure evaluation (`app/auto_placement.py`), proposal creation
(`request_auto_placement_approval`), the `auto_placement` approve branch
(job-chain parity with a manual pinned enqueue, revalidation rejections),
the additive `jobs.auto_placement_approval_id` migration, and the
`AppState.auto_placement_loop()` best-effort tick.

**Zero scheduling behavior is touched**: `scheduler_tick`/`pick_job`/
`is_idle`/`dispatch_job` are unmodified and this test module never asserts
on them changing.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.approvals import (
    approve,
    maybe_auto_approve,
    maybe_auto_decide_placement,
    request_auto_placement_approval,
    request_dispatch_policy_create_approval,
    request_enqueue_approval,
)
from app.auto_placement import PlacementCandidate, evaluate_placement_candidates
from app.config import ServerConfig
from app.db import Database, DispatchPolicy
from app.identity import ActorType, RequestContext
from app.monitor import GpuReading, ServerState


_DP_CONFIG = SimpleNamespace(
    dispatch_policy_v1_enabled=True, auto_placement_cooldown_sec=3600
)


def _human_context(db, name: str = "Ada") -> RequestContext:
    actor = db.insert_actor(
        actor_type=ActorType.HUMAN, display_name=name, platform_admin=True
    )
    return RequestContext(actor=actor, authentication_method="session")


def _approve_dispatch_policy(db, approval_id, audit_path, context):
    return asyncio.run(
        approve(
            db,
            approval_id,
            app_state=SimpleNamespace(config=_DP_CONFIG),
            audit_path=audit_path,
            request_context=context,
        )
    )


def _approve_auto_placement(
    db, approval_id, audit_path, context, *, server_configs=None, enabled=True
):
    config = SimpleNamespace(
        dispatch_policy_v1_enabled=enabled, auto_placement_cooldown_sec=3600
    )
    return asyncio.run(
        approve(
            db,
            approval_id,
            server_configs=server_configs or {},
            app_state=SimpleNamespace(config=config),
            audit_path=audit_path,
            request_context=context,
        )
    )


def _server_cfg(name: str, **overrides) -> ServerConfig:
    values = {
        "name": name,
        "host": "127.0.0.1",
        "user": "test",
        "key": "~/.ssh/id_rsa",
        "gpu": True,
        "idle_gpu_util": 15.0,
        "idle_load": 2.0,
        "enabled": True,
        "tags": [],
    }
    values.update(overrides)
    return ServerConfig(**values)


def _server_state(name: str, **overrides) -> ServerState:
    values = {"name": name, "online": True}
    values.update(overrides)
    return ServerState(**values)


def _policy(**overrides) -> DispatchPolicy:
    values = {
        "id": "policy-1",
        "project_id": "proj-1",
        "project_name": "proj1",
        "name": "p",
        "revision": 1,
        "status": "approved",
        "allowed_servers": ["worker-a"],
        "require_tag": None,
        "run_profile_id": None,
        "dataset_required": False,
        "max_concurrent_placements": 1,
        "valid_until": None,
        "approval_id": 1,
        "created_by_actor_id": None,
        "created_at": "2026-07-18T00:00:00+00:00",
    }
    values.update(overrides)
    return DispatchPolicy(**values)


_NOW = "2026-07-18T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------


def _always_false_dataset_cached(server, name, version):
    return False


def _no_dataset_binding(project_id):
    return None


def test_evaluate_idle_gpu_server_is_a_candidate():
    policy = _policy()
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert result == [
        PlacementCandidate(
            policy_id="policy-1",
            policy_name="p",
            policy_revision=1,
            project_id="proj-1",
            project_name="proj1",
            server_name="worker-a",
        )
    ]


def test_evaluate_busy_gpu_server_is_not_a_candidate():
    policy = _policy()
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(90.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert result == []


def test_evaluate_has_running_job_is_not_a_candidate():
    policy = _policy()
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={"worker-a": True},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert result == []


def test_evaluate_disabled_server_is_excluded():
    policy = _policy()
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a", enabled=False)},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert result == []


def test_evaluate_tag_mismatch_is_excluded():
    policy = _policy(require_tag="a100")
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a", tags=["v100"])},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert result == []


def test_evaluate_tag_match_is_included():
    policy = _policy(require_tag="a100")
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a", tags=["a100"])},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert len(result) == 1


def test_evaluate_server_not_in_allowed_servers_is_excluded():
    policy = _policy(allowed_servers=["worker-b"])
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert result == []


def test_evaluate_expired_valid_until_is_excluded():
    policy = _policy(valid_until="2020-01-01T00:00:00+00:00")
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert result == []


def test_evaluate_future_valid_until_is_included():
    policy = _policy(valid_until="2099-01-01T00:00:00+00:00")
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert len(result) == 1


def test_evaluate_dataset_required_but_no_binding_is_excluded():
    policy = _policy(dataset_required=True)
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={},
        dataset_cached_lookup=lambda *a: True,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert result == []


def test_evaluate_dataset_required_and_not_cached_is_excluded():
    policy = _policy(dataset_required=True)
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=lambda project_id: ("ds1", "v1"),
        now_iso=_NOW,
    )
    assert result == []


def test_evaluate_dataset_required_and_cached_is_included():
    policy = _policy(dataset_required=True)
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={},
        dataset_cached_lookup=lambda server, name, version: (server, name, version)
        == ("worker-a", "ds1", "v1"),
        project_dataset_lookup=lambda project_id: ("ds1", "v1"),
        now_iso=_NOW,
    )
    assert len(result) == 1


def test_evaluate_output_is_deterministically_sorted():
    policies = [
        _policy(id="policy-b", name="zzz", allowed_servers=["worker-b", "worker-a"]),
    ]
    result = evaluate_placement_candidates(
        policies=policies,
        server_states={
            "worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)]),
            "worker-b": _server_state("worker-b", gpus=[GpuReading(5.0, 100, 8192)]),
        },
        server_configs={
            "worker-a": _server_cfg("worker-a"),
            "worker-b": _server_cfg("worker-b"),
        },
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert [c.server_name for c in result] == ["worker-a", "worker-b"]


def test_evaluate_non_approved_policy_is_excluded():
    policy = _policy(status="archived")
    result = evaluate_placement_candidates(
        policies=[policy],
        server_states={"worker-a": _server_state("worker-a", gpus=[GpuReading(5.0, 100, 8192)])},
        server_configs={"worker-a": _server_cfg("worker-a")},
        has_running_job_by_server={},
        dataset_cached_lookup=_always_false_dataset_cached,
        project_dataset_lookup=_no_dataset_binding,
        now_iso=_NOW,
    )
    assert result == []


# ---------------------------------------------------------------------------
# Proposal creation (request_auto_placement_approval)
# ---------------------------------------------------------------------------


def _make_policy_via_db(db, audit_path, context, **overrides) -> DispatchPolicy:
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    kwargs = {
        "allowed_servers": ["worker-a"],
        "max_concurrent_placements": 1,
    }
    kwargs.update(overrides)
    approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", config=_DP_CONFIG, audit_path=audit_path,
        request_context=context, **kwargs,
    )
    return _approve_dispatch_policy(db, approval.id, audit_path, context)["dispatch_policy"]


def test_request_auto_placement_happy_path_payload_shape(db, audit_path):
    context = _human_context(db)
    db.update_project(
        "proj1", default_command="python train.py", setup_cmd=None
    ) if db.get_project("proj1") else None
    policy = _make_policy_via_db(db, audit_path, context)
    db.update_project("proj1", default_command="python train.py")

    approval = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert approval is not None
    assert approval.kind == "auto_placement"
    assert approval.status == "pending"
    payload = approval.payload
    assert payload["policy_id"] == policy.id
    assert payload["policy_revision"] == policy.revision
    assert payload["project"] == "proj1"
    assert payload["server"] == "worker-a"
    assert payload["command"] == "python train.py"
    assert payload["priority"] == "normal"
    import hashlib

    assert payload["command_sha256"] == hashlib.sha256(
        "python train.py".encode("utf-8")
    ).hexdigest()


def test_request_auto_placement_skips_on_pending_duplicate(db, audit_path):
    context = _human_context(db)
    policy = _make_policy_via_db(db, audit_path, context)
    db.update_project("proj1", default_command="python train.py")

    first = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert first is not None
    second = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert second is None


def test_request_auto_placement_skips_within_cooldown_after_rejection(db, audit_path):
    context = _human_context(db)
    policy = _make_policy_via_db(db, audit_path, context)
    db.update_project("proj1", default_command="python train.py")

    first = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert first is not None
    db.update_approval(first.id, status="rejected", decided_at="2026-07-18T00:00:01+00:00")

    second = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert second is None


def test_request_auto_placement_skips_at_max_concurrent_placements(db, audit_path):
    context = _human_context(db)
    server_configs = {"worker-a": _server_cfg("worker-a")}
    policy = _make_policy_via_db(
        db, audit_path, context, max_concurrent_placements=1
    )
    db.update_project("proj1", default_command="python train.py")

    approval = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert approval is not None
    result = _approve_auto_placement(
        db, approval.id, audit_path, context, server_configs=server_configs
    )
    assert result["approval"].status == "approved"
    # A queued job now counts against the policy's concurrency cap.
    assert (
        db.count_active_auto_placement_jobs(policy.id)
        >= policy.max_concurrent_placements
    )

    second = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert second is None


def test_request_auto_placement_skips_on_dangerous_command(db, audit_path):
    context = _human_context(db)
    policy = _make_policy_via_db(db, audit_path, context)
    db.update_project("proj1", default_command="rm -rf /")

    approval = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert approval is None


def test_request_auto_placement_skips_on_empty_command(db, audit_path):
    context = _human_context(db)
    policy = _make_policy_via_db(db, audit_path, context)
    # default_command left None (never set) — nothing resolvable.

    approval = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert approval is None


def test_request_auto_placement_skips_when_run_profile_archived(db, audit_path):
    from app.approvals import (
        request_run_profile_archive_approval,
        request_run_profile_create_approval,
    )

    context = _human_context(db)
    db.insert_project("proj1", "https://example.invalid/proj1.git")
    profile_approval = request_run_profile_create_approval(
        db, "proj1", "prof", command="python train.py",
        config=SimpleNamespace(run_profile_v1_enabled=True),
        audit_path=audit_path, request_context=context,
    )
    profile = asyncio.run(
        approve(
            db, profile_approval.id,
            app_state=SimpleNamespace(config=SimpleNamespace(run_profile_v1_enabled=True)),
            audit_path=audit_path, request_context=context,
        )
    )["run_profile"]

    policy_approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", allowed_servers=["worker-a"], run_profile_id=profile.id,
        config=_DP_CONFIG, audit_path=audit_path, request_context=context,
    )
    policy = _approve_dispatch_policy(db, policy_approval.id, audit_path, context)[
        "dispatch_policy"
    ]

    archive_approval = request_run_profile_archive_approval(
        db, "proj1", "prof",
        config=SimpleNamespace(run_profile_v1_enabled=True),
        audit_path=audit_path, request_context=context,
    )
    asyncio.run(
        approve(
            db, archive_approval.id,
            app_state=SimpleNamespace(config=SimpleNamespace(run_profile_v1_enabled=True)),
            audit_path=audit_path, request_context=context,
        )
    )

    approval = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert approval is None


def test_auto_placement_kind_is_never_auto_approved():
    from app.db import VALID_APPROVAL_KINDS

    assert "auto_placement" in VALID_APPROVAL_KINDS
    result = asyncio.run(
        maybe_auto_approve(
            SimpleNamespace(),
            SimpleNamespace(kind="auto_placement", id=1),
            source="web",
            rules=[{"kind": "any", "action": "approve"}],
        )
    )
    assert result is None


# ---------------------------------------------------------------------------
# Approve branch
# ---------------------------------------------------------------------------


def test_approve_creates_same_job_chain_as_manual_pinned_enqueue(db, audit_path):
    context = _human_context(db)
    db.insert_project(
        "proj1",
        "https://example.invalid/proj1.git",
        dataset_name="ds1",
        dataset_version="v1",
        default_command="python train.py",
        setup_cmd="pip install -r requirements.txt",
    )
    db.insert_dataset(
        "ds1", "v1", 1000, "/data/ds1",
        {"file_count": 0, "total_size": 0, "files": []},
    )
    server_configs = {
        "worker-a": _server_cfg(
            "worker-a", host="1.2.3.4", user="worker", key="~/.ssh/id_rsa"
        )
    }

    policy_approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", allowed_servers=["worker-a"], config=_DP_CONFIG,
        audit_path=audit_path, request_context=context,
    )
    policy = _approve_dispatch_policy(db, policy_approval.id, audit_path, context)[
        "dispatch_policy"
    ]

    manual_approval = request_enqueue_approval(
        db, command="python train.py", type="train", project="proj1",
        pin_server="worker-a", audit_path=audit_path, request_context=context,
    )
    manual_result = asyncio.run(
        approve(
            db, manual_approval.id, server_configs=server_configs,
            audit_path=audit_path, request_context=context,
        )
    )
    # NOTE: the manual `enqueue` approve branch does not return
    # setup_job_id/sync_job_id in its result dict (only in the audit
    # record) — resolve dependency jobs via `job.depends_on` instead, which
    # is the stable, documented way to walk the chain.
    manual_job = manual_result["job"]
    manual_dep_jobs = [db.get_job(dep_id) for dep_id in manual_job.depends_on]
    manual_setup_job = next(j for j in manual_dep_jobs if j.type == "setup")
    manual_sync_job = next(j for j in manual_dep_jobs if j.type == "sync")

    auto_approval = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert auto_approval is not None
    auto_result = _approve_auto_placement(
        db, auto_approval.id, audit_path, context, server_configs=server_configs
    )
    auto_job = auto_result["job"]
    auto_setup_job = db.get_job(auto_result["setup_job_id"])
    auto_sync_job = db.get_job(auto_result["sync_job_id"])

    assert auto_job.command == manual_job.command
    assert auto_job.pin_server == manual_job.pin_server == "worker-a"
    assert sorted(auto_job.depends_on) == sorted([auto_setup_job.id, auto_sync_job.id])
    assert auto_setup_job.command == manual_setup_job.command
    assert auto_sync_job.command == manual_sync_job.command

    assert auto_job.auto_placement_approval_id == auto_approval.id
    assert auto_setup_job.auto_placement_approval_id == auto_approval.id
    assert auto_sync_job.auto_placement_approval_id == auto_approval.id
    assert manual_job.auto_placement_approval_id is None


def test_approve_rejects_on_policy_revision_drift(db, audit_path):
    context = _human_context(db)
    db.insert_project("proj1", "https://example.invalid/proj1.git", default_command="echo hi")
    policy_approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", allowed_servers=["worker-a"], config=_DP_CONFIG,
        audit_path=audit_path, request_context=context,
    )
    policy = _approve_dispatch_policy(db, policy_approval.id, audit_path, context)[
        "dispatch_policy"
    ]
    proposal = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert proposal is not None

    from app.approvals import request_dispatch_policy_update_approval

    update_approval = request_dispatch_policy_update_approval(
        db, "proj1", "p", allowed_servers=["worker-b"], config=_DP_CONFIG,
        audit_path=audit_path, request_context=context,
    )
    _approve_dispatch_policy(db, update_approval.id, audit_path, context)

    result = _approve_auto_placement(
        db, proposal.id, audit_path, context,
        server_configs={"worker-a": _server_cfg("worker-a")},
    )
    assert result["approval"].status == "rejected"
    assert "changed" in result["approval"].note


def test_approve_rejects_archived_policy(db, audit_path):
    context = _human_context(db)
    db.insert_project("proj1", "https://example.invalid/proj1.git", default_command="echo hi")
    policy_approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", allowed_servers=["worker-a"], config=_DP_CONFIG,
        audit_path=audit_path, request_context=context,
    )
    policy = _approve_dispatch_policy(db, policy_approval.id, audit_path, context)[
        "dispatch_policy"
    ]
    proposal = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert proposal is not None

    from app.approvals import request_dispatch_policy_archive_approval

    archive_approval = request_dispatch_policy_archive_approval(
        db, "proj1", "p", config=_DP_CONFIG, audit_path=audit_path, request_context=context
    )
    _approve_dispatch_policy(db, archive_approval.id, audit_path, context)

    result = _approve_auto_placement(
        db, proposal.id, audit_path, context,
        server_configs={"worker-a": _server_cfg("worker-a")},
    )
    assert result["approval"].status == "rejected"


def test_approve_rejects_disabled_server(db, audit_path):
    context = _human_context(db)
    db.insert_project("proj1", "https://example.invalid/proj1.git", default_command="echo hi")
    policy_approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", allowed_servers=["worker-a"], config=_DP_CONFIG,
        audit_path=audit_path, request_context=context,
    )
    policy = _approve_dispatch_policy(db, policy_approval.id, audit_path, context)[
        "dispatch_policy"
    ]
    proposal = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert proposal is not None

    result = _approve_auto_placement(
        db, proposal.id, audit_path, context,
        server_configs={"worker-a": _server_cfg("worker-a", enabled=False)},
    )
    assert result["approval"].status == "rejected"


def test_approve_rejects_on_command_sha_mismatch_after_tamper(db, audit_path):
    """No `run_profile_id`: the command is re-derived from
    `project.default_command` at approve time. Unlike an immutable Run
    Profile revision (which never changes once pinned by id — tampering
    that path would require corrupting the DB directly), the project's
    legacy `default_command` field genuinely can drift between the proposal
    being created and a human approving it (e.g. someone edits the project
    in the meantime); the approve branch must catch that drift via the
    command sha256 recorded in the proposal payload."""

    context = _human_context(db)
    db.insert_project(
        "proj1", "https://example.invalid/proj1.git", default_command="python train.py"
    )
    policy_approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", allowed_servers=["worker-a"], config=_DP_CONFIG,
        audit_path=audit_path, request_context=context,
    )
    policy = _approve_dispatch_policy(db, policy_approval.id, audit_path, context)[
        "dispatch_policy"
    ]
    proposal = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert proposal is not None

    # Tamper: the project's default_command changes between request and approve.
    db.update_project("proj1", default_command="python train_v2.py")

    result = _approve_auto_placement(
        db, proposal.id, audit_path, context,
        server_configs={"worker-a": _server_cfg("worker-a")},
    )
    assert result["approval"].status == "rejected"
    assert "no longer matches" in result["approval"].note


def test_approve_rejects_expired_policy(db, audit_path):
    context = _human_context(db)
    db.insert_project("proj1", "https://example.invalid/proj1.git", default_command="echo hi")
    future = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    policy_approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", allowed_servers=["worker-a"], valid_until=future,
        config=_DP_CONFIG, audit_path=audit_path, request_context=context,
    )
    policy = _approve_dispatch_policy(db, policy_approval.id, audit_path, context)[
        "dispatch_policy"
    ]
    proposal = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert proposal is not None

    # Simulate the policy having since expired by rewriting valid_until into
    # the past directly (approve() re-reads the *current* head from the DB).
    with db.cursor() as cur:
        cur.execute(
            "UPDATE dispatch_policies SET valid_until = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", policy.id),
        )

    result = _approve_auto_placement(
        db, proposal.id, audit_path, context,
        server_configs={"worker-a": _server_cfg("worker-a")},
    )
    assert result["approval"].status == "rejected"
    assert "expired" in result["approval"].note


def test_approve_fails_closed_when_disabled(db, audit_path):
    context = _human_context(db)
    db.insert_project("proj1", "https://example.invalid/proj1.git", default_command="echo hi")
    policy_approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", allowed_servers=["worker-a"], config=_DP_CONFIG,
        audit_path=audit_path, request_context=context,
    )
    policy = _approve_dispatch_policy(db, policy_approval.id, audit_path, context)[
        "dispatch_policy"
    ]
    proposal = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert proposal is not None

    from app.approvals import DispatchPolicyAdministrationDisabledError

    with pytest.raises(DispatchPolicyAdministrationDisabledError):
        _approve_auto_placement(
            db, proposal.id, audit_path, context,
            server_configs={"worker-a": _server_cfg("worker-a")},
            enabled=False,
        )
    assert db.get_approval(proposal.id).status == "pending"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_legacy_db_reopen_gains_auto_placement_approval_id_column(tmp_path):
    from tests.test_db_migration import _OLD_JOBS_SCHEMA

    db_path = tmp_path / "legacy_jobs.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript(_OLD_JOBS_SCHEMA)
    raw.execute(
        "INSERT INTO jobs (type, command, created_at) VALUES ('adhoc', 'echo hi', 'x')"
    )
    raw.commit()
    raw.close()

    db = Database(str(db_path))
    try:
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "auto_placement_approval_id" in cols
        job = db.get_job(1)
        assert job.auto_placement_approval_id is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# AppState.auto_placement_loop() best-effort tick
# ---------------------------------------------------------------------------


def test_auto_placement_tick_creates_pending_approvals(api_client):
    client, main_module = api_client
    app_state = main_module.app_state
    app_state.config.dispatch_policy_v1_enabled = True
    app_state.config.auto_placement_proposals_enabled = True

    app_state.db.insert_project(
        "proj1", "https://example.invalid/proj1.git", default_command="python train.py"
    )
    context = _human_context(app_state.db)
    policy_approval = request_dispatch_policy_create_approval(
        app_state.db, "proj1", "p", allowed_servers=["worker-a"],
        config=app_state.config, audit_path=app_state.config.audit_path,
        request_context=context,
    )
    asyncio.run(
        approve(
            app_state.db, policy_approval.id,
            app_state=SimpleNamespace(config=app_state.config),
            audit_path=app_state.config.audit_path, request_context=context,
        )
    )

    from app.config import ServerConfig as _ServerConfig

    app_state.server_configs["worker-a"] = _ServerConfig(
        name="worker-a", host="1.2.3.4", user="worker", key="~/.ssh/id_rsa", gpu=True
    )
    app_state.server_states["worker-a"] = ServerState(
        name="worker-a", online=True, gpus=[GpuReading(5.0, 100, 8192)]
    )

    app_state._auto_placement_tick()

    pending = app_state.db.list_approvals(status="pending", kind="auto_placement")
    assert len(pending) == 1
    assert pending[0].payload["server"] == "worker-a"


def test_auto_placement_tick_db_failure_only_logs(api_client, monkeypatch):
    client, main_module = api_client
    app_state = main_module.app_state
    app_state.config.dispatch_policy_v1_enabled = True
    app_state.config.auto_placement_proposals_enabled = True

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(app_state.db, "list_projects", _raise)

    # Must not raise — the loop's caller wraps this in try/except, but the
    # tick method itself is also exercised directly here to pin the
    # best-effort contract at the unit level (mirrors
    # tests/test_server_observations.py's monitor-failure test style).
    with pytest.raises(RuntimeError):
        app_state._auto_placement_tick()


# ---------------------------------------------------------------------------
# Slice 5 — INV-APPROVAL-4b policy-scoped auto-decision
# (.claude/skills/dispatcher-domain/references/invariants.md)
# ---------------------------------------------------------------------------


def _kill_switch_config(kill_switch: bool) -> SimpleNamespace:
    return SimpleNamespace(
        dispatch_policy_v1_enabled=True,
        auto_placement_cooldown_sec=3600,
        auto_placement_kill_switch=kill_switch,
    )


def _make_policy_and_proposal(db, audit_path, context, *, default_command="echo hi", **overrides):
    db.insert_project(
        "proj1", "https://example.invalid/proj1.git", default_command=default_command
    )
    kwargs = {"allowed_servers": ["worker-a"], "max_concurrent_placements": 1}
    kwargs.update(overrides)
    policy_approval = request_dispatch_policy_create_approval(
        db, "proj1", "p", config=_DP_CONFIG, audit_path=audit_path,
        request_context=context, **kwargs,
    )
    policy = _approve_dispatch_policy(db, policy_approval.id, audit_path, context)[
        "dispatch_policy"
    ]
    proposal = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    assert proposal is not None
    return policy, proposal


def test_auto_decide_approves_in_scope_proposal_when_kill_switch_off(db, audit_path):
    context = _human_context(db)
    policy, proposal = _make_policy_and_proposal(db, audit_path, context)

    result = asyncio.run(
        maybe_auto_decide_placement(
            db,
            db.get_approval(proposal.id),
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_kill_switch_config(False)),
            audit_path=audit_path,
        )
    )

    assert result is not None
    assert result["approval"].status == "approved"
    assert result["approval"].decision_mechanism == f"policy-{policy.id}-r{policy.revision}"
    assert result["approval"].decision_actor_id is None  # never a fabricated human
    assert result["job"] is not None
    assert result["job"].auto_placement_approval_id == proposal.id

    # Audit trail explicitly names the system actor for this decision.
    audit_lines = Path(audit_path).read_text(encoding="utf-8").splitlines()
    decision_records = [
        json.loads(line)
        for line in audit_lines
        if json.loads(line).get("action") == "auto_placement_policy_decision"
    ]
    assert len(decision_records) == 1
    assert decision_records[0]["actor"]["id"] == "system"
    assert decision_records[0]["actor"]["kind"] == "system"
    assert decision_records[0]["params"]["decision_mechanism"] == (
        f"policy-{policy.id}-r{policy.revision}"
    )


def test_auto_decide_never_approves_when_kill_switch_on_by_default(db, audit_path):
    context = _human_context(db)
    _policy_row, proposal = _make_policy_and_proposal(db, audit_path, context)

    result = asyncio.run(
        maybe_auto_decide_placement(
            db,
            db.get_approval(proposal.id),
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_kill_switch_config(True)),
            audit_path=audit_path,
        )
    )

    assert result is None
    assert db.get_approval(proposal.id).status == "pending"


def test_auto_decide_out_of_scope_policy_revision_drift_stays_pending(db, audit_path):
    context = _human_context(db)
    _policy_row, proposal = _make_policy_and_proposal(db, audit_path, context)

    from app.approvals import request_dispatch_policy_update_approval

    update_approval = request_dispatch_policy_update_approval(
        db, "proj1", "p", allowed_servers=["worker-b"], config=_DP_CONFIG,
        audit_path=audit_path, request_context=context,
    )
    _approve_dispatch_policy(db, update_approval.id, audit_path, context)

    result = asyncio.run(
        maybe_auto_decide_placement(
            db,
            db.get_approval(proposal.id),
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_kill_switch_config(False)),
            audit_path=audit_path,
        )
    )

    assert result is None
    assert db.get_approval(proposal.id).status == "pending"


def test_auto_decide_out_of_scope_server_removed_from_allowed_servers_stays_pending(
    db, audit_path
):
    context = _human_context(db)
    policy, proposal = _make_policy_and_proposal(db, audit_path, context)

    # Mutate allowed_servers directly (same revision) to isolate this
    # condition from the revision-drift check above.
    with db.cursor() as cur:
        cur.execute(
            "UPDATE dispatch_policies SET allowed_servers = ? WHERE id = ?",
            (json.dumps(["worker-b"]), policy.id),
        )

    result = asyncio.run(
        maybe_auto_decide_placement(
            db,
            db.get_approval(proposal.id),
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_kill_switch_config(False)),
            audit_path=audit_path,
        )
    )

    assert result is None
    assert db.get_approval(proposal.id).status == "pending"


def test_auto_decide_out_of_scope_expired_policy_stays_pending(db, audit_path):
    context = _human_context(db)
    policy, proposal = _make_policy_and_proposal(db, audit_path, context)

    with db.cursor() as cur:
        cur.execute(
            "UPDATE dispatch_policies SET valid_until = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", policy.id),
        )

    result = asyncio.run(
        maybe_auto_decide_placement(
            db,
            db.get_approval(proposal.id),
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_kill_switch_config(False)),
            audit_path=audit_path,
        )
    )

    assert result is None
    assert db.get_approval(proposal.id).status == "pending"


def test_auto_decide_out_of_scope_at_max_concurrent_placements_stays_pending(
    db, audit_path
):
    context = _human_context(db)
    policy, proposal = _make_policy_and_proposal(db, audit_path, context)

    # Manually approve one placement so the policy is already at its cap.
    asyncio.run(
        approve(
            db, proposal.id,
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_DP_CONFIG),
            audit_path=audit_path, request_context=context,
        )
    )
    assert db.count_active_auto_placement_jobs(policy.id) >= policy.max_concurrent_placements

    second_proposal = request_auto_placement_approval(
        db, policy=policy, server_name="worker-a", config=_DP_CONFIG, audit_path=audit_path
    )
    # Cooldown/pending-duplicate guards should already skip this, but even if
    # a proposal existed, the auto-decider must independently refuse.
    if second_proposal is None:
        # Simulate a proposal that slipped through (e.g. cap raised then
        # lowered again) by directly inserting one for this test's purpose.
        second_proposal = db.get_approval(
            db.insert_approval(kind="auto_placement", payload=dict(proposal.payload))
        )

    result = asyncio.run(
        maybe_auto_decide_placement(
            db,
            second_proposal,
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_kill_switch_config(False)),
            audit_path=audit_path,
        )
    )

    assert result is None
    assert db.get_approval(second_proposal.id).status == "pending"


def test_auto_decide_out_of_scope_command_sha_drift_stays_pending(db, audit_path):
    context = _human_context(db)
    _policy_row, proposal = _make_policy_and_proposal(db, audit_path, context)

    db.update_project("proj1", default_command="echo changed")

    result = asyncio.run(
        maybe_auto_decide_placement(
            db,
            db.get_approval(proposal.id),
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_kill_switch_config(False)),
            audit_path=audit_path,
        )
    )

    assert result is None
    assert db.get_approval(proposal.id).status == "pending"


def test_auto_decide_stops_immediately_after_policy_archive(db, audit_path):
    context = _human_context(db)
    policy, proposal = _make_policy_and_proposal(db, audit_path, context)

    from app.approvals import request_dispatch_policy_archive_approval

    archive_approval = request_dispatch_policy_archive_approval(
        db, "proj1", "p", config=_DP_CONFIG, audit_path=audit_path, request_context=context
    )
    _approve_dispatch_policy(db, archive_approval.id, audit_path, context)

    result = asyncio.run(
        maybe_auto_decide_placement(
            db,
            db.get_approval(proposal.id),
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_kill_switch_config(False)),
            audit_path=audit_path,
        )
    )

    assert result is None
    assert db.get_approval(proposal.id).status == "pending"


def test_manual_approve_still_works_after_auto_decider_leaves_it_pending(db, audit_path):
    context = _human_context(db)
    _policy_row, proposal = _make_policy_and_proposal(db, audit_path, context)

    # Kill switch on: auto-decider must not touch it.
    result = asyncio.run(
        maybe_auto_decide_placement(
            db,
            db.get_approval(proposal.id),
            server_configs={"worker-a": _server_cfg("worker-a")},
            app_state=SimpleNamespace(config=_kill_switch_config(True)),
            audit_path=audit_path,
        )
    )
    assert result is None
    assert db.get_approval(proposal.id).status == "pending"

    # A human can still approve the very same pending proposal normally.
    manual_result = _approve_auto_placement(
        db, proposal.id, audit_path, context,
        server_configs={"worker-a": _server_cfg("worker-a")},
    )
    assert manual_result["approval"].status == "approved"
    assert manual_result["approval"].decision_mechanism == "manual"


def test_maybe_auto_approve_still_returns_none_for_auto_placement_after_slice5():
    """INV-APPROVAL-4 pin: `maybe_auto_approve()`'s kind gate is unaffected
    by the new Slice 5 policy-scoped mechanism."""
    result = asyncio.run(
        maybe_auto_approve(
            SimpleNamespace(),
            SimpleNamespace(kind="auto_placement", id=1),
            source="web",
            rules=[{"kind": "any", "action": "approve"}],
        )
    )
    assert result is None


def test_auto_placement_loop_calls_auto_decider_only_when_kill_switch_off(api_client):
    client, main_module = api_client
    app_state = main_module.app_state
    app_state.config.dispatch_policy_v1_enabled = True

    context = _human_context(app_state.db)
    policy, proposal = _make_policy_and_proposal(
        app_state.db, app_state.config.audit_path, context
    )
    app_state.server_configs["worker-a"] = ServerConfig(
        name="worker-a", host="1.2.3.4", user="worker", key="~/.ssh/id_rsa", gpu=True
    )

    # Kill switch on (default): the loop's decision tick must not approve.
    app_state.config.auto_placement_kill_switch = True
    app_state._auto_decide_pending_placements()
    assert app_state.db.get_approval(proposal.id).status == "pending"

    # Kill switch off: the same tick now approves the in-scope proposal.
    app_state.config.auto_placement_kill_switch = False
    app_state._auto_decide_pending_placements()
    assert app_state.db.get_approval(proposal.id).status == "approved"
