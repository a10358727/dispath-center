"""WP-2B launch arbitration primitives (DG-AMBIGUOUS-LAUNCH-v1).

This module is pure. It builds exact remote command strings and launcher bytes,
classifies launch failures, and resolves what a remote observation means. It
performs no I/O or connection management; I/O lives in the attempt dispatcher
and owner outbox worker. The attempt-driven scheduler path is rollout-gated,
while the legacy `app.scheduler.dispatch_job` path remains the default.

The contract it implements is `docs/DG_AMBIGUOUS_LAUNCH_DECISION.md`, approved
2026-07-27, which amends `INV-STATE-2`. Two properties matter more than
anything else here:

1. **Only a definite pre-launch failure may send a Job back to `queued`.** A
   timeout, a lost connection or an unrecognized error is *ambiguous*: the Job
   stays `running`, the attempt keeps its target and backend, and liveness
   becomes `unknown`.
2. **Ambiguity is resolved by arbitration, not by observation.** To declare
   that a launch never happened, the controller must itself win the same
   POSIX-atomic `mkdir` claim the launcher races for. Absence of a claim is
   never the verdict; winning it is. This is what removes the settle-window
   race — an observed-absent claim could always be created one instruction
   later, but a claim the controller owns can never be won by a launcher.

Only a validated integer job id and a validated UUID attempt id are ever
interpolated into a command string. The fencing token and every byte of user
command text reach the worker exclusively as SFTP file content (`INV-SSH-4`).
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Optional

from app.execution_contract import canonical_json, utf8_sha256
from app.jobqueue import AGENT_JOBS_DIR
from app.node_protocol import (
    MAX_ARTIFACTS_PER_REPORT,
    validate_artifact_digest,
    validate_artifact_kind,
    validate_artifact_path,
    validate_artifact_size,
)

# Bumping this requires new golden fixtures and keeping the old version in the
# tree, so attempts launched by a previous control-plane build stay
# interpretable after an upgrade (gate §8).
LAUNCHER_CONTRACT_VERSION = "v2"

_ATTEMPT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_FENCING_TOKEN_RE = re.compile(r"^[0-9a-zA-Z_-]{8,128}$")
_FULL_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# Closed reason-code set (gate §4.2 and §6). A classification is persisted at
# the moment it is made; nothing here may be re-derived at read time.
PRELAUNCH_REASON_CODES = frozenset(
    {
        "prelaunch_transport_refused",
        "prelaunch_auth_rejected",
        "prelaunch_hostkey_rejected",
        "prelaunch_prepare_failed",
        "prelaunch_claim_won_by_controller",
    }
)
AMBIGUOUS_REASON_CODES = frozenset(
    {
        "ambiguous_launch_timeout",
        "ambiguous_connection_lost",
        "ambiguous_unclassified",
    }
)
RESOLUTION_REASON_CODES = frozenset(
    {
        "resolved_terminal_sentinel",
        "resolved_running_tmux",
        "resolved_abandoned_by_controller",
        "host_rebooted_before_terminal",
        "unknown_claim_token_mismatch",
        "unknown_launched_without_terminal",
        "unknown_claimed_without_evidence",
        "unknown_target_unreachable",
    }
)
REASON_CODES = PRELAUNCH_REASON_CODES | AMBIGUOUS_REASON_CODES | RESOLUTION_REASON_CODES


def validate_job_id(job_id: int) -> int:
    """Only a real non-negative integer may reach a command string."""
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 0:
        raise ValueError(f"invalid job id for remote command: {job_id!r}")
    return job_id


def validate_attempt_id(attempt_id: str) -> str:
    if not isinstance(attempt_id, str) or not _ATTEMPT_ID_RE.match(attempt_id):
        raise ValueError(f"invalid attempt id for remote command: {attempt_id!r}")
    return attempt_id


def validate_fencing_token(token: str) -> str:
    """The token never reaches a command string, but it is embedded in SFTP'd
    launcher bytes, so it still must not carry shell metacharacters."""
    if not isinstance(token, str) or not _FENCING_TOKEN_RE.match(token):
        raise ValueError("invalid fencing token for launcher bytes")
    return token


def build_attempt_paths(job_id: int, attempt_id: str) -> dict[str, str]:
    """Attempt-scoped layout (gate §5.1).

    The terminal sentinel lives *inside* the attempt directory, so a stale
    attempt's `exit_code` can never be mistaken for the current attempt's.
    Legacy `agent_jobs/{job_id}/…` paths are untouched and keep serving legacy
    attempts forever.
    """
    validate_job_id(job_id)
    validate_attempt_id(attempt_id)
    attempt_dir = f"{AGENT_JOBS_DIR}/{job_id}/attempts/{attempt_id}"
    return {
        "dir": attempt_dir,
        "claim": f"{attempt_dir}/claim",
        "claim_attempt_id": f"{attempt_dir}/claim/attempt_id",
        "claim_fencing_token": f"{attempt_dir}/claim/fencing_token",
        "claim_boot_id": f"{attempt_dir}/claim/boot_id",
        "claim_abandoned": f"{attempt_dir}/claim/abandoned",
        "cmd_sh": f"{attempt_dir}/cmd.sh",
        "run_sh": f"{attempt_dir}/run.sh",
        "launch_sh": f"{attempt_dir}/launch.sh",
        "receipt": f"{attempt_dir}/receipt.json",
        "log": f"{attempt_dir}/job.log",
        "exit_code": f"{attempt_dir}/exit_code",
        "results": f"{attempt_dir}/results",
    }


def build_attempt_session_name(job_id: int, attempt_id: str) -> str:
    """Per-attempt tmux session (gate D-4). Reusing `job_{id}` would make a
    stale session indistinguishable from the current attempt's."""
    validate_job_id(job_id)
    validate_attempt_id(attempt_id)
    return f"job_{job_id}_a{attempt_id[:8]}"


