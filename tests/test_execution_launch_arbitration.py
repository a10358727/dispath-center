"""WP-2B crash matrix for DG-AMBIGUOUS-LAUNCH-v1.

This file is the Verification clause of the revised `INV-STATE-2`. Every case
asserts one of two things:

- a Job may return to `queued` only when non-launch is *proven*, or
- an ambiguous observation changes nothing that could produce a second launch.

Nothing here touches a real worker: the remote side is a fake filesystem plus a
recording SSH double, and every command string is produced by the pure builders
in `app.execution_launch`.
"""

from __future__ import annotations

import asyncio
import socket
import sqlite3

import pytest

from app.db import Database
from app.execution_launch import (
    AMBIGUOUS_REASON_CODES,
    LAUNCHER_CONTRACT_VERSION,
    PRELAUNCH_REASON_CODES,
    REASON_CODES,
    RemoteObservation,
    build_attempt_abandon_command,
    build_attempt_inspect_command,
    build_attempt_launch_command,
    build_attempt_launch_sh_content,
    build_attempt_paths,
    build_attempt_prepare_command,
    build_attempt_run_sh_content,
    build_attempt_session_name,
    classify_arbitration_result,
    classify_launch_failure,
    classify_prepare_failure,
    parse_inspect_output,
    resolve_attempt_observation,
    unreachable_resolution,
)

ATTEMPT = "3f2b1c0a-8d4e-4f6a-9b7c-1e2d3f4a5b6c"
OTHER_ATTEMPT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
TOKEN = "fence-token-0001"
JOB_ID = 42


# ---------------------------------------------------------------------------
# A fake worker filesystem that reproduces the ordering the launcher depends on
# ---------------------------------------------------------------------------


class FakeWorker:
    """Models only what arbitration needs: an atomic `mkdir` claim, a tmux
    session set, a receipt and a sentinel — plus the ability to stop at any
    step so a crash can be injected between any two of them."""

    def __init__(self, boot_id: str = "boot-1"):
        self.dirs: set[str] = set()
        self.files: dict[str, str] = {}
        self.sessions: set[str] = set()
        self.boot_id = boot_id
        self.launch_calls = 0

    def mkdir_atomic(self, path: str) -> bool:
        """POSIX `mkdir` without -p: succeeds exactly once."""
        if path in self.dirs:
            return False
        self.dirs.add(path)
        return True

    def run_launcher(self, job_id: int, attempt_id: str, token: str, stop_after: str = "receipt") -> str:
        paths = build_attempt_paths(job_id, attempt_id)
        if not self.mkdir_atomic(paths["claim"]):
            return "LAUNCH_CLAIM_TAKEN"
        self.launch_calls += 1
        self.files[paths["claim_attempt_id"]] = attempt_id
        self.files[paths["claim_fencing_token"]] = token
        self.files[paths["claim_boot_id"]] = self.boot_id
        if stop_after == "claim":
            return "CRASH"
        self.sessions.add(build_attempt_session_name(job_id, attempt_id))
        if stop_after == "tmux":
            return "CRASH"
        self.files[paths["receipt"]] = (
            '{"attempt_id":"%s","boot_id":"%s","fencing_token":"%s",'
            '"launcher_contract_version":"%s","session":"%s","started_at":"2026-07-27T00:00:00Z"}'
            % (
                attempt_id,
                self.boot_id,
                token,
                LAUNCHER_CONTRACT_VERSION,
                build_attempt_session_name(job_id, attempt_id),
            )
        )
        return "LAUNCH_CLAIMED"

    def controller_abandon(self, job_id: int, attempt_id: str) -> str:
        paths = build_attempt_paths(job_id, attempt_id)
        if self.mkdir_atomic(paths["claim"]):
            self.files[paths["claim_abandoned"]] = "abandoned_by_controller"
            return "CLAIM_WON"
        return "CLAIM_TAKEN"

    def finish_workload(self, job_id: int, attempt_id: str, exit_code: int) -> None:
        paths = build_attempt_paths(job_id, attempt_id)
        self.sessions.discard(build_attempt_session_name(job_id, attempt_id))
        self.files[paths["exit_code"]] = str(exit_code)

    def reboot(self, boot_id: str = "boot-2") -> None:
        self.boot_id = boot_id
        self.sessions.clear()

    def observe(self, job_id: int, attempt_id: str) -> RemoteObservation:
        paths = build_attempt_paths(job_id, attempt_id)
        return RemoteObservation(
            attempt_dir_present=paths["dir"] in self.dirs,
            claim_present=paths["claim"] in self.dirs,
            claim_attempt_id=self.files.get(paths["claim_attempt_id"]),
            claim_fencing_token=self.files.get(paths["claim_fencing_token"]),
            claim_abandoned=paths["claim_abandoned"] in self.files,
            receipt_raw=self.files.get(paths["receipt"]),
            exit_code_raw=self.files.get(paths["exit_code"]),
            exit_code_after_raw=self.files.get(paths["exit_code"]),
            boot_id=self.boot_id,
            tmux_exists=build_attempt_session_name(job_id, attempt_id) in self.sessions,
        )


