"""WP-4A: server-selected leases, recovery, and Job convergence.

Implements DG-NODE-V2-v1, ruled 2026-07-28. The defect it fixes: v1 let the
agent name the `job_id` it wanted, inverting the trust relationship — deciding
what runs where is the control plane's job.

Nothing here enrolls a node in a real deployment; `NODE_AGENT_V1_ENABLED` is
set per-test and real-machine activation still needs DG-NODE-CANARY.
"""

from __future__ import annotations

import pytest

from app.node_registry import enroll_node, select_job_for_node


@pytest.fixture()
def node_env(api_client):
    client, main_module = api_client
    state = main_module.app_state
    state.config.node_agent_v1_enabled = True
    state.config.node_canary_require_tag = "node-canary"
    enrolled = enroll_node(state.db, server_name="worker-a")
    return client, state, enrolled


def _canary_job(state, **overrides):
    kwargs = dict(command="python train.py", type="train", require_tag="node-canary")
    kwargs.update(overrides)
    return state.db.get_job(state.db.insert_job(**kwargs))


# ---------------------------------------------------------------------------
# N-1: the control plane chooses
# ---------------------------------------------------------------------------


def test_the_agent_cannot_name_its_own_work(node_env):
    client, state, enrolled = node_env
    _canary_job(state)

    resp = client.post(
        "/node-agent/poll",
        json={"job_id": 1},
        headers={"X-Node-Token": enrolled.raw_token},
    )

    assert resp.status_code == 422, "a v1 agent must be refused, not silently ignored"


def test_selection_skips_a_job_pinned_to_another_machine(node_env):
    """A pinned job belongs to exactly one machine."""
    _client, state, enrolled = node_env
    _canary_job(state, pin_server="worker-b")

    node = state.db.get_node(enrolled.node.id)
    assert select_job_for_node(state.db, node=node, canary_tag="node-canary") is None


def test_selection_takes_a_job_pinned_to_this_machine(node_env):
    _client, state, enrolled = node_env
    job = _canary_job(state, pin_server="worker-a")

    node = state.db.get_node(enrolled.node.id)
    selected = select_job_for_node(state.db, node=node, canary_tag="node-canary")
    assert selected is not None and selected.id == job.id


def test_selection_is_deterministic_fifo(node_env):
    _client, state, enrolled = node_env
    first = _canary_job(state)
    _canary_job(state)

    node = state.db.get_node(enrolled.node.id)
    for _ in range(3):
        assert select_job_for_node(
            state.db, node=node, canary_tag="node-canary"
        ).id == first.id


def test_selection_refuses_everything_when_no_canary_tag_is_configured(node_env):
    """Fail closed: with no canary tag, nothing is eligible."""
    _client, state, enrolled = node_env
    _canary_job(state)

    node = state.db.get_node(enrolled.node.id)
    assert select_job_for_node(state.db, node=node, canary_tag=None) is None


# ---------------------------------------------------------------------------
# One active attempt per node
# ---------------------------------------------------------------------------


def test_a_repoll_returns_the_same_attempt_rather_than_losing_it(node_env):
    """Regression: server-side selection only looks at `queued` jobs, so a
    node that already holds work would find nothing on its next poll and lose
    track of its own attempt. Held work must be returned before new work is
    selected."""
    client, state, enrolled = node_env
    _canary_job(state)
    headers = {"X-Node-Token": enrolled.raw_token}

    first = client.post("/node-agent/poll", json={}, headers=headers).json()
    second = client.post("/node-agent/poll", json={}, headers=headers).json()

    assert first["attempt"] is not None
    assert second["attempt"]["id"] == first["attempt"]["id"]
    assert second["reused"] is True


def test_a_node_holding_work_is_not_given_a_second_job(node_env):
    client, state, enrolled = node_env
    _canary_job(state)
    _canary_job(state)
    headers = {"X-Node-Token": enrolled.raw_token}

    first = client.post("/node-agent/poll", json={}, headers=headers).json()
    second = client.post("/node-agent/poll", json={}, headers=headers).json()

    assert second["attempt"]["job_id"] == first["attempt"]["job_id"]
    with state.db.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM node_attempts")
        assert cursor.fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# N-2: current-attempt recovery
