"""Phase 4: the Node Agent daemon is runnable, and refuses to duplicate work.

`agent/__main__.py` was absent, which is why the systemd template could not be
installed and `node_daemon` was `implemented=no`. These tests drive the whole
loop with injected transport, store and clock — no network, no processes, no
real time.

Nothing here enables an agent. Per-node activation still needs DG-NODE-CANARY.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.__main__ import (
    AgentConfig,
    AgentConfigError,
    NodeAgentDaemon,
    load_config,
    self_check,
)
from agent.client import LeasedWork, command_digest
from agent.runner import AttemptStore, build_supervisor_argv, launch


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
    base["command_sha256"] = command_digest(base["command"])
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
    assert config.isolation_mode == "direct"
    assert config.deployment_tier == "development"


def test_canary_configuration_cannot_use_direct_launcher():
    with pytest.raises(AgentConfigError, match="require systemd"):
        load_config(
            {
                "DISPATCH_CONTROL_PLANE_URL": "https://control.example",
                "DISPATCH_NODE_TOKEN": "t",
                "DISPATCH_NODE_DEPLOYMENT_TIER": "canary",
                "DISPATCH_NODE_ISOLATION_MODE": "direct",
            }
        )


def test_tls_verification_cannot_be_disabled_for_a_remote_control_plane(tmp_path):
    with pytest.raises(AgentConfigError, match="TLS verification"):
        _config(tmp_path, verify_tls=False)


def test_missing_credentials_name_the_variable_never_the_value():
    with pytest.raises(AgentConfigError) as excinfo:
        load_config({"DISPATCH_CONTROL_PLANE_URL": "https://x"})
    assert "DISPATCH_NODE_TOKEN" in str(excinfo.value)


def test_retry_backoff_is_bounded_jittered_and_resets_after_success(tmp_path):
    config = _config(
        tmp_path,
        poll_interval_sec=10,
        backoff_max_sec=25,
        backoff_jitter_sec=0,
    )
    daemon = NodeAgentDaemon(config, FakeClient(), AttemptStore(config.workdir))

    assert daemon.next_delay() == 10
    daemon.record_outcome("unreachable")
    assert daemon.next_delay() == 20
    daemon.record_outcome("unreachable")
    assert daemon.next_delay() == 25
    daemon.record_outcome("idle")
    assert daemon.next_delay() == 10


def test_the_token_never_appears_in_a_repr(tmp_path):
    """A traceback or debugger session must not leak the credential."""
    config = _config(tmp_path)
    assert "dcn_secret_value" not in repr(config)
    assert "redacted" in repr(config)


def test_staged_activation_nonce_loads_but_never_appears_in_repr(tmp_path):
    config = load_config(
        {
            "DISPATCH_CONTROL_PLANE_URL": "https://control.example",
            "DISPATCH_NODE_TOKEN": "token",
            "DISPATCH_NODE_ACTIVATION_NONCE": "one-time-secret-nonce",
            "DISPATCH_NODE_WORKDIR": str(tmp_path / "wd"),
        }
    )

    assert config.activation_nonce == "one-time-secret-nonce"
    assert "one-time-secret-nonce" not in repr(config)


def test_self_check_reports_real_machine_activation_is_still_gated(tmp_path):
    ok, findings = self_check(
        {
            "DISPATCH_CONTROL_PLANE_URL": "https://control.example",
            "DISPATCH_NODE_TOKEN": "t",
            "DISPATCH_NODE_WORKDIR": str(tmp_path / "wd"),
        }
    )
    assert ok is True
    assert any("DG-NODE-CANARY" in line for line in findings)


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


def _spawn_recorder(recorded):
    class FakeProcess:
        pid = 4242

    def _spawn(argv, cwd=None):
        recorded.append((argv, cwd))
        return FakeProcess()

    return _spawn


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
    journal = store.load("attempt-1")
    assert journal is not None
    assert journal.acked is True
    assert journal.pid is None


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
    assert spawned["argv"] == build_supervisor_argv(
        "attempt-1", attempt_dir, command_digest("echo 'quoted; rm -rf /'")
    )
    assert all("rm -rf" not in part for part in spawned["argv"])


def test_ack_is_persisted_before_the_process_starts(tmp_path):
    """A crash between the two must look like 'may have started', never like
    'safe to start again'."""
    observed = {}

    class FakeProcess:
        pid = 99

    def _spawn(argv, cwd=None):
        journal = store.load("attempt-1")
        observed["acked_at_spawn"] = journal.acked
        observed["command_materialized_at_spawn"] = journal.command_materialized
        observed["launch_intent_at_spawn"] = journal.launch_intent
        observed["not_launched_at_spawn"] = journal.not_launched
        return FakeProcess()

    client = FakeClient(work=_work())
    daemon, store = _daemon(tmp_path, client, spawn=_spawn)
    daemon.tick(7)

    assert observed["acked_at_spawn"] is True
    assert observed["command_materialized_at_spawn"] is True
    assert observed["launch_intent_at_spawn"] is True
    assert observed["not_launched_at_spawn"] is False


def test_ack_response_loss_is_unknown_and_never_relaunched_without_evidence(tmp_path):
    class ResponseLost(FakeClient):
        def acknowledge(self, work):
            self.calls.append("acknowledge")
            raise ConnectionError("response lost")

    spawned = []
    client = ResponseLost(work=_work())
    daemon, store = _daemon(tmp_path, client, spawn=_spawn_recorder(spawned))

    assert daemon.tick(7) == "unreachable"
    journal = store.load("attempt-1")
    assert journal is not None
    assert journal.ack_request_started is True
    assert journal.acked is False

    notes = daemon.recover()
    assert any("unknown" in note for note in notes)
    assert spawned == []


def test_ack_response_loss_can_resume_only_with_exact_current_evidence(tmp_path):
    class ResponseLostThenCurrent(FakeClient):
        def acknowledge(self, work):
            self.calls.append("acknowledge")
            raise ConnectionError("response lost")

        def current_attempt(self):
            return {
                "id": "attempt-1",
                "job_id": 7,
                "status": "acked",
                "command": self.work.command,
                "command_sha256": self.work.command_sha256,
                "acked": True,
            }

    spawned = []
    client = ResponseLostThenCurrent(work=_work())
    daemon, store = _daemon(tmp_path, client, spawn=_spawn_recorder(spawned))
    assert daemon.tick(7) == "unreachable"

    notes = daemon.recover()

    assert any("resumed first launch" in note for note in notes)
    assert len(spawned) == 1
    journal = store.load("attempt-1")
    assert journal.acked is True
    assert journal.launch_intent is True


def test_ack_response_loss_recovers_after_reconnect_without_daemon_restart(tmp_path):
    class ResponseLostThenReconnect(FakeClient):
        def __init__(self):
            super().__init__(work=_work())
            self.ack_calls = 0

        def acknowledge(self, work):
            self.calls.append("acknowledge")
            self.ack_calls += 1
            if self.ack_calls == 1:
                raise ConnectionError("response lost after commit")
            return False

        def current_attempt(self):
            return {
                "id": "attempt-1",
                "job_id": 7,
                "status": "acked",
                "command": self.work.command,
                "command_sha256": self.work.command_sha256,
                "acked": True,
            }

    spawned = []
    client = ResponseLostThenReconnect()
    daemon, store = _daemon(tmp_path, client, spawn=_spawn_recorder(spawned))

    assert daemon.tick(7) == "unreachable"
    client.work = _work(reused=True)
    assert daemon.tick(7) == "launched"
    assert len(spawned) == 1
    journal = store.load("attempt-1")
    assert journal.acked is True
    assert journal.launch_intent is True


def test_lost_poll_response_can_ack_the_same_unlaunched_reused_lease(tmp_path):
    class RecoveredLease(FakeClient):
        def __init__(self):
            super().__init__(work=_work(reused=True))

        def current_attempt(self):
            return {
                "id": "attempt-1",
                "job_id": 7,
                "status": "leased",
                "command": self.work.command,
                "command_sha256": self.work.command_sha256,
                "acked": False,
            }

    spawned = []
    client = RecoveredLease()
    daemon, store = _daemon(tmp_path, client, spawn=_spawn_recorder(spawned))

    assert daemon.tick(7) == "launched"
    assert client.calls.count("acknowledge") == 1
    assert len(spawned) == 1
    assert store.load("attempt-1").launch_intent is True


def test_a_stop_request_is_acknowledged_and_nothing_launches(tmp_path):
    client = FakeClient(work=_work(stop_requested=True))
    daemon, store = _daemon(tmp_path, client)

    assert daemon.tick(7) == "stop_requested"
    assert "acknowledge_stop" in client.calls
    assert store.list_all() == []


def test_heartbeat_stop_persists_receipt_and_signals_only_verified_supervisor(
    tmp_path, monkeypatch
):
    client = FakeClient(work=None, heartbeat_stop=True)
    daemon, store = _daemon(tmp_path, client)
    command = "echo running"
    attempt = store.create(
        attempt_id="attempt-2", job_id=7, command_sha256=command_digest(command)
    )
    store.record_ack(attempt)
    store.write_command("attempt-2", command)
    attempt = store.load("attempt-2")
    launch(
        store,
        attempt,
        spawn=lambda argv, **kwargs: type("P", (), {"pid": 77})(),
    )

    signals = []
    monkeypatch.setattr(
        daemon,
        "_signal_supervisor",
        lambda attempt, sig: signals.append((attempt.pid, sig)) or True,
    )

    daemon._last_heartbeat = 0
    assert daemon.tick(7) == "stop_requested"
    journal = store.load("attempt-2")
    assert journal.stop_requested is True
    assert journal.stop_acknowledged is True
    assert signals and signals[0][0] == 77
    assert "acknowledge_stop" in client.calls


def test_restart_redelivers_persisted_stop_intent(tmp_path, monkeypatch):
    """A daemon restart must not lose the SIGTERM/deadline side of stop."""
    client = FakeClient(work=None)
    first, store = _daemon(tmp_path, client)
    signals = []
    monkeypatch.setattr(
        NodeAgentDaemon,
        "_signal_supervisor",
        staticmethod(lambda attempt, sig: signals.append((attempt.pid, sig)) or True),
    )
    command = "echo running"
    attempt = store.create(
        attempt_id="attempt-stop-restart",
        job_id=7,
        command_sha256=command_digest(command),
    )
    store.record_ack(attempt)
    store.write_command(attempt.attempt_id, command)
    attempt = store.load(attempt.attempt_id)
    launch(
        store,
        attempt,
        spawn=lambda argv, **kwargs: type("P", (), {"pid": 78})(),
    )
    attempt = store.load(attempt.attempt_id)
    first._deliver_stop(attempt)

    signals.clear()
    restarted, _ = _daemon(tmp_path, client)
    restarted._deliver_stop(store.load(attempt.attempt_id))

    assert signals and signals[-1][0] == 78
    assert attempt.attempt_id in restarted._stop_deadlines


def test_terminal_evidence_is_persisted_and_artifact_report_retries(tmp_path):
    class EvidenceClient:
        def __init__(self):
            self.artifact_attempts = 0
            self.artifacts = []
            self.terminals = []

        def report_artifacts(self, attempt_id, artifacts):
            self.artifact_attempts += 1
            if self.artifact_attempts == 1:
                raise ConnectionError("artifact response lost")
            self.artifacts.append((attempt_id, artifacts))
            return len(artifacts)

        def report_terminal(self, attempt_id, *, exit_code, log_tail=""):
            self.terminals.append((attempt_id, exit_code, log_tail))
            return True

    client = EvidenceClient()
    daemon, store = _daemon(tmp_path, client)
    command = "echo evidence"
    attempt = store.create(
        attempt_id="attempt-3", job_id=7, command_sha256=command_digest(command)
    )
    store.record_ack(attempt)
    store.write_command("attempt-3", command)
    attempt = store.load("attempt-3")
    store.record_launch(attempt, 88)
    attempt = store.load("attempt-3")
    store.record_terminal(attempt, 0)
    (store.attempt_dir("attempt-3") / "stdout.log").write_text("finished\n")
    (store.attempt_dir("attempt-3") / "artifacts.json").write_text(
        json.dumps(
            [
                {
                    "path": "out/model.bin",
                    "size_bytes": 12,
                    "sha256": "A" * 64,
                }
            ]
        )
    )

    assert daemon._monitor_local_attempts() == "terminal_reported"
    journal = store.load("attempt-3")
    assert journal.evidence_collected is True
    assert journal.artifacts_reported is False
    assert journal.log_tail == "finished\n"
    assert client.terminals == [("attempt-3", 0, "finished\n")]

    assert daemon._monitor_local_attempts() is None
    journal = store.load("attempt-3")
    assert journal.artifacts_reported is True
    assert client.artifacts[0][1][0]["sha256"] == "a" * 64


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