# ---------------------------------------------------------------------------
# 1. Command purity and exact golden strings
# ---------------------------------------------------------------------------


def test_only_validated_ids_reach_command_strings():
    for bad in ("not-a-uuid", "../../etc", "3f2b1c0a-8d4e-4f6a-9b7c-1e2d3f4a5b6", ""):
        with pytest.raises(ValueError):
            build_attempt_prepare_command(JOB_ID, bad)
    for bad_job in (-1, "7; rm -rf /", True, 1.5):
        with pytest.raises(ValueError):
            build_attempt_prepare_command(bad_job, ATTEMPT)


def test_golden_prepare_launch_and_abandon_commands():
    assert build_attempt_prepare_command(JOB_ID, ATTEMPT) == (
        f"mkdir -p agent_jobs/42/attempts/{ATTEMPT}"
    )
    assert build_attempt_launch_command(JOB_ID, ATTEMPT) == (
        f"setsid bash agent_jobs/42/attempts/{ATTEMPT}/launch.sh"
        " < /dev/null > /dev/null 2>&1"
    )
    assert build_attempt_abandon_command(JOB_ID, ATTEMPT) == (
        f"if mkdir agent_jobs/42/attempts/{ATTEMPT}/claim 2>/dev/null; then "
        f"printf abandoned_by_controller > agent_jobs/42/attempts/{ATTEMPT}/claim/abandoned; "
        "echo CLAIM_WON; else echo CLAIM_TAKEN; fi"
    )


def test_prepare_is_mkdir_p_and_claim_is_not():
    """`mkdir -p` can never arbitrate: it succeeds on an existing directory."""
    assert " -p " in build_attempt_prepare_command(JOB_ID, ATTEMPT)
    launcher = build_attempt_launch_sh_content(JOB_ID, ATTEMPT, TOKEN)
    assert 'mkdir "$D/claim"' in launcher
    assert 'mkdir -p "$D/claim"' not in launcher
    abandon = build_attempt_abandon_command(JOB_ID, ATTEMPT)
    assert "mkdir agent_jobs" in abandon and "mkdir -p" not in abandon


def test_user_bytes_and_token_never_enter_a_command_string():
    """INV-SSH-4: the token and the command text land as SFTP file content."""
    for command in (
        build_attempt_prepare_command(JOB_ID, ATTEMPT),
        build_attempt_launch_command(JOB_ID, ATTEMPT),
        build_attempt_abandon_command(JOB_ID, ATTEMPT),
        build_attempt_inspect_command(JOB_ID, ATTEMPT),
    ):
        assert TOKEN not in command
    assert TOKEN in build_attempt_launch_sh_content(JOB_ID, ATTEMPT, TOKEN)


def test_launcher_rejects_a_token_carrying_shell_metacharacters():
    for bad in ("tok'; rm -rf /", "short", "tok\nnewline", "a" * 200):
        with pytest.raises(ValueError):
            build_attempt_launch_sh_content(JOB_ID, ATTEMPT, bad)


