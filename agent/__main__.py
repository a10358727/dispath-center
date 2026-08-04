"""Runnable Node Agent entry point (`python -m agent`).

Phase 0's capability audit recorded that this file was absent, which is why
`node_daemon` has been `implemented=no`: the systemd template's `ExecStart`
pointed at something that did not exist. This supplies the daemon; it does not
authorize enabling one. `DG-NODE-V2` is approved for implementation, but
per-node real-machine activation still needs `DG-NODE-CANARY`, and all
assignment flags stay off on the control plane.

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
    python -m agent --probe    # authenticated read-only protocol handshake
    python -m agent            # the daemon loop
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

logger = logging.getLogger("dispatch.node-agent")

DEFAULT_POLL_INTERVAL_SEC = 10
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30
DEFAULT_REQUEST_TIMEOUT_SEC = 20
DEFAULT_BACKOFF_MAX_SEC = 300
DEFAULT_BACKOFF_JITTER_SEC = 1.0
DEFAULT_STOP_GRACE_SEC = 30

#: Never log or echo these, even at debug level.
_SECRET_ENV = ("DISPATCH_NODE_TOKEN", "DISPATCH_NODE_ACTIVATION_NONCE")


class AgentConfigError(Exception):
    """Configuration is unusable. The message must never quote a secret."""


class AgentConfig:
    def __init__(
        self,
        *,
        control_plane_url: str,
        node_token: str,
        activation_nonce: Optional[str] = None,
        workdir: str,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        heartbeat_interval_sec: int = DEFAULT_HEARTBEAT_INTERVAL_SEC,
        request_timeout_sec: int = DEFAULT_REQUEST_TIMEOUT_SEC,
        verify_tls: bool = True,
        backoff_max_sec: int = DEFAULT_BACKOFF_MAX_SEC,
        backoff_jitter_sec: float = DEFAULT_BACKOFF_JITTER_SEC,
        stop_grace_sec: int = DEFAULT_STOP_GRACE_SEC,
    ) -> None:
        self.control_plane_url = control_plane_url.rstrip("/")
        if not verify_tls and not (
            self.control_plane_url.startswith("http://127.0.0.1")
            or self.control_plane_url.startswith("http://localhost")
        ):
            raise AgentConfigError(
                "TLS verification cannot be disabled for a non-localhost control plane"
            )
        self.node_token = node_token
        self.activation_nonce = activation_nonce
        self.workdir = workdir
        self.poll_interval_sec = poll_interval_sec
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.request_timeout_sec = request_timeout_sec
        self.verify_tls = verify_tls
        if backoff_max_sec < 1:
            raise AgentConfigError("backoff_max_sec must be >= 1")
        if backoff_jitter_sec < 0:
            raise AgentConfigError("backoff_jitter_sec must be >= 0")
        if stop_grace_sec < 0:
            raise AgentConfigError("stop_grace_sec must be >= 0")
        self.backoff_max_sec = backoff_max_sec
        self.backoff_jitter_sec = backoff_jitter_sec
        self.stop_grace_sec = stop_grace_sec

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
    activation_nonce = (
        env.get("DISPATCH_NODE_ACTIVATION_NONCE") or ""
    ).strip() or None

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

    def _float(name: str, default: float) -> float:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise AgentConfigError(f"{name} must be a number") from None
        if value < 0:
            raise AgentConfigError(f"{name} must be >= 0")
        return value

    return AgentConfig(
        control_plane_url=url,
        node_token=token,
        activation_nonce=activation_nonce,
        workdir=workdir,
        poll_interval_sec=_int("DISPATCH_NODE_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC),
        heartbeat_interval_sec=_int(
            "DISPATCH_NODE_HEARTBEAT_INTERVAL_SEC", DEFAULT_HEARTBEAT_INTERVAL_SEC
        ),
        request_timeout_sec=_int(
            "DISPATCH_NODE_REQUEST_TIMEOUT_SEC", DEFAULT_REQUEST_TIMEOUT_SEC
        ),
        backoff_max_sec=_int(
            "DISPATCH_NODE_BACKOFF_MAX_SEC", DEFAULT_BACKOFF_MAX_SEC
        ),
        backoff_jitter_sec=_float(
            "DISPATCH_NODE_BACKOFF_JITTER_SEC", DEFAULT_BACKOFF_JITTER_SEC
        ),
        stop_grace_sec=_int("DISPATCH_NODE_STOP_GRACE_SEC", DEFAULT_STOP_GRACE_SEC),
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
            context = None
            if config.control_plane_url.startswith("https://"):
                # Explicitly create the verified system trust context.  Do not
                # rely on urllib's ambient defaults, and never expose an
                # insecure production fallback.
                context = ssl.create_default_context()
            with urllib.request.urlopen(
                request, timeout=config.request_timeout_sec, context=context
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
        " DG-NODE-CANARY evidence exists and the node is enabled per-server"
    )
    return ok, findings


class NodeAgentDaemon:
    """The poll/ack/launch/heartbeat/report loop.

    `client`, `store` and `sleep` are injected so the whole loop is testable
    without a network, a process, or real time.
    """

    def __init__(
        self,
        config: AgentConfig,
        client,
        store,
        *,
        sleep=time.sleep,
        spawn=None,
        artifact_provider=None,
        log_tail_provider=None,
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
        self._failure_streak = 0
        self._activation_complete = not bool(config.activation_nonce)
        self._random = random.SystemRandom()
        self._stop_deadlines: dict[str, float] = {}
        self.artifact_provider = artifact_provider or self._read_artifact_manifest
        self.log_tail_provider = log_tail_provider or self._read_log_tail

    def record_outcome(self, outcome: str) -> None:
        """Update retry state without turning transport loss into job failure."""
        if outcome == "unreachable":
            self._failure_streak = min(self._failure_streak + 1, 30)
        else:
            self._failure_streak = 0

    def next_delay(self) -> float:
        """Return exponential retry backoff plus bounded jitter.

        The first normal poll uses the configured interval. Repeated transport
        loss backs off up to the configured ceiling; jitter prevents a fleet of
        agents from reconnecting in lockstep after a control-plane restart.
        """
        base = min(
            self.config.poll_interval_sec * (2**self._failure_streak),
            self.config.backoff_max_sec,
        )
        if self.config.backoff_jitter_sec <= 0:
            return float(base)
        return min(
            float(self.config.backoff_max_sec),
            float(base) + self._random.uniform(0, self.config.backoff_jitter_sec),
        )

    def _active_attempt(self, attempt_id: Optional[str] = None):
        for attempt in self.store.list_all():
            if attempt.terminal:
                continue
            if attempt_id is not None and attempt.attempt_id != attempt_id:
                continue
            if attempt.pid is not None:
                return attempt
        return None

    def _read_log_tail(self, attempt) -> str:
        """Read only the daemon-owned stdout/stderr evidence files."""
        from agent.runner import MAX_LOG_TAIL_BYTES

        chunks: list[bytes] = []
        for name in ("stdout.log", "stderr.log"):
            path = self.store.attempt_dir(attempt.attempt_id) / name
            try:
                chunks.append(path.read_bytes()[-MAX_LOG_TAIL_BYTES:])
            except OSError:
                continue
        return b"\n".join(chunks)[-MAX_LOG_TAIL_BYTES:].decode(
            "utf-8", errors="replace"
        )

    def _read_artifact_manifest(self, attempt) -> list[dict]:
        """Read the explicit workload-produced ``artifacts.json`` manifest.

        The agent does not recursively scan a worker filesystem and never
        uploads bytes; the manifest is metadata only and is validated by the
        AttemptStore before persistence.
        """
        path = self.store.attempt_dir(attempt.attempt_id) / "artifacts.json"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, list):
            raise ValueError("artifacts.json must contain a list")
        return payload

    def _collect_evidence(self, attempt):
        from agent.runner import MAX_ARTIFACTS_PER_REPORT

        error = None
        try:
            log_tail = self.log_tail_provider(attempt)
            artifacts = self.artifact_provider(attempt)
            if len(artifacts) > MAX_ARTIFACTS_PER_REPORT:
                raise ValueError("artifact manifest exceeds the allowed size")
            return self.store.record_evidence(
                attempt, log_tail=log_tail, artifacts=artifacts
            )
        except Exception as exc:  # noqa: BLE001 - evidence failure is explicit
            error = type(exc).__name__
            return self.store.record_evidence(
                attempt, log_tail="", artifacts=[], error=error
            )

    @staticmethod
    def _signal_supervisor(attempt, sig: int) -> bool:
        """Signal only a boot/start-time verified durable supervisor.

        A bare PID is never sufficient: Linux may have recycled it after an
        agent restart.  The pidfd helper fails closed when identity cannot be
        proven, leaving the remote state unknown instead of risking a signal
        to an unrelated process.
        """
        from agent.runner import signal_verified_supervisor

        return signal_verified_supervisor(attempt, sig)

    def _deliver_stop(self, attempt, *, attempt_id: Optional[str] = None) -> None:
        """Persist stop intent, retry its delivery receipt, then terminate.

        Stop request, delivery acknowledgement and process termination remain
        separate local facts; none of them fabricates a Job terminal state.
        """
        if attempt is not None:
            if not attempt.stop_requested:
                attempt = self.store.record_stop_requested(attempt)
            #: A persisted stop intent can survive an agent restart.  Recreate
            #: the local delivery deadline and re-send SIGTERM in that case;
            #: otherwise a restarted daemon would acknowledge the request but
            #: leave the workload running indefinitely.  Repeated ticks within
            #: one daemon instance still signal only once.
            if attempt.pid is not None and attempt.attempt_id not in self._stop_deadlines:
                delivered = self._signal_supervisor(attempt, signal.SIGTERM)
                if delivered:
                    self._stop_deadlines[attempt.attempt_id] = (
                        time.monotonic() + self.config.stop_grace_sec
                    )
            if not attempt.stop_acknowledged:
                try:
                    acknowledged = self.client.acknowledge_stop(attempt.attempt_id)
                except ConnectionError:
                    return
                if acknowledged:
                    self.store.record_stop_acknowledged(attempt)
            return
        #: A stop request attached to a leased-but-not-launched attempt still
        #: gets its delivery receipt, but never creates a local side effect.
        try:
            if attempt_id is None:
                return
            self.client.acknowledge_stop(attempt_id)
        except ConnectionError:
            pass

    def request_shutdown(self, *_args) -> None:
        """SIGTERM/SIGINT: stop taking new work, leave running work alone.

        Killing a workload because the agent is restarting would turn an
        agent upgrade into a job failure.
        """
        self._stopping = True

    def _ensure_activation(self) -> None:
        """Activate a newly delivered pending credential exactly once.

        A connection loss may mean the server committed. Leaving the local flag
        false makes the next recovery/tick replay the same token+nonce; the
        control plane's bounded receipt makes that replay idempotent.
        """
        if self._activation_complete:
            return
        if not hasattr(self.client, "activate"):
            raise RuntimeError("client does not support staged activation")
        self.client.activate(self.config.activation_nonce)
        self._activation_complete = True

    def recover(self) -> list[str]:
        """Restart recovery (`INV-NODE-5`).

        The rule that matters: an acknowledged attempt with no live process is
        `unknown`, never relaunched. Relaunching it is precisely the duplicate
        execution this design forbids, and the control plane — not the agent —
        decides what an unknown attempt means.
        """
        notes: list[str] = []
        try:
            self._ensure_activation()
            if self.config.activation_nonce:
                notes.append("pending credential activation confirmed")
        except ConnectionError:
            return ["credential activation outcome unknown; retry required"]
        except Exception as exc:  # noqa: BLE001 - fail closed, no secret text
            return [f"credential activation unavailable: {type(exc).__name__}"]
        # Ask the control plane first.  A transport outage is an observation
        # gap, not permission to lease or relaunch anything.
        current = None
        if hasattr(self.client, "current_attempt"):
            try:
                current = self.client.current_attempt()
                if current is None:
                    notes.append("control plane reports no current attempt")
                else:
                    notes.append(
                        f"control plane current attempt {current.get('id', '<unknown>')}"
                    )
            except ConnectionError:
                notes.append("control plane unreachable; local attempts remain unknown")
            except Exception:  # noqa: BLE001 - recovery must fail closed
                notes.append("control plane current attempt unavailable; local attempts remain unknown")
        for attempt in self.store.list_all():
            attempt = self.store.recover_supervisor_identity(attempt)
            if not attempt.terminal:
                from agent.runner import process_identity_matches

                terminal_exit = self.store.read_terminal_evidence(attempt)
                if terminal_exit is not None and not process_identity_matches(attempt):
                    attempt = self.store.record_terminal(attempt, terminal_exit)
                    notes.append(
                        f"{attempt.attempt_id}: recovered durable terminal evidence"
                    )
                    continue
            #: If an ack response was lost, the control plane's current-attempt
            #: payload can prove the same owner/digest and permit the one
            #: first launch. Without all of that evidence, plan_restart()
            #: deliberately returns unknown and never relaunches.
            if current is not None and self._current_proves_first_launch(current, attempt):
                try:
                    resumed = self._resume_first_launch(current, attempt)
                except Exception:  # noqa: BLE001 - unknown, never relaunch
                    resumed = False
                if resumed:
                    notes.append(f"{attempt.attempt_id}: resumed first launch from current-attempt evidence")
                    continue
            decision = plan_restart_for(attempt, self.store)
            notes.append(f"{attempt.attempt_id}: {decision.reason}")
            if decision.may_launch:
                self._launch(attempt)
        return notes

    @staticmethod
    def _current_proves_first_launch(current: dict, attempt) -> bool:
        if current.get("id") != attempt.attempt_id:
            return False
        if current.get("job_id") != attempt.job_id:
            return False
        if current.get("command_sha256") != attempt.command_sha256:
            return False
        if not bool(current.get("acked")) and current.get("status") not in {"acked", "running"}:
            return False
        #: A durable launch intent, pid, or contradictory marker means we
        #: cannot prove a first launch.
        return attempt.not_launched and attempt.pid is None and not attempt.launch_intent

    def _resume_first_launch(self, current: dict, attempt):
        if not attempt.acked:
            attempt = self.store.record_ack(attempt)
        command = current.get("command")
        if not isinstance(command, str):
            return False
        self.store.write_command(attempt.attempt_id, command)
        attempt = self.store.load(attempt.attempt_id)
        if attempt is None:
            return False
        self._launch(attempt)
        return True

    def _launch(self, attempt):
        from agent.runner import launch

        if self.spawn is not None:
            return launch(self.store, attempt, spawn=self.spawn)
        return launch(self.store, attempt)

    def _ack_and_launch_work(self, work, attempt) -> str:
        """Commit the remote ack before materializing or launching bytes."""
        from agent.client import NodeClientError

        self.store.record_ack_request_started(attempt)
        try:
            acknowledged = self.client.acknowledge(work)
        except ConnectionError:
            # The remote request may have committed; local journal remains
            # ack_request_started and recovery is deliberately conservative.
            return "unreachable"
        except NodeClientError:
            self.store.record_ack_rejected(attempt)
            raise

        if not acknowledged:
            # A duplicate response proves the remote ack, but not whether a
            # previous process was launched. Persist ack and never launch in
            # this branch; current-attempt + local not_launched evidence is the
            # only admissible recovery route.
            self.store.record_ack(attempt)
            return "duplicate_ack"

        attempt = self.store.record_ack(attempt)
        self.store.write_command(work.attempt_id, work.command)
        attempt = self.store.load(work.attempt_id)
        if attempt is None:  # pragma: no cover - atomic journal invariant
            raise RuntimeError("local attempt journal disappeared after command fsync")
        self._launch(attempt)
        return "launched"

    def _recover_reused_work(self, work) -> str:
        """Resolve a server-owned lease after poll/ack response loss.

        ``reused`` only proves that the control plane already owns an attempt
        for this node; it does not say whether ack committed.  Querying
        current-attempt distinguishes a still-unacked lease (safe to ack) from
        a committed ack (launchable only when the local journal proves the
        first launch has never been attempted).
        """
        from agent.client import NodeClientError, command_digest

        if command_digest(work.command) != work.command_sha256:
            raise NodeClientError(0, "command digest mismatch (local verification)")
        try:
            current = self.client.current_attempt()
        except ConnectionError:
            return "unreachable"
        except Exception:  # noqa: BLE001 - evidence gap is not launch permission
            return "reused"
        if (
            not isinstance(current, dict)
            or current.get("id") != work.attempt_id
            or current.get("job_id") != work.job_id
            or current.get("command_sha256") != work.command_sha256
            or current.get("command") != work.command
        ):
            return "reused"

        local = self.store.load(work.attempt_id)
        remotely_acked = bool(current.get("acked")) or current.get("status") in {
            "acked",
            "running",
        }
        if remotely_acked:
            if local is None or not self._current_proves_first_launch(current, local):
                return "reused"
            try:
                if self._resume_first_launch(current, local):
                    return "launched"
            except Exception:  # noqa: BLE001 - never relax first-launch proof
                pass
            return "reused"

        if current.get("status") != "leased":
            return "reused"
        if local is None:
            local = self.store.create(
                attempt_id=work.attempt_id,
                job_id=work.job_id,
                command_sha256=work.command_sha256,
            )
        elif (
            local.job_id != work.job_id
            or local.command_sha256 != work.command_sha256
            or local.acked
            or local.launch_intent
            or local.pid is not None
            or not local.not_launched
        ):
            return "reused"
        if local.ack_request_started:
            # The authenticated current response proves that the earlier ack
            # did not commit.  It is now safe to clear only that local intent
            # and retry the same attempt; no workload side effect was allowed.
            local = self.store.record_ack_rejected(local)
        return self._ack_and_launch_work(work, local)

    def tick(self, job_id: Optional[int] = None) -> str:
        """One iteration. Returns a short outcome label for logging/tests."""
        if self._stopping:
            return "stopping"

        try:
            self._ensure_activation()
        except ConnectionError:
            return "unreachable"

        terminal_outcome = self._monitor_local_attempts()
        if terminal_outcome is not None:
            return terminal_outcome

        now = time.monotonic()
        if now - self._last_heartbeat >= self.config.heartbeat_interval_sec:
            self._last_heartbeat = now
            try:
                active = self._active_attempt()
                try:
                    stop_requested = self.client.heartbeat(
                        active.attempt_id if active is not None else None
                    )
                except TypeError:
                    stop_requested = self.client.heartbeat()
                if stop_requested:
                    self._deliver_stop(active)
                    return "stop_requested"
            except ConnectionError:
                # Unreachable is not failure. Back off and try again.
                return "unreachable"

        try:
            # The v2 wire contract has no job selector.  The fallback keeps
            # injected legacy test doubles usable without changing the real
            # client's payload (which always omits job_id).
            try:
                work = self.client.poll()
            except TypeError:
                work = self.client.poll(None)
        except ConnectionError:
            return "unreachable"
        if work is None:
            return "idle"

        if work.stop_requested:
            self._deliver_stop(
                self.store.load(work.attempt_id), attempt_id=work.attempt_id
            )
            return "stop_requested"

        if work.reused:
            return self._recover_reused_work(work)

        # Validate the immutable payload before creating the local journal. A
        # mismatched digest never reaches the control plane or a launcher.
        from agent.client import NodeClientError, command_digest

        if command_digest(work.command) != work.command_sha256:
            raise NodeClientError(0, "command digest mismatch (local verification)")

        # The journal must exist before the remote ack request. A response-loss
        # crash therefore becomes an observable unknown, never a fresh lease.
        attempt = self.store.create(
            attempt_id=work.attempt_id,
            job_id=work.job_id,
            command_sha256=work.command_sha256,
        )
        return self._ack_and_launch_work(work, attempt)

    def _monitor_local_attempts(self) -> Optional[str]:
        """Observe child exits and durably retry terminal delivery.

        A missing/unknown pid is never turned into a fabricated terminal.  A
        terminal result is persisted before reporting, so a daemon restart can
        safely retry the idempotent report.
        """
        reported = False
        for attempt in self.store.list_all():
            # A crash immediately after spawning can leave launch_intent=true
            # before the daemon journals the PID. The supervisor's own fsynced
            # identity closes that window, and may appear just after recover().
            attempt = self.store.recover_supervisor_identity(attempt)
            if attempt.terminal:
                if not attempt.evidence_collected:
                    attempt = self._collect_evidence(attempt)
                if not attempt.artifacts_reported:
                    if not hasattr(self.client, "report_artifacts") or not attempt.artifacts:
                        self.store.record_artifacts_reported(attempt)
                        attempt = self.store.load(attempt.attempt_id) or attempt
                    else:
                        try:
                            self.client.report_artifacts(
                                attempt.attempt_id, attempt.artifacts
                            )
                            self.store.record_artifacts_reported(attempt)
                            attempt = self.store.load(attempt.attempt_id) or attempt
                        except Exception:  # noqa: BLE001 - durable retry next tick
                            pass
                if attempt.terminal_reported or not hasattr(self.client, "report_terminal"):
                    continue
                try:
                    try:
                        self.client.report_terminal(
                            attempt.attempt_id,
                            exit_code=int(attempt.exit_code or 0),
                            log_tail=attempt.log_tail,
                        )
                    except TypeError:
                        self.client.report_terminal(
                            attempt.attempt_id, exit_code=int(attempt.exit_code or 0)
                        )
                    self.store.record_terminal_reported(attempt)
                    reported = True
                except Exception:  # noqa: BLE001 - retry on next tick
                    pass
                continue
            if attempt.pid is None:
                continue
            deadline = self._stop_deadlines.get(attempt.attempt_id)
            if attempt.stop_requested and deadline is not None and time.monotonic() >= deadline:
                # SIGUSR1 asks the still-running supervisor to escalate the
                # workload to SIGKILL.  Killing the supervisor itself would
                # destroy the only process able to persist terminal evidence.
                self._signal_supervisor(attempt, signal.SIGUSR1)
                self._stop_deadlines.pop(attempt.attempt_id, None)
            # Reap a supervisor started by this daemon, but never infer the
            # workload result from waitpid. The durable terminal sentinel is
            # the sole local result evidence and survives an agent restart.
            try:
                os.waitpid(attempt.pid, os.WNOHANG)
            except ChildProcessError:
                pass
            except OSError:
                pass

            attempt = self.store.recover_supervisor_identity(attempt)
            from agent.runner import process_identity_matches

            exit_code = self.store.read_terminal_evidence(attempt)
            if exit_code is None or process_identity_matches(attempt):
                continue
            attempt = self.store.record_terminal(attempt, exit_code)
            attempt = self._collect_evidence(attempt)
            if attempt.artifacts:
                try:
                    self.client.report_artifacts(attempt.attempt_id, attempt.artifacts)
                    self.store.record_artifacts_reported(attempt)
                    attempt = self.store.load(attempt.attempt_id) or attempt
                except Exception:  # noqa: BLE001 - retry on next tick
                    pass
            elif hasattr(self.client, "report_artifacts"):
                self.store.record_artifacts_reported(attempt)
            if hasattr(self.client, "report_terminal"):
                try:
                    try:
                        self.client.report_terminal(
                            attempt.attempt_id,
                            exit_code=exit_code,
                            log_tail=attempt.log_tail,
                        )
                    except TypeError:
                        self.client.report_terminal(
                            attempt.attempt_id, exit_code=exit_code
                        )
                    self.store.record_terminal_reported(attempt)
                    reported = True
                except Exception:  # noqa: BLE001 - durable state remains pending
                    # Durable terminal state remains pending for the next tick.
                    pass
        return "terminal_reported" if reported else None


def plan_restart_for(attempt, store):
    """Bind `plan_restart` to observable local facts."""
    from agent.runner import plan_restart, process_identity_matches

    attempt = store.recover_supervisor_identity(attempt)
    process_alive = process_identity_matches(attempt)
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
    parser.add_argument(
        "--probe",
        action="store_true",
        help="perform an authenticated read-only protocol handshake, then exit",
    )
    parser.add_argument(
        "--job-id", type=int, default=0,
        help="deprecated compatibility option; v2 polling never sends a job id",
    )
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

    if args.probe:
        client = NodeAgentClient(
            build_transport(config), node_token=config.node_token
        )
        try:
            print(json.dumps(client.probe(), sort_keys=True))
        except Exception as exc:  # noqa: BLE001 - CLI must return a stable code
            logger.error("probe failed: %s", exc)
            return 3
        return 0

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
        outcome = daemon.tick()
        daemon.record_outcome(outcome)
        logger.info("tick: %s", outcome)
        time.sleep(daemon.next_delay())
    logger.info("node agent stopped; running work was left alone")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