def build_attempt_prepare_command(job_id: int, attempt_id: str) -> str:
    """`mkdir -p` the attempt directory. Deliberately *not* the claim: `-p`
    succeeds on an existing directory, so it can never arbitrate anything."""
    paths = build_attempt_paths(job_id, attempt_id)
    return f"mkdir -p {paths['dir']}"


def build_attempt_launch_command(job_id: int, attempt_id: str) -> str:
    """Invoke the SFTP'd launcher detached, so a dropped controller connection
    cannot SIGHUP it between the claim and the tmux session."""
    paths = build_attempt_paths(job_id, attempt_id)
    return f"setsid bash {paths['launch_sh']} < /dev/null > /dev/null 2>&1"


def build_attempt_abandon_command(job_id: int, attempt_id: str) -> str:
    """Controller-side arbitration (gate §5.3).

    Winning this `mkdir` proves the launcher can never launch, which is the
    only way to reach a definite verdict once the launch effect has started.
    Losing it proves nothing about success — only that someone claimed first.
    """
    paths = build_attempt_paths(job_id, attempt_id)
    return (
        f"if mkdir {paths['claim']} 2>/dev/null; then "
        f"printf abandoned_by_controller > {paths['claim_abandoned']}; "
        "echo CLAIM_WON; else echo CLAIM_TAKEN; fi"
    )


def build_attempt_stop_command(job_id: int, attempt_id: str) -> str:
    """Kill this attempt's session only (`INV-SSH-9`).

    The per-attempt session name is what makes this safe: a legacy
    `tmux kill-session -t job_{id}` would also match a session belonging to a
    different attempt of the same Job. Delivery is not terminal evidence — the
    wrapper's trap writes the real numeric sentinel, and only that converges
    the attempt.
    """
    session = build_attempt_session_name(job_id, attempt_id)
    return f"tmux kill-session -t {session}"


def build_attempt_collect_command(job_id: int, attempt_id: str) -> str:
    """List this attempt's result directory. Read-only: collection failure is
    recorded against the collect operation and never against the workload."""
    paths = build_attempt_paths(job_id, attempt_id)
    return f"ls -1 {paths['dir']}/results 2>/dev/null | head -c 65536"


class ArtifactMetadataCollectionError(ValueError):
    """Remote artifact metadata is incomplete, malformed, or out of scope."""


