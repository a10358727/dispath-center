from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.db import Database
from app.identity import Actor, ActorType, RequestContext
from dispatch_center.api.errors import APIError
from dispatch_center.api.idempotency import (
    IDEMPOTENCY_TTL_SECONDS,
    IdempotencyIdentity,
    IdempotencyResource,
    canonical_request_sha256,
    idempotency_context_dependency,
    idempotency_key_sha256,
    validate_idempotency_key_values,
)
from dispatch_center.infrastructure.db import SQLiteUnitOfWork


NOW = datetime(2026, 8, 7, 1, 2, 3, tzinfo=timezone.utc)
ROUTE = "/api/v2/projects/{project_id}/run-requests"


def _identity(
    *,
    actor_id: str = "actor-1",
    route: str = ROUTE,
    key: str = "request-key-1",
    body: object | None = None,
) -> IdempotencyIdentity:
    payload = {"command": "python train.py", "expected_revision": 3}
    if body is not None:
        payload = body
    return IdempotencyIdentity(
        actor_id=actor_id,
        route_key=f"POST {route}",
        key_sha256=idempotency_key_sha256(key),
        request_sha256=canonical_request_sha256(
            body=payload,
            method="POST",
            path={"project_id": "project-1"},
            query={},
            route_template=route,
        ),
    )


def _insert_actor(database: Database, actor_id: str) -> None:
    database.insert_actor(
        actor_id=actor_id,
        actor_type=ActorType.HUMAN,
        display_name=actor_id,
    )


def _create_result_table(database: Database) -> None:
    with database.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE api_v2_test_resources (id TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )


def _request(
    *,
    header_values: tuple[str, ...] = ("request-key-1",),
    actor: Actor | None = None,
    route: str = ROUTE,
) -> Request:
    headers = [(b"idempotency-key", value.encode("latin-1")) for value in header_values]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v2/projects/project-1/run-requests",
        "raw_path": b"/api/v2/projects/project-1/run-requests",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "state": {
            "request_context": RequestContext(
                actor=actor,
                authentication_method="session" if actor else "anonymous",
            )
        },
        "route": SimpleNamespace(path=route),
    }
    return Request(scope)


@pytest.mark.parametrize("value", ["", " leading", "trailing ", "has\ttab", "é", "x" * 256])
def test_idempotency_key_rejects_non_visible_or_oversized_values(value):
    with pytest.raises(APIError) as raised:
        validate_idempotency_key_values((value,))
    assert raised.value.code == "invalid_idempotency_key"
    assert raised.value.status_code == 400


def test_idempotency_key_is_exact_ascii_and_only_hash_is_retained():
    upper = idempotency_key_sha256("Case-Sensitive")
    lower = idempotency_key_sha256("case-sensitive")
    assert upper != lower
    assert upper == hashlib.sha256(b"Case-Sensitive").hexdigest()

    with pytest.raises(APIError) as missing:
        validate_idempotency_key_values(())
    assert missing.value.code == "idempotency_key_required"
    with pytest.raises(APIError) as duplicate:
        validate_idempotency_key_values(("one", "two"))
    assert duplicate.value.code == "invalid_idempotency_key"


@pytest.mark.asyncio
async def test_idempotency_context_requires_actor_and_captures_route_template():
    actor = Actor(id="actor-1", actor_type=ActorType.HUMAN, display_name="Ada")
    context = await idempotency_context_dependency(_request(actor=actor))
    identity = context.bind(
        body={"expected_revision": 3, "command": "python train.py"},
        path={"project_id": "project-1"},
        query={},
    )

    assert context.actor_id == actor.id
    assert context.route_template == ROUTE
    assert identity.route_key == f"POST {ROUTE}"
    with pytest.raises(APIError) as anonymous:
        await idempotency_context_dependency(_request())
    assert anonymous.value.status_code == 401
    assert anonymous.value.code == "authentication_required"