def test_wrapper_traps_produce_a_real_numeric_sentinel():
    """An approved stop must yield a sentinel written by the workload's own
    shell; the controller may never fabricate a terminal value."""
    wrapper = build_attempt_run_sh_content(JOB_ID, ATTEMPT)
    assert "trap 'exit 143' TERM" in wrapper
    assert "trap 'exit 130' INT" in wrapper
    assert "trap 'exit 129' HUP" in wrapper
    assert 'mv -f "$D/exit_code.tmp" "$D/exit_code"' in wrapper


def test_receipt_is_published_by_atomic_rename():
    launcher = build_attempt_launch_sh_content(JOB_ID, ATTEMPT, TOKEN)
    assert '> "$D/receipt.json.tmp"' in launcher
    assert 'mv -f "$D/receipt.json.tmp" "$D/receipt.json"' in launcher
    # The claim must be taken before the session, and the session before the
    # receipt; any other order allows a second launch.
    assert launcher.index('mkdir "$D/claim"') < launcher.index("tmux new-session")
    assert launcher.index("tmux new-session") < launcher.index("receipt.json.tmp")


def test_sentinel_is_attempt_scoped_so_a_stale_one_cannot_be_reused():
    mine = build_attempt_paths(JOB_ID, ATTEMPT)["exit_code"]
    other = build_attempt_paths(JOB_ID, OTHER_ATTEMPT)["exit_code"]
    assert mine != other
    assert f"attempts/{ATTEMPT}" in mine


def test_session_name_is_per_attempt():
    assert build_attempt_session_name(JOB_ID, ATTEMPT) != build_attempt_session_name(
        JOB_ID, OTHER_ATTEMPT
    )


def test_inspect_reads_the_sentinel_on_both_sides_of_the_tmux_probe():
    command = build_attempt_inspect_command(JOB_ID, ATTEMPT)
    assert command.index("EXIT_CODE=") < command.index("TMUX=")
    assert command.index("TMUX=") < command.index("EXIT_CODE_AFTER=")


# ---------------------------------------------------------------------------
# 2. Classification: the default is ambiguous
# ---------------------------------------------------------------------------


class PermissionDenied(Exception):
    """Named exactly like asyncssh's, because the classifier matches on the
    type name rather than importing asyncssh."""

    pass


class HostKeyNotVerifiable(Exception):
    pass


class ConnectionLost(Exception):
    pass


@pytest.mark.parametrize(
    "exc,reason",
    [
        (ConnectionRefusedError(), "prelaunch_transport_refused"),
        (socket.gaierror(), "prelaunch_transport_refused"),
        (PermissionDenied(), "prelaunch_auth_rejected"),
        (HostKeyNotVerifiable(), "prelaunch_hostkey_rejected"),
    ],
)
def test_definite_transport_failures_may_requeue(exc, reason):
    verdict = classify_launch_failure(exc, effect_started=False)
    assert verdict.verdict == "definite_not_launched"
    assert verdict.reason_code == reason
    assert verdict.transmission_state == "not_transmitted"
    assert verdict.may_requeue is True


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        asyncio.TimeoutError(),
        ConnectionLost(),
        ConnectionResetError(),
        BrokenPipeError(),
        RuntimeError("something nobody enumerated"),
        Exception("bare"),
        KeyError("weird"),
    ],
)
def test_ambiguous_failures_never_requeue(exc):
    verdict = classify_launch_failure(exc, effect_started=False)
    assert verdict.verdict == "ambiguous"
    assert verdict.may_requeue is False
    assert verdict.transmission_state == "unknown"
    assert verdict.reason_code in AMBIGUOUS_REASON_CODES


def test_unknown_exception_type_defaults_to_ambiguous():
    class SomethingNew(Exception):
        pass

    verdict = classify_launch_failure(SomethingNew(), effect_started=False)
    assert verdict.reason_code == "ambiguous_unclassified"
    assert verdict.may_requeue is False