def _artifact_declared_root(path_pattern: str) -> str:
    """Return the non-glob directory prefix of one safe POSIX declaration."""
    validate_artifact_path(path_pattern)
    parts = path_pattern.split("/")
    fixed: list[str] = []
    for part in parts:
        if any(character in part for character in "*?["):
            break
        fixed.append(part)
    if not fixed:
        return "."
    # An exact file declaration is scanned as itself; a glob starts below the
    # nearest fixed directory.  Either way it never widens to checkout root
    # unless the reviewed declaration itself has a root-level glob.
    if len(fixed) == len(parts):
        return "/".join(fixed)
    return "/".join(fixed)


def build_attempt_artifact_metadata_collect_command(
    *,
    checkout_path: str,
    approved_git_commit: str,
    output_declarations: list[dict[str, Any]],
) -> str:
    """Build the exact read-only SSH metadata collector for one typed run.

    All roots and patterns originate in the immutable typed Run Template. The
    shell scans only those roots, ignores symlinks, sorts paths, emits a
    base64-encoded path protocol, and ends with a mandatory completion marker.
    No artifact bytes leave the worker.
    """
    if not isinstance(checkout_path, str) or not checkout_path.startswith("/"):
        raise ArtifactMetadataCollectionError("checkout path is not absolute")
    if not isinstance(approved_git_commit, str) or not _FULL_GIT_COMMIT_RE.fullmatch(
        approved_git_commit
    ):
        raise ArtifactMetadataCollectionError("approved git commit is not canonical")
    declarations: list[tuple[str, str]] = []
    for declaration in output_declarations:
        if not isinstance(declaration, dict):
            raise ArtifactMetadataCollectionError("invalid output declaration")
        kind = declaration.get("kind")
        path_pattern = declaration.get("path_pattern")
        if kind not in {"file", "directory"}:
            raise ArtifactMetadataCollectionError("invalid output declaration kind")
        if not isinstance(path_pattern, str):
            raise ArtifactMetadataCollectionError("invalid output declaration path")
        try:
            root = _artifact_declared_root(path_pattern)
        except ValueError as exc:
            raise ArtifactMetadataCollectionError("invalid output declaration path") from exc
        declarations.append((kind, path_pattern))
        # Ensure the root calculation stays coupled to declaration validation;
        # it is deliberately not an input accepted from callers.
        _ = root
    if len(declarations) > 32:
        raise ArtifactMetadataCollectionError("too many output declarations")

    script_lines = [
        "set -euo pipefail",
        f"cd -- {shlex.quote(checkout_path)}",
        f"expected_commit={shlex.quote(approved_git_commit)}",
        "actual_commit=$(git rev-parse HEAD 2>/dev/null) || exit 73",
        "[[ $actual_commit == $expected_commit ]] || exit 73",
        "count=0",
        "declare -A seen=()",
        "emit_file() {",
        "  local candidate=$1 rel size digest encoded",
        "  [[ ! -L $candidate && -f $candidate ]] || return 0",
        "  rel=${candidate#./}",
        "  [[ -n $rel && $rel != /* && $rel != *\\\\* ]] || exit 70",
        "  [[ -z ${seen[$rel]+x} ]] || return 0",
        "  count=$((count + 1))",
        f"  (( count <= {MAX_ARTIFACTS_PER_REPORT} )) || exit 71",
        "  size=$(stat -c %s -- \"$candidate\")",
        "  digest=$(sha256sum -- \"$candidate\" | awk '{print $1}')",
        "  [[ $digest =~ ^[0-9a-f]{64}$ ]] || exit 72",
        "  encoded=$(printf %s \"$rel\" | base64 | tr -d '\\n')",
        "  printf 'ARTIFACT\\t%s\\tfile\\t%s\\t%s\\n' \"$encoded\" \"$size\" \"$digest\"",
        "  seen[$rel]=1",
        "}",
        "collect_root() {",
        "  local root=$1 pattern=$2 candidate rel",
        "  [[ ! -L $root && -e $root ]] || return 0",
        "  while IFS= read -r -d '' candidate; do",
        "    rel=${candidate#./}",
        "    [[ $rel == $pattern ]] && emit_file \"$candidate\"",
        "  done < <(find -- \"$root\" -type f -print0 | sort -z)",
        "}",
    ]
    for kind, pattern in declarations:
        root = _artifact_declared_root(pattern)
        if kind == "directory":
            # A directory declaration intentionally tracks every regular file
            # below that reviewed directory, while rejecting a symlink root.
            script_lines.append(
                f"collect_root {shlex.quote(root)} {shlex.quote(pattern + '/*')}"
            )
        else:
            script_lines.append(f"collect_root {shlex.quote(root)} {shlex.quote(pattern)}")
    script_lines.append("printf 'ARTIFACT_COLLECTION_COMPLETE\\n'")
    return "bash -c " + shlex.quote("\n".join(script_lines))