def test_request_digest_is_canonical_and_isolates_path_query_and_route():
    left = canonical_request_sha256(
        body={"b": 2, "a": 1},
        method="post",
        path={"project_id": "project-1"},
        query={"dry_run": False},
        route_template=ROUTE,
    )
    reordered = canonical_request_sha256(
        body={"a": 1, "b": 2},
        method="POST",
        path={"project_id": "project-1"},
        query={"dry_run": False},
        route_template=ROUTE,
    )
    other_path = canonical_request_sha256(
        body={"a": 1, "b": 2},
        method="POST",
        path={"project_id": "project-2"},
        query={"dry_run": False},
        route_template=ROUTE,
    )

    assert left == reordered
    assert left != other_path
    with pytest.raises(ValueError, match="floats"):
        canonical_request_sha256(
            body={"ratio": 0.5},
            method="POST",
            path={"project_id": "project-1"},
            query={},
            route_template=ROUTE,
        )


def test_same_payload_replays_and_changed_payload_conflicts_atomically(tmp_path):
    database = Database(str(tmp_path / "idempotency.db"))
    _insert_actor(database, "actor-1")
    _create_result_table(database)
    identity = _identity()
    callback_calls = 0

    def create(cursor):
        nonlocal callback_calls
        callback_calls += 1
        cursor.execute(
            "INSERT INTO api_v2_test_resources VALUES (?, ?)",
            ("resource-1", "created"),
        )
        return IdempotencyResource("run_request", "resource-1")

    with SQLiteUnitOfWork(database) as uow:
        created = uow.run_idempotent(identity, create, now=NOW)
        replayed = uow.run_idempotent(identity, create, now=NOW + timedelta(seconds=1))
        with pytest.raises(APIError) as conflict:
            uow.run_idempotent(
                _identity(body={"command": "different", "expected_revision": 3}),
                create,
                now=NOW + timedelta(seconds=2),
            )

    assert created.replayed is False
    assert replayed.replayed is True
    assert replayed.resource == created.resource
    assert callback_calls == 1
    assert conflict.value.code == "idempotency_key_reused"
    with database.cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM api_v2_test_resources").fetchone()[0] == 1
        row = cursor.execute("SELECT * FROM api_idempotency_keys").fetchone()
    assert row["key_sha256"] == identity.key_sha256
    assert "request-key-1" not in tuple(str(value) for value in row)
    database.close()


def test_scope_isolated_by_actor_and_stable_route(tmp_path):
    database = Database(str(tmp_path / "scope.db"))
    _insert_actor(database, "actor-1")
    _insert_actor(database, "actor-2")
    _create_result_table(database)

    def create(resource_id):
        def callback(cursor):
            cursor.execute(
                "INSERT INTO api_v2_test_resources VALUES (?, ?)",
                (resource_id, "created"),
            )
            return IdempotencyResource("request", resource_id)

        return callback

    with SQLiteUnitOfWork(database) as uow:
        actor_one = uow.run_idempotent(_identity(actor_id="actor-1"), create("one"), now=NOW)
        actor_two = uow.run_idempotent(_identity(actor_id="actor-2"), create("two"), now=NOW)
        other_route = uow.run_idempotent(
            _identity(
                actor_id="actor-1",
                route="/api/v2/projects/{project_id}/stop-requests",
            ),
            create("three"),
            now=NOW,
        )

    assert {
        actor_one.resource.resource_id,
        actor_two.resource.resource_id,
        other_route.resource.resource_id,
    } == {
        "one",
        "two",
        "three",
    }
    with database.cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM api_idempotency_keys").fetchone()[0] == 3
    database.close()