def test_transport_evidence_is_inadmissible_once_the_effect_started():
    """Gate §4.1 rule 2: after the launch effect began, only arbitration can
    produce a definite verdict."""
    verdict = classify_launch_failure(ConnectionRefusedError(), effect_started=True)
    assert verdict.verdict == "ambiguous"
    assert verdict.may_requeue is False


def test_prepare_failure_is_definite_only_before_any_launch_effect():
    assert classify_prepare_failure(launch_effect_started=False).may_requeue is True
    assert classify_prepare_failure(launch_effect_started=True).may_requeue is False


def test_every_reason_code_is_in_the_closed_set():
    produced = {
        classify_launch_failure(ConnectionRefusedError(), False).reason_code,
        classify_launch_failure(TimeoutError(), False).reason_code,
        classify_prepare_failure(False).reason_code,
        classify_arbitration_result("CLAIM_WON").reason_code,
        classify_arbitration_result("CLAIM_TAKEN").reason_code,
        unreachable_resolution().reason_code,
    }
    assert produced <= REASON_CODES


# ---------------------------------------------------------------------------
# 3. Arbitration, not observation
# ---------------------------------------------------------------------------


def test_controller_winning_the_claim_is_the_only_definite_post_effect_verdict():
    worker = FakeWorker()
    assert worker.controller_abandon(JOB_ID, ATTEMPT) == "CLAIM_WON"
    verdict = classify_arbitration_result("CLAIM_WON")
    assert verdict.may_requeue is True
    assert verdict.reason_code == "prelaunch_claim_won_by_controller"


def test_a_launcher_that_arrives_after_the_controller_won_never_launches():
    """The whole point of arbitration: the late launcher loses deterministically
    instead of racing a settle window."""
    worker = FakeWorker()
    assert worker.controller_abandon(JOB_ID, ATTEMPT) == "CLAIM_WON"
    assert worker.run_launcher(JOB_ID, ATTEMPT, TOKEN) == "LAUNCH_CLAIM_TAKEN"
    assert worker.launch_calls == 0
    assert worker.sessions == set()


def test_controller_loses_when_the_launcher_claimed_first_and_must_not_requeue():
    worker = FakeWorker()
    assert worker.run_launcher(JOB_ID, ATTEMPT, TOKEN) == "LAUNCH_CLAIMED"
    assert worker.controller_abandon(JOB_ID, ATTEMPT) == "CLAIM_TAKEN"
    assert classify_arbitration_result("CLAIM_TAKEN").may_requeue is False


def test_two_launchers_racing_the_same_claim_produce_exactly_one_launch():
    worker = FakeWorker()
    first = worker.run_launcher(JOB_ID, ATTEMPT, TOKEN)
    second = worker.run_launcher(JOB_ID, ATTEMPT, TOKEN)
    assert {first, second} == {"LAUNCH_CLAIMED", "LAUNCH_CLAIM_TAKEN"}
    assert worker.launch_calls == 1
    assert len(worker.sessions) == 1


def test_unparseable_arbitration_output_stays_ambiguous():
    for output in ("", "garbage", "CLAIM_TAKEN", None):
        assert classify_arbitration_result(output or "").may_requeue is False


# ---------------------------------------------------------------------------
# 4. Crash matrix (gate §12)
# ---------------------------------------------------------------------------


def test_crash_after_claim_before_tmux_stays_unknown():
    """Nothing may requeue here: the claim is taken, so non-launch cannot be
    proven, and no evidence says the workload is running either."""
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, ATTEMPT, TOKEN, stop_after="claim")
    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.job_status is None
    assert resolution.attempt_state is None
    assert resolution.liveness == "unknown"
    assert resolution.reason_code == "unknown_claimed_without_evidence"
    assert worker.controller_abandon(JOB_ID, ATTEMPT) == "CLAIM_TAKEN"


def test_crash_after_tmux_before_receipt_is_running_not_requeue():
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, ATTEMPT, TOKEN, stop_after="tmux")
    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.attempt_state == "running"
    assert resolution.job_status is None
    assert resolution.liveness == "known"