# ---------------------------------------------------------------------------


def test_a_restarting_agent_can_ask_what_it_owns(node_env):
    """The agent asks instead of inferring: deciding wrongly in either
    direction is duplicate execution or a fabricated terminal."""
    client, state, enrolled = node_env
    _canary_job(state)
    headers = {"X-Node-Token": enrolled.raw_token}
    leased = client.post("/node-agent/poll", json={}, headers=headers).json()

    current = client.post("/node-agent/current-attempt", json={}, headers=headers).json()

    assert current["attempt"]["id"] == leased["attempt"]["id"]
    assert current["attempt"]["acked"] is False
    assert current["attempt"]["command"] == "python train.py"


def test_a_node_with_nothing_in_flight_owns_nothing(node_env):
    client, _state, enrolled = node_env
    body = client.post(
        "/node-agent/current-attempt", json={}, headers={"X-Node-Token": enrolled.raw_token}
    ).json()
    assert body["attempt"] is None


def test_recovery_never_reveals_another_nodes_attempt(node_env):
    client, state, enrolled = node_env
    _canary_job(state)
    client.post("/node-agent/poll", json={}, headers={"X-Node-Token": enrolled.raw_token})

    other = enroll_node(state.db, server_name="worker-b")
    body = client.post(
        "/node-agent/current-attempt", json={}, headers={"X-Node-Token": other.raw_token}
    ).json()

    assert body["attempt"] is None


# ---------------------------------------------------------------------------
# Node terminals converge the canonical Job
# ---------------------------------------------------------------------------


def _run_to_terminal(client, state, enrolled, exit_code):
    headers = {"X-Node-Token": enrolled.raw_token}
    leased = client.post("/node-agent/poll", json={}, headers=headers).json()["attempt"]
    client.post(
        "/node-agent/ack",
        json={"attempt_id": leased["id"], "command_sha256": leased["command_sha256"]},
        headers=headers,
    )
    with state.db.cursor() as cursor:
        cursor.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?", (leased["job_id"],)
        )
    resp = client.post(
        "/node-agent/terminal",
        json={"attempt_id": leased["id"], "exit_code": exit_code, "log_tail": ""},
        headers=headers,
    )
    return leased, resp


@pytest.mark.parametrize("exit_code,expected", [(0, "done"), (3, "failed")])
def test_a_node_terminal_converges_the_canonical_job(node_env, exit_code, expected):
    """v1 closed only `node_attempts`, so a Job could sit `running` forever
    while its node attempt was finished."""
    client, state, enrolled = node_env
    _canary_job(state)

    leased, resp = _run_to_terminal(client, state, enrolled, exit_code)

    assert resp.status_code == 200
    job = state.db.get_job(leased["job_id"])
    assert job.status == expected
    assert job.exit_code == exit_code


def test_the_projection_refuses_a_terminal_the_attempt_does_not_carry(node_env):
    """Same guard as the SSH path: nothing may invent a terminal."""
    client, state, enrolled = node_env
    _canary_job(state)
    headers = {"X-Node-Token": enrolled.raw_token}
    leased = client.post("/node-agent/poll", json={}, headers=headers).json()["attempt"]

    with pytest.raises(ValueError, match="must match the node attempt terminal state"):
        state.db.apply_node_terminal_to_job(
            attempt_id=leased["id"], job_status="done", exit_code=0
        )


def test_a_job_that_is_no_longer_running_is_left_alone(node_env):
    """A stop or an earlier convergence already decided it."""
    client, state, enrolled = node_env
    _canary_job(state)
    headers = {"X-Node-Token": enrolled.raw_token}
    leased = client.post("/node-agent/poll", json={}, headers=headers).json()["attempt"]
    client.post(
        "/node-agent/ack",
        json={"attempt_id": leased["id"], "command_sha256": leased["command_sha256"]},
        headers=headers,
    )
    # The Job was never moved to running.
    resp = client.post(
        "/node-agent/terminal",
        json={"attempt_id": leased["id"], "exit_code": 0, "log_tail": ""},
        headers=headers,
    )

    assert resp.status_code == 200
    assert state.db.get_job(leased["job_id"]).status == "queued"
