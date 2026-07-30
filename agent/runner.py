"""Node Agent 的本機執行與重啟收斂（INV-NODE-3/4/5）。

三個保證：

1. **非插值落地**（INV-NODE-3）：核准的指令位元組先寫成 `cmd.sh`，啟動器
   只帶已驗證的識別字，且以 **argv list** 執行（`shell=False`）——自由文字
   結構性地進不了 shell。
2. **ack 先於副作用**（INV-NODE-2）：attempt identity/digest 先在 ack request
   前 `fsync`；`record_ack()`、command materialization 與 launch intent 都在
   任何啟動副作用前完成並 `fsync`。
3. **重啟不重複**（INV-NODE-5）：重啟後讀本機紀錄決定每個 attempt 怎麼辦。
   **已 ack 過的 attempt 永遠不會被重新啟動**——行程不在了就是 unknown，
   交給 control plane／人處理，不自動重跑。

`plan_restart()` 的判定必須與 control plane 的
`app.node_protocol.plan_agent_restart()` 完全一致；本套件刻意不 import
`app.*`（agent 要能單獨部署到工作機），一致性由
`tests/test_node_agent.py::test_agent_and_control_plane_restart_rules_agree`
以完整情境矩陣釘住。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


MAX_LOG_TAIL_BYTES = 16 * 1024
MAX_ARTIFACTS_PER_REPORT = 100
MAX_ARTIFACT_PATH_LENGTH = 1024


@dataclass
class LocalAttempt:
    """本機持久化的 attempt 身分（INV-NODE-2/5 的 agent 側真相）。"""

    attempt_id: str
    job_id: int
    command_sha256: str
    #: ack request 已送出但 response 尚未被本機確認；重啟後一律 unknown。
    ack_request_started: bool = False
    #: 只有寫過這個欄位（＝已向 control plane ack 成功）才允許啟動。
    acked: bool = False
    #: command bytes 已經 fsync 到本機，ack 前刻意保持 False。
    command_materialized: bool = False
    #: spawn 前的 launch intent 已經 fsync；為 True 就不得自行重啟。
    launch_intent: bool = False
    #: Explicit durable state required by the protocol contract.  ``pid`` is
    #: retained as an observation, but this field is the conservative launch
    #: gate after restart.
    not_launched: bool = True
    #: stop intent and delivery receipt are distinct from terminal evidence.
    stop_requested: bool = False
    stop_acknowledged: bool = False
    termination_reason: Optional[str] = None
    #: Local evidence queue.  It is marked collected before any remote report
    #: so a restart can continue the report without rescanning the workload.
    evidence_collected: bool = False
    log_tail: str = ""
    artifacts: list[dict] = field(default_factory=list)
    artifacts_reported: bool = False
    evidence_error: Optional[str] = None
    #: 已啟動的行程 pid；None 表示還沒啟動過。
    pid: Optional[int] = None
    terminal: bool = False
    exit_code: Optional[int] = None
    terminal_reported: bool = False


class AttemptStore:
    """attempt 的本機持久化。一個 attempt 一個目錄，狀態寫成 JSON。

    `_write()` 走 `write → flush → fsync → os.replace` 的原子替換，確保
    斷電/被 kill 之後不會讀到半寫的狀態（INV-NODE-5 要求重啟可收斂）。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def attempt_dir(self, attempt_id: str) -> Path:
        if not _is_safe_identifier(attempt_id):
            raise ValueError(f"unsafe attempt id: {attempt_id!r}")
        return self.root / attempt_id

    def _state_path(self, attempt_id: str) -> Path:
        return self.attempt_dir(attempt_id) / "attempt.json"

    def _write(self, attempt: LocalAttempt) -> None:
        directory = self.attempt_dir(attempt.attempt_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = self._state_path(attempt.attempt_id)
        tmp = target.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(asdict(attempt), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        #: The directory entry is part of the durable journal too.  Without
        #: this fsync a power loss may lose the freshly replaced state file.
        try:
            directory_fd = os.open(directory, os.O_DIRECTORY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def load(self, attempt_id: str) -> Optional[LocalAttempt]:
        path = self._state_path(attempt_id)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                return None
            #: Older journals predate the extra crash-window fields. Missing
            #: fields use dataclass defaults; infer the explicit not_launched
            #: marker from the old pid field. Unknown fields fail closed.
            allowed = set(LocalAttempt.__dataclass_fields__)
            if set(raw) - allowed:
                return None
            if "not_launched" not in raw:
                raw["not_launched"] = raw.get("pid") is None
            return LocalAttempt(**raw)
        except (json.JSONDecodeError, TypeError, ValueError, OSError):
            #: 壞掉的本機狀態 fail-closed：當成「不知道」，不當成可啟動。
            return None

    def list_all(self) -> list[LocalAttempt]:
        found = []
        for directory in sorted(self.root.iterdir()) if self.root.exists() else []:
            if not directory.is_dir():
                continue
            attempt = self.load(directory.name)
            if attempt is not None:
                found.append(attempt)
        return found

    def create(self, *, attempt_id: str, job_id: int, command_sha256: str) -> LocalAttempt:
        existing = self.load(attempt_id)
        if existing is not None:
            if (
                existing.job_id != job_id
                or existing.command_sha256 != command_sha256
            ):
                raise ValueError("attempt identity/digest conflicts with local journal")
            return existing
        attempt = LocalAttempt(
            attempt_id=attempt_id, job_id=job_id, command_sha256=command_sha256
        )
        self._write(attempt)
        return attempt

    def record_ack_request_started(self, attempt: LocalAttempt) -> LocalAttempt:
        """Persist the outbound ack intent before contacting the control plane.

        A response-loss window must not look like a never-leased attempt. The
        conservative recovery action is ``unknown`` rather than a relaunch.
        """
        attempt.ack_request_started = True
        self._write(attempt)
        return attempt

    def record_ack(self, attempt: LocalAttempt) -> LocalAttempt:
        """把「已 ack」持久化。**必須在啟動任何行程之前**成功返回
        （INV-NODE-2：ack 前不得產生副作用；也讓重啟後認得出這個 attempt
        可能已經動過手）。"""
        attempt.ack_request_started = False
        attempt.acked = True
        self._write(attempt)
        return attempt

    def record_ack_rejected(self, attempt: LocalAttempt) -> LocalAttempt:
        """Clear an ack intent only after a definitive control-plane reject."""
        attempt.ack_request_started = False
        self._write(attempt)
        return attempt

    def record_launch_intent(self, attempt: LocalAttempt) -> LocalAttempt:
        """Persist launch intent immediately before the external side effect."""
        if not attempt.acked:
            raise RuntimeError("cannot persist launch intent before acknowledgement")
        if not attempt.command_materialized:
            raise RuntimeError("cannot persist launch intent before command materialization")
        attempt.launch_intent = True
        attempt.not_launched = False
        self._write(attempt)
        return attempt

    def record_launch(self, attempt: LocalAttempt, pid: int) -> LocalAttempt:
        attempt.pid = pid
        attempt.not_launched = False
        self._write(attempt)
        return attempt

    def record_terminal(self, attempt: LocalAttempt, exit_code: int) -> LocalAttempt:
        attempt.terminal = True
        attempt.exit_code = exit_code
        if attempt.termination_reason is None:
            attempt.termination_reason = "process_exit"
        self._write(attempt)
        return attempt

    def record_stop_requested(self, attempt: LocalAttempt) -> LocalAttempt:
        attempt.stop_requested = True
        attempt.termination_reason = "stop_requested"
        self._write(attempt)
        return attempt

    def record_stop_acknowledged(self, attempt: LocalAttempt) -> LocalAttempt:
        attempt.stop_acknowledged = True
        self._write(attempt)
        return attempt

    def record_evidence(
        self,
        attempt: LocalAttempt,
        *,
        log_tail: str = "",
        artifacts: Optional[list[dict]] = None,
        error: Optional[str] = None,
    ) -> LocalAttempt:
        """Persist bounded terminal evidence before attempting network sends."""
        if not isinstance(log_tail, str):
            raise ValueError("log tail must be text")
        encoded = log_tail.encode("utf-8")
        if len(encoded) > MAX_LOG_TAIL_BYTES:
            encoded = encoded[-MAX_LOG_TAIL_BYTES:]
            log_tail = encoded.decode("utf-8", errors="replace")
        normalized = _normalize_artifacts(artifacts or [])
        attempt.evidence_collected = True
        attempt.log_tail = log_tail
        attempt.artifacts = normalized
        attempt.evidence_error = error
        self._write(attempt)
        return attempt

    def record_artifacts_reported(self, attempt: LocalAttempt) -> LocalAttempt:
        attempt.artifacts_reported = True
        self._write(attempt)
        return attempt

    def record_terminal_reported(self, attempt: LocalAttempt) -> LocalAttempt:
        attempt.terminal_reported = True
        self._write(attempt)
        return attempt

    def write_command(self, attempt_id: str, command: str) -> Path:
        """指令位元組落地成檔案（INV-NODE-3：只走檔案，永不進 shell 字串）。"""
        attempt = self.load(attempt_id)
        if attempt is None:
            raise ValueError("attempt journal missing")
        digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
        if digest != attempt.command_sha256:
            raise ValueError("command digest does not match local attempt journal")
        if not attempt.acked:
            raise RuntimeError("refusing to materialize command before acknowledgement")
        directory = self.attempt_dir(attempt_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "cmd.sh"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(command)
            handle.flush()
            os.fsync(handle.fileno())
        attempt.command_materialized = True
        self._write(attempt)
        return path


def _normalize_artifacts(artifacts: list[dict]) -> list[dict]:
    """Validate the explicit workload-produced metadata manifest.

    The agent never walks arbitrary directories or uploads bytes.  A workload
    may write ``artifacts.json`` in its attempt directory; only relative path,
    non-negative size and a SHA-256 are accepted and persisted.
    """
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS_PER_REPORT:
        raise ValueError("artifact manifest exceeds the allowed size")
    normalized: list[dict] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("artifact manifest entry must be an object")
        if set(item) != {"path", "size_bytes", "sha256"}:
            raise ValueError("artifact manifest entry has unexpected fields")
        path = item["path"]
        size = item["size_bytes"]
        digest = item["sha256"]
        if (
            not isinstance(path, str)
            or not path
            or len(path) > MAX_ARTIFACT_PATH_LENGTH
            or path.startswith("/")
            or any(char in path for char in (chr(0), "\n", "\r", "\\"))
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError("artifact path is not a safe relative path")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("artifact size is invalid")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in digest)
        ):
            raise ValueError("artifact digest is not SHA-256")
        normalized.append(
            {"path": path, "size_bytes": size, "sha256": digest.lower()}
        )
    return normalized


def _is_safe_identifier(value: str) -> bool:
    """只允許 UUID 用得到的字元——擋掉路徑穿越與任何 shell 元字元。
    必須與 `app.node_protocol._is_safe_identifier()` 等價。"""
    if not isinstance(value, str) or not value:
        return False
    return all(char.isalnum() or char == "-" for char in value)


def build_launcher_argv(attempt_id: str, workdir: str | Path) -> list[str]:
    """啟動器的 argv（**list，不是字串**）。與
    `app.node_protocol.build_node_launcher_argv()` 逐位元一致。"""
    if not _is_safe_identifier(attempt_id):
        raise ValueError(f"unsafe attempt id: {attempt_id!r}")
    return ["/bin/bash", f"{workdir}/cmd.sh"]


@dataclass(frozen=True)
class RestartDecision:
    may_launch: bool
    resume_monitoring: bool
    reason: str


def plan_restart(
    attempt: LocalAttempt, *, process_alive: bool, lease_expired: bool
) -> RestartDecision:
    """重啟後對單一本機 attempt 的處置（INV-NODE-5）。

    必須與 `app.node_protocol.plan_agent_restart()` 給出相同結論——見模組
    docstring 提到的一致性測試。
    """
    if attempt.terminal:
        return RestartDecision(False, False, "attempt already terminal")
    if process_alive:
        return RestartDecision(False, True, "process still running; resume monitoring")
    if (
        attempt.ack_request_started
        or attempt.acked
        or attempt.launch_intent
        or not attempt.not_launched
    ):
        return RestartDecision(
            False,
            True,
            "ack/launch outcome may be committed: unknown, never relaunch",
        )
    if lease_expired:
        return RestartDecision(False, False, "lease expired before ack; drop")
    return RestartDecision(True, True, "leased but never acknowledged; safe to launch")


def launch(
    store: AttemptStore,
    attempt: LocalAttempt,
    *,
    spawn=subprocess.Popen,
) -> LocalAttempt:
    """啟動工作負載。

    fail-closed 前置條件：**沒有 ack 過就不啟動**（INV-NODE-2）。`spawn`
    可注入，測試不會真的開行程。刻意用 argv list（`shell=False` 是
    `Popen` 的預設）——不經過 shell。
    """
    if not attempt.acked:
        raise RuntimeError("refusing to launch an attempt that was never acknowledged")
    if attempt.pid is not None:
        raise RuntimeError(f"attempt {attempt.attempt_id} was already launched")
    if not attempt.command_materialized:
        raise RuntimeError("refusing to launch before command materialization")
    argv = build_launcher_argv(attempt.attempt_id, store.attempt_dir(attempt.attempt_id))
    #: The intent is the last durable write before the external side effect.
    attempt = store.record_launch_intent(attempt)
    launch_kwargs = {
        "cwd": str(store.attempt_dir(attempt.attempt_id)),
        "shell": False,
        "start_new_session": True,
        "stdin": subprocess.DEVNULL,
    }
    stdout_path = store.attempt_dir(attempt.attempt_id) / "stdout.log"
    stderr_path = store.attempt_dir(attempt.attempt_id) / "stderr.log"
    stdout_handle = open(stdout_path, "ab")
    stderr_handle = open(stderr_path, "ab")
    launch_kwargs.update(stdout=stdout_handle, stderr=stderr_handle)
    try:
        try:
            process = spawn(argv, **launch_kwargs)
        except TypeError:
            #: Small injected test doubles from older suites only accepted
            #: ``(argv, cwd)``. Production ``subprocess.Popen`` must never take
            #: this fallback, so a real signature/type error remains visible.
            if spawn is subprocess.Popen:
                raise
            process = spawn(argv, cwd=launch_kwargs["cwd"])
    finally:
        #: Popen duplicates these descriptors; injected fakes do not need the
        #: handles to stay open after the launch call returns.
        stdout_handle.close()
        stderr_handle.close()
    return store.record_launch(attempt, process.pid)