def parse_attempt_artifact_metadata_output(stdout: object) -> list[tuple[str, int, str, str]]:
    """Parse one bounded collector response; any deviation fails closed."""
    if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > 262_144:
        raise ArtifactMetadataCollectionError("artifact metadata response is invalid")
    lines = stdout.splitlines()
    if not lines or lines[-1] != "ARTIFACT_COLLECTION_COMPLETE":
        raise ArtifactMetadataCollectionError("artifact metadata response is truncated")
    artifacts: list[tuple[str, int, str, str]] = []
    seen: set[str] = set()
    for line in lines[:-1]:
        fields = line.split("\t")
        if len(fields) != 5 or fields[0] != "ARTIFACT":
            raise ArtifactMetadataCollectionError("artifact metadata response is malformed")
        try:
            relative_path = base64.b64decode(fields[1], validate=True).decode("utf-8")
            relative_path = validate_artifact_path(relative_path)
            if any(ord(character) < 0x20 or ord(character) == 0x7F for character in relative_path):
                raise ValueError("artifact path contains a control character")
            kind = validate_artifact_kind(fields[2])
            if re.fullmatch(r"[0-9]+", fields[3]) is None:
                raise ValueError("artifact size must be canonical decimal")
            size_bytes = validate_artifact_size(int(fields[3]))
            sha256 = validate_artifact_digest(fields[4])
        except (UnicodeDecodeError, ValueError) as exc:
            raise ArtifactMetadataCollectionError("artifact metadata response is malformed") from exc
        if kind != "file" or relative_path in seen:
            raise ArtifactMetadataCollectionError("artifact metadata response is malformed")
        seen.add(relative_path)
        artifacts.append((relative_path, size_bytes, sha256, kind))
        if len(artifacts) > MAX_ARTIFACTS_PER_REPORT:
            raise ArtifactMetadataCollectionError("artifact metadata response exceeds maximum item count")
    return artifacts


def build_attempt_inspect_command(job_id: int, attempt_id: str) -> str:
    """One round trip returning every piece of evidence gate §6 needs.

    `EXIT_CODE` is read both before and after the tmux probe on purpose: a
    workload that finishes between the two reads would otherwise look like
    "no sentinel and no session", which is exactly the observation that must
    never be treated as evidence of non-launch.
    """
    paths = build_attempt_paths(job_id, attempt_id)
    session = build_attempt_session_name(job_id, attempt_id)
    d = paths["dir"]
    return (
        f"printf 'ATTEMPT_DIR=%s\\n' \"$(test -d '{d}' && echo present || echo missing)\"; "
        f"printf 'CLAIM=%s\\n' \"$(test -d '{d}/claim' && echo present || echo missing)\"; "
        f"printf 'CLAIM_ATTEMPT_ID=%s\\n' \"$(cat '{d}/claim/attempt_id' 2>/dev/null)\"; "
        f"printf 'CLAIM_FENCING_TOKEN=%s\\n' \"$(cat '{d}/claim/fencing_token' 2>/dev/null)\"; "
        f"printf 'CLAIM_ABANDONED=%s\\n' \"$(test -f '{d}/claim/abandoned' && echo yes || echo no)\"; "
        f"printf 'RECEIPT=%s\\n' \"$(cat '{d}/receipt.json' 2>/dev/null | tr -d '\\n')\"; "
        f"printf 'EXIT_CODE=%s\\n' \"$(cat '{d}/exit_code' 2>/dev/null)\"; "
        f"printf 'BOOT_ID=%s\\n' \"$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)\"; "
        f"printf 'TMUX=%s\\n' \"$(tmux has-session -t {session} 2>/dev/null && echo EXISTS || echo GONE)\"; "
        f"printf 'EXIT_CODE_AFTER=%s\\n' \"$(cat '{d}/exit_code' 2>/dev/null)\""
    )