def test_the_rb_launch_001_case_tmux_started_but_response_lost():
    """The defect this gate exists to fix: the launch response is lost after
    tmux already forked. The Job must stay running."""
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, ATTEMPT, TOKEN)
    verdict = classify_launch_failure(TimeoutError(), effect_started=True)
    assert verdict.may_requeue is False

    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.attempt_state == "running"
    assert resolution.job_status is None
    assert worker.launch_calls == 1


def test_sentinel_present_is_the_only_terminal_evidence():
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, ATTEMPT, TOKEN)
    worker.finish_workload(JOB_ID, ATTEMPT, 0)
    done = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert (done.attempt_state, done.job_status, done.exit_code) == ("done", "done", 0)

    worker2 = FakeWorker()
    worker2.run_launcher(JOB_ID, ATTEMPT, TOKEN)
    worker2.finish_workload(JOB_ID, ATTEMPT, 7)
    failed = resolve_attempt_observation(worker2.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert (failed.attempt_state, failed.job_status, failed.exit_code) == ("failed", "failed", 7)


def test_sentinel_written_before_the_db_converged_still_resolves_terminally():
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, ATTEMPT, TOKEN)
    worker.finish_workload(JOB_ID, ATTEMPT, 0)
    first = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    second = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert first == second


def test_workload_finishing_during_the_probe_is_not_read_as_missing():
    """The sentinel appears between the two reads; the late read wins and the
    attempt resolves terminally instead of looking like a vanished session."""
    observation = RemoteObservation(
        attempt_dir_present=True,
        claim_present=True,
        claim_attempt_id=ATTEMPT,
        claim_fencing_token=TOKEN,
        receipt_raw='{"boot_id":"boot-1"}',
        exit_code_raw=None,
        exit_code_after_raw="0",
        boot_id="boot-1",
        tmux_exists=False,
    )
    resolution = resolve_attempt_observation(observation, ATTEMPT, TOKEN)
    assert resolution.attempt_state == "done"
    assert resolution.exit_code == 0


def test_launched_but_vanished_on_the_same_boot_stays_unknown(monkeypatch):
    """Gate D-2: stricter than the legacy requeue on purpose. Auto-requeue here
    would re-run a workload killed by OOM or a manual tmux kill."""
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, ATTEMPT, TOKEN)
    worker.sessions.clear()  # killed, no sentinel written
    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.job_status is None
    assert resolution.liveness == "unknown"
    assert resolution.reason_code == "unknown_launched_without_terminal"


def test_host_reboot_is_positive_proof_of_death_and_may_requeue():
    """Gate D-3: the boot id changed, so the workload cannot still be running
    and a new attempt cannot duplicate it."""
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, ATTEMPT, TOKEN)
    worker.reboot("boot-2")
    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.attempt_state == "failed"
    assert resolution.job_status == "queued"
    assert resolution.reason_code == "host_rebooted_before_terminal"


def test_reboot_does_not_override_an_existing_sentinel():
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, ATTEMPT, TOKEN)
    worker.finish_workload(JOB_ID, ATTEMPT, 0)
    worker.reboot("boot-2")
    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.attempt_state == "done"


def test_stale_or_mismatched_claim_token_is_never_believed():
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, ATTEMPT, "fence-token-9999")
    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.reason_code == "unknown_claim_token_mismatch"
    assert resolution.job_status is None
    assert resolution.attempt_state is None


def test_a_previous_attempts_evidence_cannot_resolve_this_attempt():
    worker = FakeWorker()
    worker.run_launcher(JOB_ID, OTHER_ATTEMPT, TOKEN)
    worker.finish_workload(JOB_ID, OTHER_ATTEMPT, 0)
    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.attempt_state is None
    assert resolution.job_status is None


def test_controller_abandoned_claim_resolves_to_requeue():
    worker = FakeWorker()
    worker.controller_abandon(JOB_ID, ATTEMPT)
    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.attempt_state == "abandoned_before_launch"
    assert resolution.job_status == "queued"


