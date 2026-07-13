"""Focused tests for fail-open, observational authorization shadowing."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import app.authorization_shadow as shadow
from app.authorization import Action, ResourceScope
from app.audit import read_audit
from app.db import (
    Approval,
    Dataset,
    Job,
    Project,
    VALID_APPROVAL_KINDS,
)
from app.identity import (
    Actor,
    ActorType,
    ProjectMembership,
    ProjectRole,
    RequestContext,
)


PROJECT_A = "11111111-1111-4111-8111-111111111111"
PROJECT_B = "22222222-2222-4222-8222-222222222222"
SERVICE_ACTOR = "33333333-3333-4333-8333-333333333333"
MEMBER_ACTOR = "44444444-4444-4444-8444-444444444444"


def _config(mode: str = "shadow"):
    return SimpleNamespace(authorization_mode=mode)


def _human_context(
    *,
    actor_id: str = "human-1",
    platform_admin: bool = False,
    memberships: tuple[ProjectMembership, ...] = (),
) -> RequestContext:
    return RequestContext(
        actor=Actor(
            id=actor_id,
            actor_type=ActorType.HUMAN,
            display_name=actor_id,
            platform_admin=platform_admin,
        ),
        authentication_method="session",
        project_memberships=memberships,
    )


def _service_context(*, scopes: tuple[str, ...] = ()) -> RequestContext:
    return RequestContext(
        actor=Actor(
            id="service-1",
            actor_type=ActorType.SERVICE,
            display_name="service-1",
        ),
        authentication_method="service_token",
        service_token_id="private-token-id",
        service_scopes=scopes,
    )


def _project(
    name: str,
    project_id: str,
    *,
    dataset_name: str | None = None,
    dataset_version: str | None = None,
) -> Project:
    return Project(
        name=name,
        repo_or_path=f"/srv/{name}",
        id=project_id,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
    )


def _job(job_id: int, project: str | None) -> Job:
    return Job(
        id=job_id,
        type="adhoc",
        project=project,
        command="true",
        require_tag=None,
        pin_server=None,
    )


class ReadOnlyDB:
    """Small resolver fake with no mutation methods."""

    def __init__(
        self,
        *,
        projects=(),
        jobs=(),
        datasets=(),
        approvals=(),
        coding_runs=(),
    ):
        self.projects = list(projects)
        self.jobs = list(jobs)
        self.datasets = list(datasets)
        self.approvals = list(approvals)
        self.coding_runs = list(coding_runs)
        self.calls: list[tuple[str, object]] = []

    def get_project(self, identifier):
        self.calls.append(("get_project", identifier))
        return next(
            (
                project
                for project in self.projects
                if identifier in (project.name, project.id)
            ),
            None,
        )

    def list_projects(self):
        self.calls.append(("list_projects", None))
        return list(self.projects)

    def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        return next((job for job in self.jobs if job.id == job_id), None)

    def list_jobs(self, status=None, project=None):
        self.calls.append(("list_jobs", (status, project)))
        return [
            job
            for job in self.jobs
            if (status is None or job.status == status)
            and (project is None or job.project == project)
        ]

    def get_dataset(self, name, version):
        self.calls.append(("get_dataset", (name, version)))
        return next(
            (
                dataset
                for dataset in self.datasets
                if (dataset.name, dataset.version) == (name, version)
            ),
            None,
        )

    def list_datasets(self):
        self.calls.append(("list_datasets", None))
        return list(self.datasets)

    def get_approval(self, approval_id):
        self.calls.append(("get_approval", approval_id))
        return next(
            (approval for approval in self.approvals if approval.id == approval_id),
            None,
        )

    def list_approvals(self, status=None, kind=None):
        self.calls.append(("list_approvals", (status, kind)))
        return [
            approval
            for approval in self.approvals
            if (status is None or approval.status == status)
            and (kind is None or approval.kind == kind)
        ]

    def get_coding_run(self, coding_run_id):
        self.calls.append(("get_coding_run", coding_run_id))
        return next(
            (run for run in self.coding_runs if run.id == coding_run_id),
            None,
        )

    def list_coding_runs(self, status=None, project=None, limit=50):
        self.calls.append(("list_coding_runs", (status, project, limit)))
        return [
            run
            for run in self.coding_runs
            if (status is None or run.status == status)
            and (project is None or run.project == project)
        ][:limit]


class ExplodingDB:
    def __getattr__(self, name):
        raise AssertionError(f"off mode touched database member {name}")


def _collect(
    db,
    context,
    *,
    action=Action.PLATFORM_VIEW,
    resource_kind="platform",
    values=None,
    interface_kind="route",
    interface_name="GET /servers",
    mode="shadow",
):
    return shadow.collect_shadow_evidence(
        mode=mode,
        db=db,
        context=context,
        action=action,
        resource_kind=resource_kind,
        values=values or {},
        interface_kind=interface_kind,
        interface_name=interface_name,
    )


def _request(
    method: str,
    route: str,
    *,
    path: str | None = None,
    path_params=None,
    query_string: bytes = b"",
    body: dict | None = None,
) -> Request:
    encoded = json.dumps(body).encode() if body is not None else b""
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    actual_path = path or route
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": actual_path,
            "raw_path": actual_path.encode(),
            "query_string": query_string,
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 123),
            "server": ("test", 80),
            "route": SimpleNamespace(path=route),
            "path_params": dict(path_params or {}),
        },
        receive,
    )
    return request


def test_off_mode_returns_before_route_db_evaluator_or_audit(monkeypatch, tmp_path):
    class ExplodingConnection:
        @property
        def scope(self):
            raise AssertionError("off mode inspected the HTTP route")

    def explode(*args, **kwargs):
        raise AssertionError("off mode evaluated or audited")

    monkeypatch.setattr(shadow, "evaluate_authorization", explode)
    monkeypatch.setattr(shadow, "append_audit", explode)
    path = tmp_path / "audit.jsonl"

    assert (
        asyncio.run(
            shadow.observe_http_authorization(
                ExplodingConnection(),
                db=ExplodingDB(),
                config=_config("off"),
                audit_path=str(path),
            )
        )
        == ()
    )
    assert shadow.observe_local_tool_authorization(
        "job_detail",
        {"job_id": 1},
        db=ExplodingDB(),
        config=_config("off"),
        audit_path=str(path),
    ) == ()
    assert not path.exists()


def test_allowed_observation_has_no_denial_or_audit(tmp_path):
    evidence = _collect(ReadOnlyDB(), _human_context(platform_admin=True))
    shadow.emit_shadow_evidence(evidence, audit_path=str(tmp_path / "audit.jsonl"))

    assert evidence == ()
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize(
    ("context", "reason", "principal_kind"),
    [
        (RequestContext(), "denied_anonymous", "anonymous"),
        (_service_context(), "denied_service_scope_missing", "service"),
    ],
)
def test_anonymous_and_service_would_deny_are_observational(
    tmp_path, context, reason, principal_kind
):
    path = tmp_path / f"{principal_kind}.jsonl"
    evidence = _collect(ReadOnlyDB(), context)
    shadow.emit_shadow_evidence(evidence, audit_path=str(path))

    assert len(evidence) == 1
    assert evidence[0].audit_action == "authorization_shadow_denied"
    assert evidence[0].params == {
        "action": "platform.view",
        "resource": "platform",
        "project": None,
        "route": "GET /servers",
        "tool": None,
        "reason": reason,
        "principal_kind": principal_kind,
        "shadow_mode": True,
    }
    record = read_audit(path)[0]
    assert record["result"] == "would_deny"
    if principal_kind == "service":
        assert record["actor"] == {
            "id": "service-1",
            "kind": "service",
            "authentication": "service_token",
        }
        serialized = json.dumps(record)
        assert "private-token-id" not in serialized
    else:
        assert "actor" not in record


def test_projectless_job_is_explicitly_global_not_unresolved_or_project_scoped():
    db = ReadOnlyDB(jobs=[_job(7, None)])
    evidence = _collect(
        db,
        _human_context(),
        action=Action.PROJECT_VIEW,
        resource_kind="job",
        values={"job_id": 7},
        interface_name="GET /jobs/{job_id}",
    )

    assert len(evidence) == 1
    assert evidence[0].audit_action == "authorization_shadow_denied"
    assert evidence[0].params["resource"] == "job:7"
    assert evidence[0].params["project"] is None
    assert evidence[0].params["reason"] == "denied_platform_admin_required"


def test_dataset_with_multiple_projects_uses_allow_if_any_and_evaluates_all(
    monkeypatch,
):
    dataset = Dataset("shared", "v1", 1, "/data", {})
    project_a = _project(
        "a", PROJECT_A, dataset_name=dataset.name, dataset_version=dataset.version
    )
    project_b = _project(
        "b", PROJECT_B, dataset_name=dataset.name, dataset_version=dataset.version
    )
    context = _human_context(
        memberships=(
            ProjectMembership(
                project_id=PROJECT_B,
                actor_id="human-1",
                role=ProjectRole.VIEWER,
            ),
        )
    )
    evaluated_projects = []
    real_evaluator = shadow.evaluate_authorization

    def recording_evaluator(*args, **kwargs):
        evaluated_projects.append(kwargs.get("project_id"))
        return real_evaluator(*args, **kwargs)

    monkeypatch.setattr(shadow, "evaluate_authorization", recording_evaluator)
    evidence = _collect(
        ReadOnlyDB(projects=[project_a, project_b], datasets=[dataset]),
        context,
        action=Action.PROJECT_VIEW,
        resource_kind="dataset",
        values={"name": "shared", "version": "v1"},
        interface_name="GET /datasets/{name}/{version}/card",
    )

    assert evidence == ()
    assert evaluated_projects == [PROJECT_A, PROJECT_B]


def test_unresolved_resource_is_error_and_never_promoted_to_global():
    evidence = _collect(
        ReadOnlyDB(),
        _human_context(platform_admin=True),
        action=Action.PROJECT_VIEW,
        resource_kind="job",
        values={"job_id": 404},
        interface_name="GET /jobs/{job_id}",
    )

    assert len(evidence) == 1
    assert evidence[0].audit_action == "authorization_shadow_error"
    assert evidence[0].params["stage"] == "resolution"
    assert evidence[0].params["reason"] == "resource_not_found"
    assert evidence[0].params["resource"] == "job:404"
    assert evidence[0].params["project"] is None


def test_evaluator_exception_becomes_safe_error_and_never_escapes(monkeypatch):
    def broken_evaluator(*args, **kwargs):
        raise RuntimeError("raw-secret-must-not-be-logged")

    monkeypatch.setattr(shadow, "evaluate_authorization", broken_evaluator)
    evidence = _collect(ReadOnlyDB(), RequestContext())

    assert len(evidence) == 1
    assert evidence[0].audit_action == "authorization_shadow_error"
    assert evidence[0].params["stage"] == "evaluation"
    assert evidence[0].params["exception_type"] == "RuntimeError"
    assert "raw-secret" not in json.dumps(evidence[0].params)


def test_audit_exception_attempts_safe_error_then_is_swallowed(monkeypatch, tmp_path):
    calls = []

    def broken_append(action, params, **kwargs):
        calls.append((action, dict(params)))
        raise RuntimeError("raw-audit-secret")

    monkeypatch.setattr(shadow, "append_audit", broken_append)
    evidence = _collect(ReadOnlyDB(), RequestContext())

    shadow.emit_shadow_evidence(evidence, audit_path=str(tmp_path / "audit.jsonl"))

    assert [action for action, _ in calls] == [
        "authorization_shadow_denied",
        "authorization_shadow_error",
    ]
    error = calls[1][1]
    assert error["stage"] == "audit"
    assert error["exception_type"] == "RuntimeError"
    assert "raw-audit-secret" not in json.dumps(error)


def test_collection_is_observed_per_resource_without_filtering_source_rows():
    projects = [_project("a", PROJECT_A), _project("b", PROJECT_B)]
    original = list(projects)
    db = ReadOnlyDB(projects=projects)
    context = _human_context(
        memberships=(
            ProjectMembership(
                project_id=PROJECT_A,
                actor_id="human-1",
                role=ProjectRole.VIEWER,
            ),
        )
    )

    evidence = _collect(
        db,
        context,
        action=Action.PROJECT_VIEW,
        resource_kind="project_collection",
        interface_name="GET /projects",
    )

    assert projects == original
    assert db.projects == original
    assert [item.params["project"] for item in evidence] == [PROJECT_B]
    assert all(item.audit_action == "authorization_shadow_denied" for item in evidence)


def test_http_observer_uses_canonical_route_template_and_json_body(tmp_path):
    request = _request(
        "POST",
        "/jobs",
        path="/jobs",
        query_string=b"source=spoofed",
        body={"project": None, "command": "do-not-log-this"},
    )
    path = tmp_path / "audit.jsonl"

    evidence = asyncio.run(
        shadow.observe_http_authorization(
            request,
            db=ExplodingDB(),
            config=_config(),
            audit_path=str(path),
            context=RequestContext(),
        )
    )

    assert len(evidence) == 1
    assert evidence[0].params["route"] == "POST /jobs"
    assert evidence[0].params["resource"] == "job_request:projectless"
    assert evidence[0].params["reason"] == "denied_anonymous"
    serialized = path.read_text()
    assert "do-not-log-this" not in serialized
    assert "spoofed" not in serialized


def test_http_observer_extracts_path_params_against_template():
    request = _request(
        "GET",
        "/jobs/{job_id}",
        path="/jobs/9",
        path_params={"job_id": 9},
    )
    evidence = asyncio.run(
        shadow.collect_http_shadow_evidence(
            request,
            db=ReadOnlyDB(jobs=[_job(9, None)]),
            config=_config(),
            context=_human_context(),
        )
    )

    assert len(evidence) == 1
    assert evidence[0].params["route"] == "GET /jobs/{job_id}"
    assert evidence[0].params["resource"] == "job:9"


def test_local_tool_observer_uses_catalog_and_standard_actor_envelope(tmp_path):
    path = tmp_path / "audit.jsonl"
    evidence = shadow.observe_local_tool_authorization(
        "job_detail",
        {"job_id": 12},
        db=ReadOnlyDB(jobs=[_job(12, None)]),
        config=_config(),
        audit_path=str(path),
        context=_service_context(),
    )

    assert len(evidence) == 1
    assert evidence[0].params["route"] is None
    assert evidence[0].params["tool"] == "job_detail"
    assert evidence[0].params["resource"] == "job:12"
    assert read_audit(path)[0]["actor"] == {
        "id": "service-1",
        "kind": "service",
        "authentication": "service_token",
    }


def test_every_current_non_enqueue_stop_approval_is_high_risk():
    assert shadow.HIGH_RISK_APPROVAL_KINDS == frozenset(
        VALID_APPROVAL_KINDS - {"enqueue", "stop"}
    )


def test_high_risk_platform_approval_self_decision_would_deny_admin():
    approval = Approval(
        id=31,
        kind="inventory_scan",
        payload={"server": "gpu-a", "project_roots": ["/srv/projects"]},
        requester_actor_id="human-1",
    )
    evidence = _collect(
        ReadOnlyDB(approvals=[approval]),
        _human_context(platform_admin=True),
        action=Action.APPROVAL_DECIDE,
        resource_kind="approval",
        values={"approval_id": approval.id},
        interface_name="POST /approve/{approval_id}",
    )

    assert len(evidence) == 1
    assert evidence[0].params["reason"] == "denied_high_risk_self_decision"
    assert evidence[0].params["resource"] == "approval:31"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (
            "project_membership_upsert",
            {
                "project_id": PROJECT_A,
                "actor_id": MEMBER_ACTOR,
                "role": "admin",
            },
        ),
        (
            "project_membership_remove",
            {"project_id": PROJECT_A, "actor_id": MEMBER_ACTOR},
        ),
    ],
)
def test_membership_approval_shadow_uses_canonical_project_id(kind, payload):
    approval = Approval(
        id=32,
        kind=kind,
        payload=payload,
        requester_actor_id="human-1",
    )
    db = ReadOnlyDB(
        projects=[_project("alpha", PROJECT_A)],
        approvals=[approval],
    )

    targets, issues = shadow.resolve_shadow_targets(
        db,
        resource_kind="approval",
        values={"approval_id": approval.id},
    )

    assert issues == ()
    assert targets == (
        shadow.ShadowTarget(
            resource="approval:32",
            scope=ResourceScope.PROJECT,
            project_id=PROJECT_A,
            requester_actor_id="human-1",
            high_risk=True,
        ),
    )
    assert ("get_project", PROJECT_A) in db.calls


def test_service_identity_admin_approval_denial_is_shadow_evidence_only():
    approval = Approval(
        id=33,
        kind="service_token_issue",
        payload={
            "service_account_actor_id": SERVICE_ACTOR,
            "label": None,
            "scopes": [],
            "expires_at": "2030-01-01T00:00:00+00:00",
        },
    )

    evidence = _collect(
        ReadOnlyDB(approvals=[approval]),
        _service_context(scopes=(Action.APPROVAL_DECIDE.value,)),
        action=Action.APPROVAL_DECIDE,
        resource_kind="approval",
        values={"approval_id": approval.id},
        interface_name="POST /approve/{approval_id}",
    )

    assert len(evidence) == 1
    assert evidence[0].audit_action == "authorization_shadow_denied"
    assert evidence[0].params["resource"] == "approval:33"
    assert evidence[0].params["reason"] == "denied_service_action"


@pytest.mark.parametrize(
    ("actor_type", "actor_id", "authentication"),
    [
        (ActorType.HUMAN, "human-private", "session"),
        (ActorType.SERVICE, "service-private", "service_token"),
    ],
)
def test_authenticated_denial_has_exact_actor_and_no_private_or_source_fields(
    tmp_path,
    actor_type,
    actor_id,
    authentication,
):
    context = RequestContext(
        actor=Actor(
            id=actor_id,
            actor_type=actor_type,
            display_name="RAW-DISPLAY-NAME",
            email="RAW-EMAIL@example.invalid",
        ),
        authentication_method=authentication,
        service_token_id=("RAW-TOKEN-ID" if actor_type is ActorType.SERVICE else None),
        service_scopes=(
            {Action.PLATFORM_VIEW.value, "RAW-PRIVATE-SCOPE"}
            if actor_type is ActorType.SERVICE
            else ()
        ),
    )
    path = tmp_path / f"{actor_type.value}.jsonl"

    evidence = _collect(
        ReadOnlyDB(),
        context,
        values={"source": "RAW-SOURCE", "credential": "RAW-CREDENTIAL"},
    )
    shadow.emit_shadow_evidence(evidence, audit_path=str(path))

    assert len(evidence) == 1
    assert evidence[0].params == {
        "action": "platform.view",
        "resource": "platform",
        "project": None,
        "route": "GET /servers",
        "tool": None,
        "reason": "denied_platform_admin_required",
        "principal_kind": actor_type.value,
        "shadow_mode": True,
    }
    record = read_audit(path)[0]
    assert record["result"] == "would_deny"
    assert record["actor"] == {
        "id": actor_id,
        "kind": actor_type.value,
        "authentication": authentication,
    }
    serialized = json.dumps(record, sort_keys=True)
    for forbidden in (
        "RAW-DISPLAY-NAME",
        "RAW-EMAIL",
        "RAW-TOKEN-ID",
        "RAW-PRIVATE-SCOPE",
        "RAW-SOURCE",
        "RAW-CREDENTIAL",
    ):
        assert forbidden not in serialized


def test_orphan_dataset_is_explicitly_global_not_unresolved_or_project_scoped():
    dataset = Dataset("orphan", "v1", 1, "/data/orphan", {})
    db = ReadOnlyDB(datasets=[dataset])

    targets, issues = shadow.resolve_shadow_targets(
        db,
        resource_kind="dataset",
        values={"name": "orphan", "version": "v1"},
    )
    evidence = _collect(
        db,
        _human_context(),
        action=Action.PROJECT_VIEW,
        resource_kind="dataset",
        values={"name": "orphan", "version": "v1"},
        interface_name="/datasets/{name}/{version}/card",
    )

    assert issues == ()
    assert targets == (
        shadow.ShadowTarget("dataset:orphan@v1", ResourceScope.GLOBAL),
    )
    assert len(evidence) == 1
    assert evidence[0].params["project"] is None
    assert evidence[0].params["reason"] == "denied_platform_admin_required"


def test_multi_project_dataset_emits_binding_denials_only_when_all_bindings_deny():
    dataset = Dataset("shared", "v1", 1, "/data/shared", {})
    projects = [
        _project(
            "a", PROJECT_A, dataset_name=dataset.name, dataset_version=dataset.version
        ),
        _project(
            "b", PROJECT_B, dataset_name=dataset.name, dataset_version=dataset.version
        ),
    ]

    evidence = _collect(
        ReadOnlyDB(projects=projects, datasets=[dataset]),
        _human_context(),
        action=Action.PROJECT_VIEW,
        resource_kind="dataset",
        values={"name": "shared", "version": "v1"},
        interface_name="/datasets/{name}/{version}/card",
    )

    assert len(evidence) == 2
    assert {item.params["project"] for item in evidence} == {PROJECT_A, PROJECT_B}
    assert {item.params["reason"] for item in evidence} == {
        "denied_project_membership_missing"
    }
    assert all(item.audit_action == "authorization_shadow_denied" for item in evidence)


def test_resolver_exception_becomes_safe_error_and_never_escapes(monkeypatch):
    class PrivateResolverError(RuntimeError):
        pass

    def broken_resolver(*args, **kwargs):
        raise PrivateResolverError("RAW-RESOLVER-SECRET")

    monkeypatch.setattr(shadow, "resolve_shadow_targets", broken_resolver)

    evidence = _collect(
        ExplodingDB(),
        _human_context(),
        action=Action.PROJECT_VIEW,
        resource_kind="project",
        values={"name": "RAW-PROJECT-PAYLOAD"},
        interface_name="/projects/{name}/detail",
    )

    assert len(evidence) == 1
    assert evidence[0].audit_action == "authorization_shadow_error"
    assert evidence[0].params == {
        "action": "project.view",
        "resource": "project",
        "project": None,
        "route": "/projects/{name}/detail",
        "tool": None,
        "reason": "shadow_observation_error",
        "principal_kind": "human",
        "shadow_mode": True,
        "stage": "resolution",
        "exception_type": "PrivateResolverError",
    }
    serialized = json.dumps(evidence[0].params, sort_keys=True)
    assert "RAW-RESOLVER-SECRET" not in serialized
    assert "RAW-PROJECT-PAYLOAD" not in serialized
    assert "message" not in evidence[0].params


@pytest.mark.parametrize(
    "resource_kind",
    [
        "project_collection",
        "job_collection",
        "coding_run_collection",
        "dataset_collection",
        "approval_collection",
    ],
)
def test_empty_collections_have_no_synthetic_target_denial_or_error(resource_kind):
    db = ReadOnlyDB()

    targets, issues = shadow.resolve_shadow_targets(
        db,
        resource_kind=resource_kind,
        values={},
    )
    evidence = _collect(
        db,
        RequestContext(),
        action=Action.PROJECT_VIEW,
        resource_kind=resource_kind,
        interface_name=f"/{resource_kind}",
    )

    assert targets == ()
    assert issues == ()
    assert evidence == ()


def test_persisted_project_job_coding_dataset_and_approval_resolvers(db):
    project_id = db.insert_project(
        "alpha",
        "/projects/alpha",
        dataset_name="images",
        dataset_version="v1",
    )
    db.insert_dataset("images", "v1", 1, "/datasets/images", {"files": []})
    requester = db.insert_actor(
        actor_id="persisted-requester",
        actor_type=ActorType.HUMAN,
        display_name="Persisted Requester",
    )
    approval_id = db.insert_approval(
        "apply_patch",
        {"project": "alpha", "patch": "safe"},
        requester_actor_id=requester.id,
    )
    job_id = db.insert_job(command="true", project="alpha")
    coding_run_id = db.insert_coding_run(
        approval_id=approval_id,
        project="alpha",
        runner_server="runner",
        instruction="safe",
    )
    cases = (
        (
            "project",
            {"name": "alpha"},
            shadow.ShadowTarget(f"project:{project_id}", ResourceScope.PROJECT, project_id),
        ),
        (
            "job",
            {"job_id": job_id},
            shadow.ShadowTarget(f"job:{job_id}", ResourceScope.PROJECT, project_id),
        ),
        (
            "coding_run",
            {"coding_run_id": coding_run_id},
            shadow.ShadowTarget(
                f"coding_run:{coding_run_id}", ResourceScope.PROJECT, project_id
            ),
        ),
        (
            "dataset",
            {"name": "images", "version": "v1"},
            shadow.ShadowTarget("dataset:images@v1", ResourceScope.PROJECT, project_id),
        ),
        (
            "approval",
            {"approval_id": approval_id},
            shadow.ShadowTarget(
                f"approval:{approval_id}",
                ResourceScope.PROJECT,
                project_id,
                requester_actor_id=requester.id,
                high_risk=True,
            ),
        ),
    )

    for resource_kind, values, expected in cases:
        targets, issues = shadow.resolve_shadow_targets(
            db,
            resource_kind=resource_kind,
            values=values,
        )
        assert issues == (), resource_kind
        assert targets == (expected,), resource_kind


def test_global_only_action_bypasses_resource_database_resolution():
    evidence = _collect(
        ExplodingDB(),
        RequestContext(),
        action=Action.PLATFORM_MANAGE,
        resource_kind="dataset",
        values={"name": "does-not-matter", "version": "v1"},
        interface_name="PATCH /datasets/{name}/{version}/card",
    )

    assert len(evidence) == 1
    assert evidence[0].audit_action == "authorization_shadow_denied"
    assert evidence[0].params["resource"] == "dataset"
    assert evidence[0].params["reason"] == "denied_anonymous"


def test_supported_resource_kinds_match_resolver_branches():
    assert shadow.SUPPORTED_RESOURCE_KINDS == frozenset(
        {
            "identity_self",
            "platform",
            "audit",
            "dynamic_agent",
            "agent_catalog",
            "project",
            "project_collection",
            "job",
            "job_request",
            "job_collection",
            "coding_run",
            "coding_run_collection",
            "dataset",
            "dataset_collection",
            "approval",
            "approval_collection",
        }
    )