def build_attempt_run_sh_content(job_id: int, attempt_id: str) -> str:
    """Wrapper bytes (SFTP only).

    The traps exist so an approved stop produces a real numeric sentinel
    written by the workload's own shell. The controller must never fabricate a
    terminal value — `INV-SSH-9` keeps the exit-code sentinel the single source
    of workload terminal truth.
    """
    paths = build_attempt_paths(job_id, attempt_id)
    d = paths["dir"]
    return (
        "#!/bin/bash\n"
        f"# dispatch-center attempt wrapper, contract version {LAUNCHER_CONTRACT_VERSION}\n"
        f"D='{d}'\n"
        "trap 'c=$?; printf \"%s\" \"$c\" > \"$D/exit_code.tmp\";"
        " mv -f \"$D/exit_code.tmp\" \"$D/exit_code\"' EXIT\n"
        "trap 'exit 143' TERM\n"
        "trap 'exit 130' INT\n"
        "trap 'exit 129' HUP\n"
        "bash \"$D/cmd.sh\" > \"$D/job.log\" 2>&1\n"
    )


def build_attempt_launch_sh_content(
    job_id: int, attempt_id: str, fencing_token: str
) -> str:
    """Launcher bytes (SFTP only).

    `mkdir "$D/claim"` — without `-p` — is the entire arbitration. A loser
    exits 0 having written nothing. Only the winner may create the tmux
    session, and the receipt is published by temp file + atomic rename so a
    crash between `tmux` and the rename can never produce a second launch.

    The receipt's JSON keys are emitted in sorted order to match
    `app.execution_contract.canonical_json`, so the digest the controller
    computes on first read is stable.
    """
    paths = build_attempt_paths(job_id, attempt_id)
    validate_fencing_token(fencing_token)
    session = build_attempt_session_name(job_id, attempt_id)
    d = paths["dir"]
    return (
        "#!/bin/bash\n"
        f"# dispatch-center attempt launcher, contract version {LAUNCHER_CONTRACT_VERSION}\n"
        "set -u\n"
        f"D='{d}'\n"
        f"ATTEMPT_ID='{attempt_id}'\n"
        f"FENCING_TOKEN='{fencing_token}'\n"
        f"SESSION='{session}'\n"
        'if ! mkdir "$D/claim" 2>/dev/null; then\n'
        "  echo LAUNCH_CLAIM_TAKEN\n"
        "  exit 0\n"
        "fi\n"
        'printf \'%s\\n\' "$ATTEMPT_ID" > "$D/claim/attempt_id"\n'
        'printf \'%s\\n\' "$FENCING_TOKEN" > "$D/claim/fencing_token"\n'
        "BOOT_ID=\"$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)\"\n"
        'printf \'%s\\n\' "$BOOT_ID" > "$D/claim/boot_id"\n'
        'tmux new-session -d -s "$SESSION" "bash $D/run.sh"\n'
        "STARTED_AT=\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"\n"
        'printf \'{"attempt_id":"%s","boot_id":"%s","fencing_token":"%s",'
        f'"launcher_contract_version":"{LAUNCHER_CONTRACT_VERSION}",'
        '"session":"%s","started_at":"%s"}\''
        ' "$ATTEMPT_ID" "$BOOT_ID" "$FENCING_TOKEN" "$SESSION" "$STARTED_AT"'
        ' > "$D/receipt.json.tmp"\n'
        'mv -f "$D/receipt.json.tmp" "$D/receipt.json"\n'
        "echo LAUNCH_CLAIMED\n"
    )