def test_unreachable_target_changes_nothing():
    resolution = unreachable_resolution()
    assert resolution.attempt_state is None
    assert resolution.job_status is None
    assert resolution.liveness == "unknown"


def test_nothing_on_the_worker_at_all_does_not_requeue_by_itself():
    """Absence of a claim is not the verdict — winning it is."""
    worker = FakeWorker()
    resolution = resolve_attempt_observation(worker.observe(JOB_ID, ATTEMPT), ATTEMPT, TOKEN)
    assert resolution.job_status is None
    assert resolution.attempt_state is None


def test_no_observation_ever_yields_a_second_launch_target():
    """Sweep the matrix: no resolution may produce a terminal `failed` Job from
    absence, and only two proven cases may requeue."""
    worker = FakeWorker()
    cases = []
    for stop_after in ("claim", "tmux", "receipt"):
        w = FakeWorker()
        w.run_launcher(JOB_ID, ATTEMPT, TOKEN, stop_after=stop_after)
        cases.append(w.observe(JOB_ID, ATTEMPT))
    cases.append(worker.observe(JOB_ID, ATTEMPT))

    for observation in cases:
        resolution = resolve_attempt_observation(observation, ATTEMPT, TOKEN)
        assert resolution.job_status in (None, "queued", "done", "failed")
        if resolution.job_status == "queued":
            assert resolution.reason_code in {
                "resolved_abandoned_by_controller",
                "host_rebooted_before_terminal",
            }


def test_parse_inspect_output_infers_nothing_from_missing_keys():
    observation = parse_inspect_output("")
    assert observation.claim_present is False
    assert observation.claim_attempt_id is None
    assert observation.tmux_exists is False
    assert observation.exit_code_raw is None

    parsed = parse_inspect_output(
        "ATTEMPT_DIR=present\nCLAIM=present\nCLAIM_ATTEMPT_ID=%s\n"
        "CLAIM_FENCING_TOKEN=%s\nCLAIM_ABANDONED=no\nRECEIPT=\nEXIT_CODE=\n"
        "BOOT_ID=boot-1\nTMUX=EXISTS\nEXIT_CODE_AFTER=" % (ATTEMPT, TOKEN)
    )
    assert parsed.claim_attempt_id == ATTEMPT
    assert parsed.tmux_exists is True
    assert parsed.receipt_raw is None


# ---------------------------------------------------------------------------
# 5. Schema delta: additive, domain-enforced, and honest about legacy rows
# ---------------------------------------------------------------------------

_LEGACY_WP1A_ATTEMPTS_SCHEMA = """
CREATE TABLE execution_attempts (
    id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    backend TEXT NOT NULL,
    server_name TEXT NOT NULL,
    server_config_revision_id TEXT NOT NULL,
    target_identity_sha256 TEXT NOT NULL,
    execution_approval_id INTEGER NOT NULL,
    approved_payload_sha256 TEXT NOT NULL,
    execution_contract_version TEXT NOT NULL,
    state TEXT NOT NULL,
    liveness TEXT NOT NULL,
    fencing_token TEXT NOT NULL UNIQUE,
    scheduler_fencing_epoch INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def test_fresh_db_has_the_new_launch_columns(tmp_path):
    Database(str(tmp_path / "fresh.db"))
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(execution_attempts)")}
    assert {
        "remote_claim_state",
        "launch_receipt_sha256",
        "remote_boot_id",
        "launcher_contract_version",
        "prelaunch_verdict",
    } <= columns
    op_columns = {row[1] for row in conn.execute("PRAGMA table_info(execution_operations)")}
    assert "transmission_state" in op_columns
    rev_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(server_config_revisions)")
    }
    assert "attempt_backend_preflight" in rev_columns


def test_legacy_wp1a_attempt_row_migrates_with_null_launch_evidence(tmp_path):
    """A pre-WP-2B attempt has no claim, receipt or verdict. Migration must
    leave every new column NULL rather than fabricate an observation."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(_LEGACY_WP1A_ATTEMPTS_SCHEMA)
    raw.execute(
        "INSERT INTO execution_attempts (id, job_id, attempt_number, backend,"
        " server_name, server_config_revision_id, target_identity_sha256,"
        " execution_approval_id, approved_payload_sha256,"
        " execution_contract_version, state, liveness, fencing_token,"
        " scheduler_fencing_epoch, created_at)"
        " VALUES ('legacy-attempt', 1, 1, 'ssh', 'compute-a', 'rev-1', 'sha',"
        " 1, 'sha', 'v1', 'running', 'unknown', 'legacy-token', 1, 'legacy')"
    )
    raw.commit()
    raw.close()

    Database(str(path))

    conn = sqlite3.connect(str(path))
    row = conn.execute(
        "SELECT remote_claim_state, launch_receipt_sha256, remote_boot_id,"
        " launcher_contract_version, prelaunch_verdict"
        " FROM execution_attempts WHERE id = 'legacy-attempt'"
    ).fetchone()
    assert row == (None, None, None, None, None)


