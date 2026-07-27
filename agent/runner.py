"""Node Agent 的本機執行與重啟收斂（INV-NODE-3/4/5）。

三個保證：

1. **非插值落地**（INV-NODE-3）：核准的指令位元組先寫成 `cmd.sh`，啟動器
   只帶已驗證的識別字，且以 **argv list** 執行（`shell=False`）——自由文字
   結構性地進不了 shell。
2. **ack 先於副作用**（INV-NODE-2）：`AttemptStore.record_ack()` 必須在
   啟動之前完成並 `fsync`；沒有本機紀錄就不啟動。
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

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class LocalAttempt:
    """本機持久化的 attempt 身分（INV-NODE-2/5 的 agent 側真相）。"""

    attempt_id: str
    job_id: int
    command_sha256: str
    #: 只有寫過這個欄位（＝已向 control plane ack 成功）才允許啟動。
    acked: bool = False
    #: 已啟動的行程 pid；None 表示還沒啟動過。
    pid: Optional[int] = None
    terminal: bool = False
    exit_code: Optional[int] = None


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

    def load(self, attempt_id: str) -> Optional[LocalAttempt]:
        path = self._state_path(attempt_id)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                return LocalAttempt(**json.load(handle))
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
        attempt = LocalAttempt(
            attempt_id=attempt_id, job_id=job_id, command_sha256=command_sha256
        )
        self._write(attempt)
        return attempt

    def record_ack(self, attempt: LocalAttempt) -> LocalAttempt:
        """把「已 ack」持久化。**必須在啟動任何行程之前**成功返回
        （INV-NODE-2：ack 前不得產生副作用；也讓重啟後認得出這個 attempt
        可能已經動過手）。"""
        attempt.acked = True
        self._write(attempt)
        return attempt

    def record_launch(self, attempt: LocalAttempt, pid: int) -> LocalAttempt:
        attempt.pid = pid
        self._write(attempt)
        return attempt

    def record_terminal(self, attempt: LocalAttempt, exit_code: int) -> LocalAttempt:
        attempt.terminal = True
        attempt.exit_code = exit_code
        self._write(attempt)
        return attempt

    def write_command(self, attempt_id: str, command: str) -> Path:
        """指令位元組落地成檔案（INV-NODE-3：只走檔案，永不進 shell 字串）。"""
        directory = self.attempt_dir(attempt_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "cmd.sh"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(command)
            handle.flush()
            os.fsync(handle.fileno())
        return path


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
    if attempt.acked:
        return RestartDecision(
            False,
            True,
            "acknowledged attempt with no live process: unknown, never relaunch",
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
    argv = build_launcher_argv(attempt.attempt_id, store.attempt_dir(attempt.attempt_id))
    process = spawn(argv, cwd=str(store.attempt_dir(attempt.attempt_id)))
    return store.record_launch(attempt, process.pid)
