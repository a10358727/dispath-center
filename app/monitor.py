"""資源監控：純函式（parse_nvidia_smi、parse_loadavg、is_idle）+ 迴圈。

迴圈每 20 秒對每台機器 SSH 執行：
    nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total \
        --format=csv,noheader,nounits; cat /proc/loadavg

parse_* 函式只吃字串、吐結構化資料，不碰網路，方便單元測試。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GpuReading:
    util_percent: float
    mem_used_mb: float
    mem_total_mb: float


@dataclass
class ServerState:
    name: str
    online: bool = False
    gpus: list[GpuReading] = field(default_factory=list)
    load1: Optional[float] = None
    updated_at: Optional[str] = None
    error: Optional[str] = None
    #: 階段 3：機器 home 目錄所在檔案系統的可用空間（bytes）。用來在總覽卡片
    #: 顯示磁碟餘量，也給 sync 的 df 剩餘空間檢查重用同一個解析函式
    #: （`parse_df_output`）。探測不到（離線、df 失敗）時是 None。
    disk_avail_bytes: Optional[int] = None

    @property
    def gpu_util_max(self) -> Optional[float]:
        if not self.gpus:
            return None
        return max(g.util_percent for g in self.gpus)

    @property
    def gpu_count(self) -> int:
        return len(self.gpus)


def parse_nvidia_smi(text: str) -> list[GpuReading]:
    """解析 `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` 輸出。

    - 多卡：每行一張卡，取全部。
    - 空輸出（無 GPU 或指令不存在）：回傳空列表。
    - 亂碼/格式不符的行：跳過該行，不整包失敗。
    """
    readings: list[GpuReading] = []
    if not text:
        return readings
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            logger.warning("nvidia-smi 輸出格式異常，跳過該行: %r", raw_line)
            continue
        try:
            util, mem_used, mem_total = (float(p) for p in parts)
        except ValueError:
            logger.warning("nvidia-smi 輸出無法解析數值，跳過該行: %r", raw_line)
            continue
        readings.append(
            GpuReading(util_percent=util, mem_used_mb=mem_used, mem_total_mb=mem_total)
        )
    return readings


def parse_loadavg(text: str) -> Optional[float]:
    """解析 `/proc/loadavg` 的 load1（第一個數字）。

    格式：`0.12 0.34 0.56 1/234 5678`
    空輸出或亂碼回傳 None（呼叫端應視為「這輪判斷不到，維持上一個狀態或
    視為未知」）。
    """
    if not text:
        return None
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if not first_line:
        return None
    parts = first_line.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        logger.warning("/proc/loadavg 輸出無法解析: %r", text)
        return None


def is_idle(
    *,
    online: bool,
    has_running_job: bool,
    is_gpu_server: bool,
    gpu_util_max: Optional[float],
    idle_gpu_util: float,
    load1: Optional[float],
    idle_load: float,
) -> bool:
    """空閒判定（純函式）。

    - 離線一律不空閒（排程器應直接跳過離線機，不會走到這裡）。
    - 已有本系統派發的 running 任務：不空閒（階段 1 每機同時最多一個任務）。
    - GPU 機：所有卡的 GPU 使用率最大值 < idle_gpu_util 才算閒；若沒有讀到
      任何 GPU 讀數（gpu_util_max is None，例如 nvidia-smi 失敗），保守判定
      為「不閒」，避免誤判在跑任務的機器為空閒。
    - CPU 機：load1 < idle_load 才算閒；load1 未知一樣保守判定為不閒。
    """
    if not online:
        return False
    if has_running_job:
        return False
    if is_gpu_server:
        if gpu_util_max is None:
            return False
        return gpu_util_max < idle_gpu_util
    else:
        if load1 is None:
            return False
        return load1 < idle_load


def build_probe_command() -> str:
    """組出監控用的 SSH 指令（供 monitor loop 呼叫，非測試重點但集中管理）。

    階段 3 追加 `---DF---` 區段：`df -Pk .`（home 目錄所在檔案系統的可用空間），
    供總覽卡片顯示磁碟餘量。刻意用新的區段標記追加在尾巴，`parse_probe_output()`
    （階段 1 就有、被測試釘住回傳 2-tuple 的既有函式）維持不動，改由
    `parse_full_probe_output()` 承接三段輸出，避免動到既有測試的介面。
    """
    return (
        "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total "
        "--format=csv,noheader,nounits 2>/dev/null; echo '---LOADAVG---'; "
        "cat /proc/loadavg; echo '---DF---'; df -Pk . 2>/dev/null | tail -1"
    )


def parse_probe_output(text: str) -> tuple[list[GpuReading], Optional[float]]:
    """把 build_probe_command() 的整包輸出拆成 (gpu讀數, load1)。

    階段 1 就有的函式，介面（回傳 2-tuple）保持不變；階段 3 的磁碟資訊改由
    `parse_full_probe_output()` 額外解析，不動這裡，避免影響既有測試。
    """
    marker = "---LOADAVG---"
    if marker in text:
        gpu_part, _, load_part = text.partition(marker)
    else:
        gpu_part, load_part = "", text
    return parse_nvidia_smi(gpu_part), parse_loadavg(load_part)


def parse_df_output(text: str) -> Optional[int]:
    """解析 `df -Pk <path>` 尾巴那一行，回傳可用空間（bytes）。

    `df -Pk` 的 POSIX 格式：`Filesystem 1024-blocks Used Available Capacity
    Mounted-on`，第 4 欄是可用空間（KB）。空輸出、格式異常、非數字都回傳
    None，呼叫端（磁碟餘量顯示、sync 前的空間檢查）要對 None 保守處理
    （磁碟餘量顯示「未知」；空間檢查則視為「無法確認，不放行」）。
    """
    if not text:
        return None
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None
    parts = lines[-1].split()
    if len(parts) < 4:
        return None
    try:
        avail_kb = float(parts[3])
    except ValueError:
        return None
    return int(avail_kb * 1024)


def parse_full_probe_output(
    text: str,
) -> tuple[list[GpuReading], Optional[float], Optional[int]]:
    """把 `build_probe_command()`（含 `---DF---` 區段）的完整輸出拆成
    (gpu 讀數, load1, 可用磁碟空間 bytes)。"""
    df_marker = "---DF---"
    if df_marker in text:
        rest, _, df_part = text.partition(df_marker)
    else:
        rest, df_part = text, ""
    gpus, load1 = parse_probe_output(rest)
    disk_avail_bytes = parse_df_output(df_part)
    return gpus, load1, disk_avail_bytes


async def probe_server(ssh_run, server_name: str) -> ServerState:
    """對單一伺服器跑一次監控探測。

    `ssh_run` 是一個 async callable：`await ssh_run(server_name, command,
    timeout) -> CommandResult`，由 sshpool 提供；測試可以注入假的
    callable，不需要真的 SSH 連線。
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = await ssh_run(server_name, build_probe_command(), 15)
    except Exception as exc:  # noqa: BLE001 - SSH 層可能丟出各種例外
        return ServerState(name=server_name, online=False, updated_at=now, error=str(exc))

    gpus, load1, disk_avail_bytes = parse_full_probe_output(result.stdout or "")
    return ServerState(
        name=server_name,
        online=True,
        gpus=gpus,
        load1=load1,
        updated_at=now,
        error=None,
        disk_avail_bytes=disk_avail_bytes,
    )