def test_migrated_db_enforces_the_value_domains_by_trigger(tmp_path):
    """ALTER TABLE cannot add a CHECK, so a migrated DB must get the same
    enforcement from triggers or it would silently accept junk."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(_LEGACY_WP1A_ATTEMPTS_SCHEMA)
    raw.execute(
        "INSERT INTO execution_attempts (id, job_id, attempt_number, backend,"
        " server_name, server_config_revision_id, target_identity_sha256,"
        " execution_approval_id, approved_payload_sha256,"
        " execution_contract_version, state, liveness, fencing_token,"
        " scheduler_fencing_epoch, created_at)"
        " VALUES ('legacy-attempt', 1, 1, 'ssh', 'compute-a', 'rev-1', 'sha',"
        " 1, 'sha', 'v1', 'running', 'unknown', 'legacy-token', 1, 'legacy')"
    )
    raw.commit()
    raw.close()

    Database(str(path))
    conn = sqlite3.connect(str(path))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE execution_attempts SET remote_claim_state = 'nonsense'"
            " WHERE id = 'legacy-attempt'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE execution_attempts SET prelaunch_verdict = 'maybe'"
            " WHERE id = 'legacy-attempt'"
        )


def test_a_settled_claim_state_cannot_be_walked_back(tmp_path):
    path = tmp_path / "fresh.db"
    Database(str(path))
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO execution_attempts (id, job_id, attempt_number, backend,"
        " server_name, server_config_revision_id, target_identity_sha256,"
        " execution_approval_id, approved_payload_sha256,"
        " execution_contract_version, state, liveness, fencing_token,"
        " scheduler_fencing_epoch, created_at, remote_claim_state)"
        " VALUES ('a1', 1, 1, 'ssh', 'compute-a', 'rev-1', 'sha', 1, 'sha',"
        " 'v1', 'running', 'known', 'tok-1', 1, 'now', 'launcher_claimed')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE execution_attempts SET remote_claim_state = 'unclaimed'"
            " WHERE id = 'a1'"
        )


def test_recorded_launch_evidence_is_immutable(tmp_path):
    path = tmp_path / "fresh.db"
    Database(str(path))
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO execution_attempts (id, job_id, attempt_number, backend,"
        " server_name, server_config_revision_id, target_identity_sha256,"
        " execution_approval_id, approved_payload_sha256,"
        " execution_contract_version, state, liveness, fencing_token,"
        " scheduler_fencing_epoch, created_at, launch_receipt_sha256,"
        " remote_boot_id)"
        " VALUES ('a2', 1, 1, 'ssh', 'compute-a', 'rev-1', 'sha', 1, 'sha',"
        " 'v1', 'running', 'known', 'tok-2', 1, 'now', 'digest-1', 'boot-1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE execution_attempts SET launch_receipt_sha256 = 'digest-2'"
            " WHERE id = 'a2'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE execution_attempts SET remote_boot_id = 'boot-2' WHERE id = 'a2'"
        )


# ---------------------------------------------------------------------------
# 6. WP-2B stays out of the dispatch path
# ---------------------------------------------------------------------------


def test_scheduler_reaches_the_launch_primitives_only_through_wp2c():
    """WP-2B kept the scheduler free of these primitives; WP-2C connected them
    deliberately, and only through `app.execution_dispatch`. The scheduler must
    still never build a remote command itself."""
    import app.scheduler as scheduler

    source = open(scheduler.__file__, encoding="utf-8").read()
    assert "from app.execution_dispatch import AttemptLaunchContext" in source
    assert "execution_launch" not in source


def test_legacy_builders_are_untouched():
    from app.jobqueue import build_launch_command, build_mkdir_command

    assert build_mkdir_command(42) == "mkdir -p agent_jobs/42"
    assert build_launch_command(42) == (
        "tmux new-session -d -s job_42 'bash agent_jobs/42/run.sh'"
    )


# ---------------------------------------------------------------------------
# 7. Versioned exact goldens (gate §8)
#
# Every attempt-driven remote string is pinned here verbatim. Changing any of
# them requires a new LAUNCHER_CONTRACT_VERSION and a new fixture, so an
# in-flight attempt stays interpretable after a control-plane upgrade.
# ---------------------------------------------------------------------------

_GOLDEN_V2 = {
    "prepare": "mkdir -p agent_jobs/42/attempts/" + ATTEMPT,
    "launch": (
        "setsid bash agent_jobs/42/attempts/" + ATTEMPT + "/launch.sh"
        " < /dev/null > /dev/null 2>&1"
    ),
    "abandon": (
        "if mkdir agent_jobs/42/attempts/" + ATTEMPT + "/claim 2>/dev/null; then "
        "printf abandoned_by_controller > agent_jobs/42/attempts/"
        + ATTEMPT
        + "/claim/abandoned; echo CLAIM_WON; else echo CLAIM_TAKEN; fi"
    ),
    "stop": "tmux kill-session -t job_42_a3f2b1c0a",
    "collect": (
        "ls -1 agent_jobs/42/attempts/" + ATTEMPT + "/results 2>/dev/null"
        " | head -c 65536"
    ),
}


def test_versioned_golden_commands_are_byte_exact():
    from app.execution_launch import (
        build_attempt_collect_command,
        build_attempt_stop_command,
    )

    assert LAUNCHER_CONTRACT_VERSION == "v2", (
        "bumping the contract version requires a new golden fixture set, "
        "keeping this one in the tree for in-flight attempts"
    )
    assert build_attempt_prepare_command(JOB_ID, ATTEMPT) == _GOLDEN_V2["prepare"]
    assert build_attempt_launch_command(JOB_ID, ATTEMPT) == _GOLDEN_V2["launch"]
    assert build_attempt_abandon_command(JOB_ID, ATTEMPT) == _GOLDEN_V2["abandon"]
    assert build_attempt_stop_command(JOB_ID, ATTEMPT) == _GOLDEN_V2["stop"]
    assert build_attempt_collect_command(JOB_ID, ATTEMPT) == _GOLDEN_V2["collect"]


def test_stop_targets_this_attempts_session_not_the_whole_job():
    """A legacy `job_42` target would also kill another attempt's session."""
    from app.execution_launch import build_attempt_stop_command

    mine = build_attempt_stop_command(JOB_ID, ATTEMPT)
    other = build_attempt_stop_command(JOB_ID, OTHER_ATTEMPT)
    assert mine != other
    assert mine.endswith("job_42_a3f2b1c0a")
    assert "tmux kill-session -t job_42" != mine


def test_launcher_and_wrapper_bytes_are_pinned():
    launcher = build_attempt_launch_sh_content(JOB_ID, ATTEMPT, TOKEN)
    wrapper = build_attempt_run_sh_content(JOB_ID, ATTEMPT)
    assert launcher.startswith("#!/bin/bash\n# dispatch-center attempt launcher, contract version v2\n")
    assert wrapper.startswith("#!/bin/bash\n# dispatch-center attempt wrapper, contract version v2\n")
    assert "LAUNCH_CLAIM_TAKEN" in launcher and "LAUNCH_CLAIMED" in launcher