# ---------------------------------------------------------------------------
# Definite / ambiguous classification (gate §4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchVerdict:
    """`definite_not_launched` is the only verdict that may requeue a Job."""

    verdict: str  # 'definite_not_launched' | 'ambiguous'
    reason_code: str
    transmission_state: str  # 'not_transmitted' | 'unknown'

    @property
    def may_requeue(self) -> bool:
        return self.verdict == "definite_not_launched"


_DEFINITE_EXCEPTION_TYPES: tuple[tuple[str, str], ...] = (
    # (exception type name, reason code). Matched by exact type name so an
    # asyncssh import is never required to classify, and so a subclass with
    # different semantics is not silently swept into the definite set.
    ("ConnectionRefusedError", "prelaunch_transport_refused"),
    ("gaierror", "prelaunch_transport_refused"),
    ("PermissionDenied", "prelaunch_auth_rejected"),
    ("HostKeyNotVerifiable", "prelaunch_hostkey_rejected"),
)

_AMBIGUOUS_EXCEPTION_TYPES: tuple[tuple[str, str], ...] = (
    ("TimeoutError", "ambiguous_launch_timeout"),
    ("ConnectionLost", "ambiguous_connection_lost"),
    ("ConnectionResetError", "ambiguous_connection_lost"),
    ("BrokenPipeError", "ambiguous_connection_lost"),
)


def classify_launch_failure(exc: BaseException, effect_started: bool) -> LaunchVerdict:
    """Classify a launch-path exception.

    `effect_started` is the launch operation's `effect_started_at IS NOT NULL`.
    Once the controller has begun the launch effect, transport evidence is no
    longer admissible as proof of non-launch (gate §4.1 rule 2) — only the §5.3
    arbitration can produce a definite verdict from then on.

    The default is **ambiguous**. Membership of the definite set is a closed
    enumeration in code, never runtime configuration, and an unrecognized
    exception is ambiguous by construction.
    """
    type_names = {klass.__name__ for klass in type(exc).__mro__}

    if not effect_started:
        for name, reason in _DEFINITE_EXCEPTION_TYPES:
            if name in type_names:
                return LaunchVerdict(
                    verdict="definite_not_launched",
                    reason_code=reason,
                    transmission_state="not_transmitted",
                )

    for name, reason in _AMBIGUOUS_EXCEPTION_TYPES:
        if name in type_names:
            return LaunchVerdict(
                verdict="ambiguous",
                reason_code=reason,
                transmission_state="unknown",
            )

    return LaunchVerdict(
        verdict="ambiguous",
        reason_code="ambiguous_unclassified",
        transmission_state="unknown",
    )


def classify_prepare_failure(launch_effect_started: bool) -> LaunchVerdict:
    """A `prepare` failure is definite only while no launch effect ever
    started for the attempt."""
    if launch_effect_started:
        return LaunchVerdict(
            verdict="ambiguous",
            reason_code="ambiguous_unclassified",
            transmission_state="unknown",
        )
    return LaunchVerdict(
        verdict="definite_not_launched",
        reason_code="prelaunch_prepare_failed",
        transmission_state="not_transmitted",
    )


def classify_arbitration_result(output: str) -> LaunchVerdict:
    """Interpret `build_attempt_abandon_command` output.

    `CLAIM_WON` is the only definite verdict available after the launch effect
    started. Anything else — including an unparseable response — stays
    ambiguous.
    """
    if "CLAIM_WON" in (output or ""):
        return LaunchVerdict(
            verdict="definite_not_launched",
            reason_code="prelaunch_claim_won_by_controller",
            transmission_state="not_transmitted",
        )
    return LaunchVerdict(
        verdict="ambiguous",
        reason_code="ambiguous_unclassified",
        transmission_state="unknown",
    )


# ---------------------------------------------------------------------------
# Remote observation and resolution (gate §6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteObservation:
    attempt_dir_present: bool = False
    claim_present: bool = False
    claim_attempt_id: Optional[str] = None
    claim_fencing_token: Optional[str] = None
    claim_abandoned: bool = False
    receipt_raw: Optional[str] = None
    exit_code_raw: Optional[str] = None
    exit_code_after_raw: Optional[str] = None
    boot_id: Optional[str] = None
    tmux_exists: bool = False


