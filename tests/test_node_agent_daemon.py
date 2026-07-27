"""Phase 4: the Node Agent daemon is runnable, and refuses to duplicate work.

`agent/__main__.py` was absent, which is why the systemd template could not be
installed and `node_daemon` was `implemented=no`. These tests drive the whole
loop with injected transport, store and clock — no network, no processes, no
real time.

Nothing here enables an agent. Per-node activation still needs DG-NODE-V2.
"""

from __future__ import annotations

import os

import pytest

from agent.__main__ import (
    AgentConfig,
    AgentConfigError,
    NodeAgentDaemon,
    load_config,
    self_check,
)
from agent.client import LeasedWork
from agent.runner import AttemptStore


def _config(tmp_path, **overrides):
    base = dict(
        control_plane_url="https://control.example",
        node_token="dcn_secret_value",
        workdir=str(tmp_path / "wd"),
    )
    base.update(overrides)
    return AgentConfig(**base)


class FakeClient:
    def __init__(self, work=None, *, ack=True, heartbeat_stop=False):
        self.work = work
        self.ack_result = ack
        self.heartbeat_stop = heartbeat_stop
        self.calls: list[str] = []
        self.launched_ids: list[str] = []

    def poll(self, job_id):
        self.calls.append("poll")
        return self.work

    def acknowledge(self, work):
        self.calls.append("acknowledge")
        return self.ack_result

    def heartbeat(self, attempt_id=None):
        self.calls.append("heartbeat")
        return self.heartbeat_stop

    def acknowledge_stop(self, attempt_id):
        self.calls.append("acknowledge_stop")
        return True


def _work(**overrides):
    base = dict(
        attempt_id="attempt-1",
        job_id=7,
        command="echo hello",
        command_sha256="sha",
        lease_expires_at="2026-07-28T00:00:00Z",
        reused=False,
    )
    base.update(overrides)
    return LeasedWork(**base)


# ---------------------------------------------------------------------------
# Configuration and credential hygiene
# ---------------------------------------------------------------------------


def test_config_rejects_a_plaintext_control_plane_url():
    """A plaintext URL would put the node token on the wire."""
    with pytest.raises(AgentConfigError, match="https"):
        load_config(
            {
                "DISPATCH_CONTROL_PLANE_URL": "http://control.example",
                "DISPATCH_NODE_TOKEN": "t",
            }
        )


def test_localhost_is_allowed_for_development():
    config = load_config(
        {
            "DISPATCH_CONTROL_PLANE_URL": "http://127.0.0.1:8000",
            "DISPATCH_NODE_TOKEN": "t",
        }
    )
    assert config.control_plane_url == "http://127.0.0.1:8000"


def test_missing_credentials_name_the_variable_never_the_value():
    with pytest.raises(AgentConfigError) as excinfo:
        load_config({"DISPATCH_CONTROL_PLANE_URL": "https://x"})
    assert "DISPATCH_NODE_TOKEN" in str(excinfo.value)


def test_the_token_never_appears_in_a_repr(tmp_path):
    """A traceback or debugger session must not leak the credential."""
    config = _config(tmp_path)
    assert "dcn_secret_value" not in repr(config)
    assert "redacted" in repr(config)


def test_self_check_reports_activation_is_still_gated(tmp_path):
    ok, findings = self_check(
        {
            "DISPATCH_CONTROL_PLANE_URL": "https://control.example",
            "DISPATCH_NODE_TOKEN": "t",
            "DISPATCH_NODE_WORKDIR": str(tmp_path / "wd"),
        }
    )
    assert ok is True
    assert any("DG-NODE-V2" in line for line in findings)


