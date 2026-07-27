"""Runnable Node Agent entry point (`python -m agent`).

Phase 0's capability audit recorded that this file was absent, which is why
`node_daemon` has been `implemented=no`: the systemd template's `ExecStart`
pointed at something that did not exist. This supplies the daemon; it does not
authorize enabling one. Per-node activation still needs `DG-NODE-V2`, and
`NODE_AGENT_V1_ENABLED` stays off on the control plane.

The invariants this file exists to honor (`INV-NODE-1…6`):

- **Outbound only.** No listener is ever opened. Every interaction is an HTTPS
  request the agent initiates.
- **Non-root, credential-hygienic.** The token is read from the environment
  (the systemd unit sources a 0600 file) and never appears in a log line, an
  argv, or an exception message.
- **Never launch what was not acknowledged.** `runner.launch()` enforces it;
  the loop below never tries to work around it.
- **A restart never relaunches acknowledged work.** An acknowledged attempt
  with no live process is *unknown*, not *retryable* — relaunching it is
  exactly the duplicate execution the whole design forbids.
- **Heartbeats never change job state.** They report liveness and may carry a
  stop request back; that is all.

Two commands:

    python -m agent --check    # self-check, no network, exits non-zero on fail
    python -m agent            # the daemon loop
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

logger = logging.getLogger("dispatch.node-agent")

DEFAULT_POLL_INTERVAL_SEC = 10
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30
DEFAULT_REQUEST_TIMEOUT_SEC = 20

#: Never log or echo these, even at debug level.
_SECRET_ENV = ("DISPATCH_NODE_TOKEN",)


class AgentConfigError(Exception):
    """Configuration is unusable. The message must never quote a secret."""


class AgentConfig:
    def __init__(
        self,
        *,
        control_plane_url: str,
        node_token: str,
        workdir: str,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        heartbeat_interval_sec: int = DEFAULT_HEARTBEAT_INTERVAL_SEC,
        request_timeout_sec: int = DEFAULT_REQUEST_TIMEOUT_SEC,
        verify_tls: bool = True,
    ) -> None:
        self.control_plane_url = control_plane_url.rstrip("/")
        self.node_token = node_token
        self.workdir = workdir
        self.poll_interval_sec = poll_interval_sec
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.request_timeout_sec = request_timeout_sec
        self.verify_tls = verify_tls

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # The token must not reach a traceback or a debugger session.
        return (
            f"AgentConfig(control_plane_url={self.control_plane_url!r}, "
            f"workdir={self.workdir!r}, token=<redacted>)"
        )


def load_config(env: Optional[dict[str, str]] = None) -> AgentConfig:
    """Read configuration from the environment.

    Errors name the missing variable but never its value: a misconfigured
    agent must not turn into a credential disclosure in a log aggregator.
    """
    env = dict(os.environ if env is None else env)

    url = (env.get("DISPATCH_CONTROL_PLANE_URL") or "").strip()
    if not url:
        raise AgentConfigError("DISPATCH_CONTROL_PLANE_URL is not set")
    if not url.startswith("https://"):
        # A plaintext control-plane URL would put the node token on the wire.
        # Localhost is allowed only so a developer can exercise the loop
        # against a local instance without inventing a certificate.
        if not (url.startswith("http://127.0.0.1") or url.startswith("http://localhost")):
            raise AgentConfigError(
                "DISPATCH_CONTROL_PLANE_URL must use https (or localhost for development)"
            )

    token = (env.get("DISPATCH_NODE_TOKEN") or "").strip()
    if not token:
        raise AgentConfigError("DISPATCH_NODE_TOKEN is not set")

    workdir = (env.get("DISPATCH_NODE_WORKDIR") or "").strip()
    if not workdir:
        workdir = os.path.expanduser("~/.dispatch-node-agent")

    def _int(name: str, default: int) -> int:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise AgentConfigError(f"{name} must be an integer") from None
        if value < 1:
            raise AgentConfigError(f"{name} must be >= 1")
        return value

    return AgentConfig(
        control_plane_url=url,
        node_token=token,
        workdir=workdir,
        poll_interval_sec=_int("DISPATCH_NODE_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC),
        heartbeat_interval_sec=_int(
            "DISPATCH_NODE_HEARTBEAT_INTERVAL_SEC", DEFAULT_HEARTBEAT_INTERVAL_SEC
        ),
        request_timeout_sec=_int(
            "DISPATCH_NODE_REQUEST_TIMEOUT_SEC", DEFAULT_REQUEST_TIMEOUT_SEC
        ),
    )


def build_transport(config: AgentConfig) -> Callable[..., tuple[int, Any]]:
    """An outbound-only HTTP transport for `NodeAgentClient`.

    Deliberately built on `urllib` rather than a session object that could be
    reused as a server: there is no code path here that can accept a
    connection.
    """

    def _transport(method: str, path: str, *, json_body=None, headers=None, **kwargs):
        payload = json_body if json_body is not None else kwargs.get("json")
        url = f"{config.control_plane_url}{path}"
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(
                request, timeout=config.request_timeout_sec
            ) as response:
                body = response.read().decode("utf-8") or "{}"
                return response.status, json.loads(body)
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                body = {}
            return exc.code, body
        except urllib.error.URLError as exc:
            # Unreachable control plane is not a failed job. Surface it as a
            # transport status the loop can back off on.
            raise ConnectionError(f"control plane unreachable: {exc.reason}") from exc

    return _transport


def self_check(env: Optional[dict[str, str]] = None) -> tuple[bool, list[str]]:
    """`--check`: validate configuration and imports without any network I/O.

    The systemd template needs this to exist before it can become a deployable
    unit, so an operator can verify a node before enabling anything.
    """
    findings: list[str] = []
    ok = True

    try:
        config = load_config(env)
        findings.append(f"config: control plane {config.control_plane_url}")
        findings.append(f"config: workdir {config.workdir}")
    except AgentConfigError as exc:
        return False, [f"config: FAIL {exc}"]

    try:
        from agent.client import NodeAgentClient  # noqa: F401
        from agent.runner import AttemptStore, launch, plan_restart  # noqa: F401

        findings.append("imports: client and runner available")
    except Exception as exc:  # noqa: BLE001
        ok = False
        findings.append(f"imports: FAIL {type(exc).__name__}")

    try:
        os.makedirs(config.workdir, exist_ok=True)
        probe = os.path.join(config.workdir, ".write-probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        findings.append("workdir: writable")
    except OSError as exc:
        ok = False
        findings.append(f"workdir: FAIL {exc.strerror}")

    if os.geteuid() == 0:
        # INV-NODE-1. Refuse rather than warn: an agent running as root
        # defeats the isolation the whole Node design is built on.
        ok = False
        findings.append("privileges: FAIL running as root is not permitted")
    else:
        findings.append("privileges: non-root")

    findings.append(
        "activation: this agent is not authorized to take work until"
        " DG-NODE-V2 is approved and the node is enabled per-server"
    )
    return ok, findings


class NodeAgentDaemon:
    """The poll/ack/launch/heartbeat/report loop.

    `client`, `store` and `sleep` are injected so the whole loop is testable
    without a network, a process, or real time.
    """

    def __init__(
        self, config: AgentConfig, client, store, *, sleep=time.sleep, spawn=None
    ):
        self.config = config
        self.client = client
        self.store = store
        self.sleep = sleep
        # Injectable so a test can exercise the launch path without starting a
        # real process. `runner.launch()` binds subprocess.Popen as a default
        # argument, so patching the module attribute afterwards does nothing —
        # without this seam every launch test would actually spawn a shell.
        self.spawn = spawn
        self._stopping = False
        self._last_heartbeat = 0.0

    def request_shutdown(self, *_args) -> None:
        """SIGTERM/SIGINT: stop taking new work, leave running work alone.

        Killing a workload because the agent is restarting would turn an
        agent upgrade into a job failure.
        """
        self._stopping = True

    def recover(self) -> list[str]:
        """Restart recovery (`INV-NODE-5`).

        The rule that matters: an acknowledged attempt with no live process is
        `unknown`, never relaunched. Relaunching it is precisely the duplicate
        execution this design forbids, and the control plane — not the agent —
        decides what an unknown attempt means.
        """
        notes: list[str] = []
        for attempt in self.store.list_all():
            decision = plan_restart_for(attempt, self.store)
            notes.append(f"{attempt.attempt_id}: {decision.reason}")
            if decision.may_launch:
                self._launch(attempt)
        return notes

    def _launch(self, attempt):
        from agent.runner import launch

        if self.spawn is not None:
            return launch(self.store, attempt, spawn=self.spawn)
        return launch(self.store, attempt)

    def tick(self, job_id: int) -> str:
        """One iteration. Returns a short outcome label for logging/tests."""
        if self._stopping:
            return "stopping"

        now = time.monotonic()
        if now - self._last_heartbeat >= self.config.heartbeat_interval_sec:
            self._last_heartbeat = now
            try:
                if self.client.heartbeat():
                    return "stop_requested"
            except ConnectionError:
                # Unreachable is not failure. Back off and try again.
                return "unreachable"

        try:
            work = self.client.poll(job_id)
        except ConnectionError:
            return "unreachable"
        if work is None:
            return "idle"

        if work.stop_requested:
            self.client.acknowledge_stop(work.attempt_id)
            return "stop_requested"

        if work.reused:
            # Already held. Never a second launch.
            return "reused"

        # `acknowledge()` verifies the command digest locally first and raises
        # on mismatch, so a control plane whose payload disagrees with its own
        # digest can never get a command executed here.
        if not self.client.acknowledge(work):
            return "duplicate_ack"

        # Command bytes land as a file before anything can run them
        # (`INV-NODE-3`): they never become part of a shell string.
        attempt = self.store.create(
            attempt_id=work.attempt_id,
            job_id=work.job_id,
            command_sha256=work.command_sha256,
        )
        self.store.write_command(work.attempt_id, work.command)
        # Persist the ack *before* spawning: a crash between the two must look
        # like "may have started", never like "safe to start again".
        attempt = self.store.record_ack(attempt)
        self._launch(attempt)
        return "launched"


def plan_restart_for(attempt, store):
    """Bind `plan_restart` to observable local facts."""
    from agent.runner import plan_restart

    process_alive = False
    if attempt.pid is not None:
        try:
            os.kill(attempt.pid, 0)
            process_alive = True
        except OSError:
            process_alive = False
    # A lease the agent cannot prove is still valid is treated as expired.
    # That only ever makes `plan_restart` *more* conservative: the one branch
    # it unlocks (drop) requires the attempt to have never been acknowledged.
    return plan_restart(attempt, process_alive=process_alive, lease_expired=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent", description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and environment, then exit without any network I/O",
    )
    parser.add_argument("--job-id", type=int, default=0, help="job id to poll for")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.check:
        ok, findings = self_check()
        for line in findings:
            print(line)
        print(f"RESULT: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1

    try:
        config = load_config()
    except AgentConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 2

    ok, findings = self_check()
    if not ok:
        for line in findings:
            logger.error("%s", line)
        return 2

    from agent.client import NodeAgentClient
    from agent.runner import AttemptStore

    client = NodeAgentClient(
        build_transport(config), node_token=config.node_token
    )
    store = AttemptStore(config.workdir)
    daemon = NodeAgentDaemon(config, client, store)

    signal.signal(signal.SIGTERM, daemon.request_shutdown)
    signal.signal(signal.SIGINT, daemon.request_shutdown)

    for note in daemon.recover():
        logger.info("restart recovery: %s", note)

    logger.info("node agent started (outbound only, non-root)")
    while not daemon._stopping:
        outcome = daemon.tick(args.job_id)
        logger.info("tick: %s", outcome)
        time.sleep(config.poll_interval_sec)
    logger.info("node agent stopped; running work was left alone")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