def test_expired_key_is_deleted_and_reused_at_exact_ttl(tmp_path):
    database = Database(str(tmp_path / "expiry.db"))
    _insert_actor(database, "actor-1")
    _create_result_table(database)
    identity = _identity()

    def create(resource_id):
        def callback(cursor):
            cursor.execute(
                "INSERT INTO api_v2_test_resources VALUES (?, ?)",
                (resource_id, "created"),
            )
            return IdempotencyResource("request", resource_id)

        return callback

    with SQLiteUnitOfWork(database) as uow:
        first = uow.run_idempotent(identity, create("first"), now=NOW)
        second = uow.run_idempotent(
            identity,
            create("second"),
            now=NOW + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS),
        )

    assert first.replayed is False
    assert second.replayed is False
    assert second.resource.resource_id == "second"
    with database.cursor() as cursor:
        rows = cursor.execute(
            "SELECT result_resource_id, created_at, expires_at FROM api_idempotency_keys"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["result_resource_id"] == "second"
    assert rows[0]["expires_at"] > rows[0]["created_at"]
    database.close()


@pytest.mark.parametrize("invalid_result", [None, object()])
def test_callback_failure_or_invalid_result_rolls_back_everything(tmp_path, invalid_result):
    database = Database(str(tmp_path / f"rollback-{id(invalid_result)}.db"))
    _insert_actor(database, "actor-1")
    _create_result_table(database)

    def callback(cursor):
        cursor.execute("INSERT INTO api_v2_test_resources VALUES ('orphan', 'must rollback')")
        if invalid_result is None:
            raise RuntimeError("injected callback failure")
        return invalid_result

    expected = RuntimeError if invalid_result is None else ValueError
    with pytest.raises(expected):
        with SQLiteUnitOfWork(database) as uow:
            uow.run_idempotent(_identity(), callback, now=NOW)
    with database.cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM api_v2_test_resources").fetchone()[0] == 0
        assert cursor.execute("SELECT COUNT(*) FROM api_idempotency_keys").fetchone()[0] == 0
    database.close()


def test_actor_foreign_key_restricts_deletion_after_completed_result(tmp_path):
    database = Database(str(tmp_path / "foreign-key.db"))
    _insert_actor(database, "actor-1")
    _create_result_table(database)

    with SQLiteUnitOfWork(database) as uow:
        uow.run_idempotent(
            _identity(),
            lambda cursor: IdempotencyResource("request", "resource-1"),
            now=NOW,
        )
    with pytest.raises(sqlite3.IntegrityError):
        with database.cursor() as cursor:
            cursor.execute("DELETE FROM actors WHERE id = 'actor-1'")
    database.close()


def test_two_connections_create_once_then_replay_after_restart(tmp_path):
    path = tmp_path / "race.db"
    bootstrap = Database(str(path))
    _insert_actor(bootstrap, "actor-1")
    _create_result_table(bootstrap)
    bootstrap.close()
    left = Database(str(path))
    right = Database(str(path))
    barrier = threading.Barrier(2)
    outcomes = []
    failures: list[BaseException] = []

    def contend(database: Database) -> None:
        try:
            barrier.wait(timeout=5)

            def create(cursor):
                cursor.execute("INSERT INTO api_v2_test_resources VALUES ('winner', 'created')")
                return IdempotencyResource("request", "winner")

            with SQLiteUnitOfWork(database) as uow:
                outcomes.append(uow.run_idempotent(_identity(), create, now=NOW))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [
        threading.Thread(target=contend, args=(left,)),
        threading.Thread(target=contend, args=(right,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
    left.close()
    right.close()

    reopened = Database(str(path))
    with SQLiteUnitOfWork(reopened) as uow:
        replay = uow.run_idempotent(
            _identity(),
            lambda _cursor: (_ for _ in ()).throw(AssertionError("replay must not call callback")),
            now=NOW + timedelta(seconds=1),
        )
    assert replay.replayed is True
    with reopened.cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM api_v2_test_resources").fetchone()[0] == 1
        assert cursor.execute("SELECT COUNT(*) FROM api_idempotency_keys").fetchone()[0] == 1
    reopened.close()


def test_bounded_database_lock_wait_maps_to_retriable_conflict(tmp_path):
    path = tmp_path / "busy.db"
    owner = Database(str(path))
    _insert_actor(owner, "actor-1")
    contender = Database(str(path))
    contender._conn.execute("PRAGMA busy_timeout = 1")
    owner._conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(APIError) as raised:
            with SQLiteUnitOfWork(contender) as uow:
                uow.run_idempotent(
                    _identity(),
                    lambda _cursor: IdempotencyResource("request", "never"),
                    now=NOW,
                )
        assert raised.value.code == "idempotency_in_progress"
        assert raised.value.status_code == 409
        assert raised.value.details == {"retryable": True}
    finally:
        owner._conn.rollback()
        contender.close()
        owner.close()