def test_self_check_fails_on_an_unwritable_workdir(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    ok, findings = self_check(
        {
            "DISPATCH_CONTROL_PLANE_URL": "https://control.example",
            "DISPATCH_NODE_TOKEN": "t",
            "DISPATCH_NODE_WORKDIR": str(blocker),
        }
    )
    assert ok is False
    assert any("workdir" in line and "FAIL" in line for line in findings)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _daemon(tmp_path, client, spawn=None):
    config = _config(tmp_path)
    store = AttemptStore(config.workdir)
    daemon = NodeAgentDaemon(config, client, store, spawn=spawn)
    # Skip the first heartbeat so tick() exercises the poll path.
    daemon._last_heartbeat = 10**9
    return daemon, store


def test_no_work_is_idle_not_an_error(tmp_path):
    daemon, _ = _daemon(tmp_path, FakeClient(work=None))
    assert daemon.tick(7) == "idle"


def test_a_reused_attempt_is_never_launched_again(tmp_path):
    """INV-NODE-2/5: the control plane handing back the same attempt must not
    produce a second process."""
    client = FakeClient(work=_work(reused=True))
    daemon, store = _daemon(tmp_path, client)

    assert daemon.tick(7) == "reused"
    assert "acknowledge" not in client.calls
    assert store.list_all() == []


def test_a_duplicate_ack_stops_before_launching(tmp_path):
    client = FakeClient(work=_work(), ack=False)
    daemon, store = _daemon(tmp_path, client)

    assert daemon.tick(7) == "duplicate_ack"
    assert store.list_all() == []


def test_command_bytes_land_as_a_file_before_launch(tmp_path):
    """INV-NODE-3: the command never becomes part of a shell string."""
    spawned = {}

    class FakeProcess:
        pid = 4242

    def _spawn(argv, cwd=None):
        spawned["argv"] = argv
        spawned["cwd"] = cwd
        return FakeProcess()

    client = FakeClient(work=_work(command="echo 'quoted; rm -rf /'"))
    daemon, store = _daemon(tmp_path, client, spawn=_spawn)
    assert daemon.tick(7) == "launched"

    attempt_dir = store.attempt_dir("attempt-1")
    assert (attempt_dir / "cmd.sh").read_text() == "echo 'quoted; rm -rf /'"
    # argv is a list and the command text is not in it.
    assert spawned["argv"] == ["/bin/bash", f"{attempt_dir}/cmd.sh"]
    assert all("rm -rf" not in part for part in spawned["argv"])


def test_ack_is_persisted_before_the_process_starts(tmp_path):
    """A crash between the two must look like 'may have started', never like
    'safe to start again'."""
    observed = {}

    class FakeProcess:
        pid = 99

    def _spawn(argv, cwd=None):
        observed["acked_at_spawn"] = store.load("attempt-1").acked
        return FakeProcess()

    client = FakeClient(work=_work())
    daemon, store = _daemon(tmp_path, client, spawn=_spawn)
    daemon.tick(7)

    assert observed["acked_at_spawn"] is True


def test_a_stop_request_is_acknowledged_and_nothing_launches(tmp_path):
    client = FakeClient(work=_work(stop_requested=True))
    daemon, store = _daemon(tmp_path, client)

    assert daemon.tick(7) == "stop_requested"
    assert "acknowledge_stop" in client.calls
    assert store.list_all() == []


def test_an_unreachable_control_plane_is_not_a_failure(tmp_path):
    class DownClient(FakeClient):
        def poll(self, job_id):
            raise ConnectionError("control plane unreachable")

    daemon, _ = _daemon(tmp_path, DownClient())
    assert daemon.tick(7) == "unreachable"


def test_shutdown_leaves_running_work_alone(tmp_path):
    """Killing a workload because the agent is restarting would turn an agent
    upgrade into a job failure."""
    client = FakeClient(work=_work())
    daemon, _ = _daemon(tmp_path, client)
    daemon.request_shutdown()

    assert daemon.tick(7) == "stopping"
    assert client.calls == []


# ---------------------------------------------------------------------------
# Restart recovery (INV-NODE-5)
# ---------------------------------------------------------------------------


def test_restart_never_relaunches_acknowledged_work(tmp_path):
    """The rule that matters most: an acknowledged attempt with no live
    process is unknown, not retryable."""
    client = FakeClient()
    daemon, store = _daemon(tmp_path, client)
    attempt = store.create(attempt_id="attempt-9", job_id=1, command_sha256="s")
    store.record_ack(attempt)

    notes = daemon.recover()

    assert any("never relaunch" in note for note in notes)
    assert store.load("attempt-9").pid is None


def test_restart_drops_a_lease_that_was_never_acknowledged(tmp_path):
    client = FakeClient()
    daemon, store = _daemon(tmp_path, client)
    store.create(attempt_id="attempt-8", job_id=1, command_sha256="s")

    notes = daemon.recover()

    assert any("drop" in note for note in notes)
    assert store.load("attempt-8").pid is None


def test_restart_leaves_a_terminal_attempt_alone(tmp_path):
    client = FakeClient()
    daemon, store = _daemon(tmp_path, client)
    attempt = store.create(attempt_id="attempt-7", job_id=1, command_sha256="s")
    store.record_terminal(attempt, 0)

    notes = daemon.recover()

    assert any("already terminal" in note for note in notes)


def test_the_module_opens_no_listener():
    """INV-NODE-1: outbound only. A bind/listen anywhere here would be an
    inbound control port on a worker."""
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "agent", "__main__.py"),
        encoding="utf-8",
    ).read()
    for forbidden in ("socket.bind", ".listen(", "HTTPServer", "socketserver", "uvicorn"):
        assert forbidden not in source