@dataclass(frozen=True)
class AttemptResolution:
    """`attempt_state=None` means "no state change"; the attempt stays exactly
    as it is. `job_status` is only ever set when the transition is proven."""

    attempt_state: Optional[str]
    liveness: str
    job_status: Optional[str]
    reason_code: str
    exit_code: Optional[int] = None


@dataclass(frozen=True)
class LaunchEvidence:
    """Sanitized positive evidence that the exact launch effect occurred.

    A matching companion claim is mandatory.  Receipt fields are optional
    because tmux or an attempt-scoped terminal sentinel also proves launch in
    the crash window before ``receipt.json`` is renamed.
    """

    proof: str
    receipt_sha256: Optional[str] = None
    remote_boot_id: Optional[str] = None
    launcher_contract_version: Optional[str] = None


_RECEIPT_FIELDS = frozenset(
    {
        "attempt_id",
        "boot_id",
        "fencing_token",
        "launcher_contract_version",
        "session",
        "started_at",
    }
)


def _validated_receipt(
    receipt_raw: Optional[str],
    *,
    attempt_id: str,
    fencing_token: str,
    expected_session: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """Return an exact canonical receipt or ``None`` without guessing."""

    if not receipt_raw:
        return None
    try:
        value = json.loads(receipt_raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS:
        return None
    if any(not isinstance(value[field], str) or not value[field] for field in value):
        return None
    if (
        value["attempt_id"] != attempt_id
        or value["fencing_token"] != fencing_token
        or (
            expected_session is not None
            and value["session"] != expected_session
        )
    ):
        return None
    try:
        if canonical_json(value) != receipt_raw:
            return None
    except ValueError:
        return None
    return value


def launch_evidence_from_observation(
    observation: RemoteObservation,
    *,
    job_id: int,
    attempt_id: str,
    fencing_token: str,
) -> Optional[LaunchEvidence]:
    """Extract only evidence strong enough to settle an uncertain launch.

    Missing/mismatched companion identity, an abandoned controller claim, and
    an unvalidated receipt all fail closed.  The returned object contains no
    raw receipt, command, credential or fencing token.
    """

    if (
        observation.claim_abandoned
        or not observation.claim_present
        or observation.claim_attempt_id != attempt_id
        or observation.claim_fencing_token != fencing_token
    ):
        return None

    receipt = _validated_receipt(
        observation.receipt_raw,
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        expected_session=build_attempt_session_name(job_id, attempt_id),
    )
    exit_code = _parse_exit_code(observation)
    if exit_code is not None:
        proof = "terminal_sentinel"
    elif observation.tmux_exists:
        proof = "tmux_session"
    elif receipt is not None:
        proof = "launch_receipt"
    else:
        return None

    return LaunchEvidence(
        proof=proof,
        receipt_sha256=(
            utf8_sha256(observation.receipt_raw)
            if receipt is not None and observation.receipt_raw is not None
            else None
        ),
        remote_boot_id=(
            receipt["boot_id"] if receipt is not None else observation.boot_id
        ),
        launcher_contract_version=(
            receipt["launcher_contract_version"] if receipt is not None else None
        ),
    )


def parse_inspect_output(stdout: str) -> RemoteObservation:
    """Parse `build_attempt_inspect_command` output. Unknown or missing keys
    stay falsy/None; nothing is inferred."""
    fields: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()

    def _opt(name: str) -> Optional[str]:
        value = fields.get(name, "")
        return value or None

    return RemoteObservation(
        attempt_dir_present=fields.get("ATTEMPT_DIR") == "present",
        claim_present=fields.get("CLAIM") == "present",
        claim_attempt_id=_opt("CLAIM_ATTEMPT_ID"),
        claim_fencing_token=_opt("CLAIM_FENCING_TOKEN"),
        claim_abandoned=fields.get("CLAIM_ABANDONED") == "yes",
        receipt_raw=_opt("RECEIPT"),
        exit_code_raw=_opt("EXIT_CODE"),
        exit_code_after_raw=_opt("EXIT_CODE_AFTER"),
        boot_id=_opt("BOOT_ID"),
        tmux_exists=fields.get("TMUX") == "EXISTS",
    )


def _parse_exit_code(observation: RemoteObservation) -> Optional[int]:
    for raw in (observation.exit_code_raw, observation.exit_code_after_raw):
        if raw is None:
            continue
        try:
            return int(raw.strip())
        except ValueError:
            continue
    return None


def resolve_attempt_observation(
    observation: RemoteObservation,
    attempt_id: str,
    fencing_token: str,
) -> AttemptResolution:
    """Decide what a remote observation means for one attempt (gate §6).

    Never returns a requeue for "I did not see anything". The only requeue
    this function can produce is the reboot case (gate D-3), where the host
    changing its `boot_id` is positive proof that the workload is dead and a
    new attempt therefore cannot duplicate it.
    """
    # A controller-owned abandoned claim is already positive non-launch
    # evidence.  It deliberately has no launcher companion files.
    if observation.claim_abandoned:
        return AttemptResolution(
            attempt_state="abandoned_before_launch",
            liveness="known",
            job_status="queued",
            reason_code="resolved_abandoned_by_controller",
        )

    # 1. The launcher companion identity must be present and match before any
    # tmux, sentinel or receipt bytes are believed.  Missing fields are
    # unreadable evidence, not a wildcard.
    if (
        not observation.claim_present
        or observation.claim_attempt_id != attempt_id
        or observation.claim_fencing_token != fencing_token
    ):
        return AttemptResolution(
            attempt_state=None,
            liveness="unknown",
            job_status=None,
            reason_code="unknown_claim_token_mismatch",
        )

    # 3. The exit-code sentinel is the only workload terminal evidence.
    exit_code = _parse_exit_code(observation)
    if exit_code is not None:
        return AttemptResolution(
            attempt_state="done" if exit_code == 0 else "failed",
            liveness="known",
            job_status="done" if exit_code == 0 else "failed",
            reason_code="resolved_terminal_sentinel",
            exit_code=exit_code,
        )

    # 4. A live session proves launch, not outcome. A missing receipt here only
    #    means the launcher was interrupted between tmux and the rename.
    if observation.tmux_exists:
        return AttemptResolution(
            attempt_state="running",
            liveness="known",
            job_status=None,
            reason_code="resolved_running_tmux",
        )

    receipt = _validated_receipt(
        observation.receipt_raw,
        attempt_id=attempt_id,
        fencing_token=fencing_token,
    )
    receipt_boot_id = receipt["boot_id"] if receipt is not None else None

    # 5/6. Reboot is positive proof the workload is dead, so requeue here is
    #      duplicate-safe. Same boot, no session, no sentinel is *not* proof of
    #      anything and stays unknown (gate D-2/D-3).
    if receipt_boot_id is not None:
        if observation.boot_id is not None and observation.boot_id != receipt_boot_id:
            return AttemptResolution(
                attempt_state="failed",
                liveness="known",
                job_status="queued",
                reason_code="host_rebooted_before_terminal",
            )
        return AttemptResolution(
            attempt_state=None,
            liveness="unknown",
            job_status=None,
            reason_code="unknown_launched_without_terminal",
        )

    # 7. Claim taken, no receipt, no session, no sentinel. Do not arbitrate:
    #    the claim is already gone, so nothing here can prove non-launch.
    if observation.claim_present:
        return AttemptResolution(
            attempt_state=None,
            liveness="unknown",
            job_status=None,
            reason_code="unknown_claimed_without_evidence",
        )

    # 8. No claim at all. Absence is not the verdict — the caller must win the
    #    claim through `build_attempt_abandon_command` to reach a decision.
    return AttemptResolution(
        attempt_state=None,
        liveness="unknown",
        job_status=None,
        reason_code="unknown_claimed_without_evidence",
    )


def unreachable_resolution() -> AttemptResolution:
    """An unreachable target changes nothing at all (`INV-SSH-7`)."""
    return AttemptResolution(
        attempt_state=None,
        liveness="unknown",
        job_status=None,
        reason_code="unknown_target_unreachable",
    )
