"""資源監控：純函式（parse_nvidia_smi、parse_loadavg、is_idle）+ 迴圈。

迴圈每 20 秒對每台機器 SSH 執行：
    nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total \
        --format=csv,noheader,nounits; cat /proc/loadavg

parse_* 函式只吃字串、吐結構化資料，不碰網路，方便單元測試。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

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
    #: Goal 2 Slice 1：RAM 探測（`free -b` Mem 行），供 server_observations
    #: 落地與之後的閒置摘要引用。探測不到（離線、free 失敗、格式異常）時是
    #: None，同 disk_avail_bytes 的保守慣例。
    mem_total_bytes: Optional[int] = None
    mem_available_bytes: Optional[int] = None
    #: Part A（總覽儀表板改版）：CPU 核心數，來自唯讀 `nproc`（INV-SSH-4
    #: 封閉指令集追加一項，同 nvidia-smi/loadavg/df/free 的既有慣例——白名單
    #: 在組指令的 Python 層,而非依賴遠端攔截）。總覽卡片用它把 load1 換算成
    #: 「CPU 負載（load1/核心）」量表；探測不到（離線、nproc 失敗/不存在）
    #: 時是 None，前端保守退回顯示 load1 原始值，絕不影響 online 判定。
    cpu_count: Optional[int] = None
    #: DG-HARDWARE-EXECUTION v1 P1（H-1）：宣告在 servers.yaml `devices:` 的
    #: 附掛裝置在場觀測，`{device_id: "present"|"absent"}`。**None＝未觀測**
    #: （離線、探測失敗、輸出缺段），不是 absent——INV-SSH-7 同構的保守語意；
    #: 沒宣告任何裝置的機器是空 dict（觀測過、無裝置可觀測）。
    devices: Optional[dict[str, str]] = None
    #: executable_present 證據（`command -v`）：`{name: bool}`。None＝該輪
    #: 未探測（離線、失敗、沒有任何 environment 宣告 executable 預檢）。
    executables: Optional[dict[str, bool]] = None

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

    Goal 2 Slice 1 再追加 `---FREE---` 區段：`free -b` 的 Mem 行，供 RAM
    total/available 落地成觀測歷史。同樣是新區段追加在尾巴，
    `parse_full_probe_output()`（3-tuple）也維持不動，改由
    `parse_capacity_probe_output()` 承接含 RAM 的完整輸出。

    Part A（總覽儀表板改版）再追加 `---NPROC---` 區段：`nproc`（CPU 核心數，
    一整數），供總覽把 load1 換算成使用率量表。仍是同一個唯讀封閉指令集
    （INV-SSH-4）多一項、新區段標記追加在尾巴——`parse_capacity_probe_output()`
    （5-tuple）維持不動，改由 `parse_capacity_probe_output_with_cpu_count()`
    承接含 CPU 核心數的完整輸出。
    """
    return (
        "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total "
        "--format=csv,noheader,nounits 2>/dev/null; echo '---LOADAVG---'; "
        "cat /proc/loadavg; echo '---DF---'; df -Pk . 2>/dev/null | tail -1; "
        "echo '---FREE---'; free -b 2>/dev/null | grep -i '^mem'; "
        "echo '---NPROC---'; nproc 2>/dev/null"
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


def parse_free_output(text: str) -> tuple[Optional[int], Optional[int]]:
    """解析 `free -b` 的 Mem 那一行，回傳 (mem_total_bytes, mem_available_bytes)。

    `free -b` 欄位（含表頭）：`Mem: total used free shared buff/cache
    available`——第 2 欄是 total，第 7 欄是 available（比 free 欄更能反映
    「還能用多少」，含可回收的 cache）。空輸出、格式異常、非數字都保守回傳
    (None, None)，同 `parse_df_output` 的既有慣例。
    """
    if not text:
        return None, None
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None, None
    parts = lines[-1].split()
    if len(parts) < 7:
        return None, None
    try:
        mem_total = int(float(parts[1]))
        mem_available = int(float(parts[6]))
    except ValueError:
        logger.warning("free -b 輸出無法解析: %r", text)
        return None, None
    return mem_total, mem_available


def parse_capacity_probe_output(
    text: str,
) -> tuple[list[GpuReading], Optional[float], Optional[int], Optional[int], Optional[int]]:
    """把 `build_probe_command()`（含 `---FREE---` 區段）的完整輸出拆成
    (gpu 讀數, load1, 可用磁碟空間 bytes, mem_total_bytes, mem_available_bytes)。

    Goal 2 Slice 1 新函式；既有 `parse_probe_output`（2-tuple）／
    `parse_full_probe_output`（3-tuple）介面維持不變，測試已釘住，不能改
    簽名。"""
    free_marker = "---FREE---"
    if free_marker in text:
        rest, _, free_part = text.partition(free_marker)
    else:
        rest, free_part = text, ""
    gpus, load1, disk_avail_bytes = parse_full_probe_output(rest)
    mem_total_bytes, mem_available_bytes = parse_free_output(free_part)
    return gpus, load1, disk_avail_bytes, mem_total_bytes, mem_available_bytes


def parse_nproc_output(text: str) -> Optional[int]:
    """解析 `nproc` 的輸出（單一整數，CPU 核心數）。

    Part A（總覽儀表板改版）新函式。空輸出、格式異常、非整數都保守回傳
    None，同 `parse_df_output`/`parse_free_output` 的既有慣例——絕不影響
    online 判定，只讓總覽的 CPU 使用率量表退回顯示 load1 原始值。
    """
    if not text:
        return None
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if not first_line:
        return None
    try:
        return int(first_line.strip())
    except ValueError:
        logger.warning("nproc 輸出無法解析: %r", text)
        return None


def parse_capacity_probe_output_with_cpu_count(
    text: str,
) -> tuple[
    list[GpuReading],
    Optional[float],
    Optional[int],
    Optional[int],
    Optional[int],
    Optional[int],
]:
    """把 `build_probe_command()`（含 `---NPROC---` 區段）的完整輸出拆成
    (gpu 讀數, load1, disk_avail_bytes, mem_total_bytes, mem_available_bytes,
    cpu_count)。

    Part A 新函式；既有 `parse_probe_output`（2-tuple）／
    `parse_full_probe_output`（3-tuple）／`parse_capacity_probe_output`
    （5-tuple）介面維持不變，測試已釘住，不能改簽名。"""
    nproc_marker = "---NPROC---"
    if nproc_marker in text:
        rest, _, nproc_part = text.partition(nproc_marker)
    else:
        rest, nproc_part = text, ""
    gpus, load1, disk_avail_bytes, mem_total_bytes, mem_available_bytes = (
        parse_capacity_probe_output(rest)
    )
    cpu_count = parse_nproc_output(nproc_part)
    return gpus, load1, disk_avail_bytes, mem_total_bytes, mem_available_bytes, cpu_count


#: DG-HARDWARE-EXECUTION v1 P1（H-1）：裝置在場探測的**封閉列舉**。每種
#: presence 形式對應一條純函式產生、只插值已驗證識別字的唯讀指令（INV-SSH-4
#: 封閉指令集追加，同 nvidia-smi/loadavg/df/free/nproc 的既有慣例——白名單在
#: 組指令的 Python 層）。自由文字永遠不進指令。
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: executable_present 證據探測的工具名（鏡射 app.project_bootstrap 的
#: `_EXECUTABLE_NAME_RE`；monitor 保持零依賴，故各自宣告、同一語彙）。
EXECUTABLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_PRESENCE_USB_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$")
_PRESENCE_SERIAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_PRESENCE_PATH_RE = re.compile(r"^/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_PRESENCE_PATH_MAX = 200


def device_presence_error(presence: object) -> Optional[str]:
    """驗證 presence 宣告字串；合法回 None，否則回中文錯誤訊息。

    封閉形式（H-1）：
      - ``usb_vidpid:<vvvv:pppp>`` → ``lsusb -d vvvv:pppp``
      - ``serial_by_id:<name>``    → ``test -e /dev/serial/by-id/<name>``
      - ``path:<absolute-path>``   → ``test -e <path>``
    """

    if not isinstance(presence, str) or not presence:
        return "presence 必須是非空字串"
    scheme, sep, value = presence.partition(":")
    if not sep:
        return "presence 必須是 <形式>:<值>（usb_vidpid / serial_by_id / path）"
    if scheme == "usb_vidpid":
        if not _PRESENCE_USB_RE.match(value):
            return f"usb_vidpid 必須是 vvvv:pppp（16 進位各 4 碼）：{value!r}"
        return None
    if scheme == "serial_by_id":
        if not _PRESENCE_SERIAL_RE.match(value):
            return f"serial_by_id 名稱只能含英數字與 . _ -（≤128）：{value!r}"
        return None
    if scheme == "path":
        if len(value) > _PRESENCE_PATH_MAX or not _PRESENCE_PATH_RE.match(value):
            return f"path 必須是絕對路徑、只含英數字與 . _ - 的節點（≤{_PRESENCE_PATH_MAX}）：{value!r}"
        if ".." in value.split("/"):
            return f"path 不可含 .. 節點：{value!r}"
        return None
    return f"presence 形式不在封閉列舉內（usb_vidpid / serial_by_id / path）：{scheme!r}"


def build_device_presence_check(presence: str) -> str:
    """presence 宣告 → 一條唯讀在場檢查指令（純函式；不合法即 raise）。"""

    error = device_presence_error(presence)
    if error is not None:
        raise ValueError(error)
    scheme, _, value = presence.partition(":")
    if scheme == "usb_vidpid":
        return f"lsusb -d {value.lower()}"
    if scheme == "serial_by_id":
        return f"test -e /dev/serial/by-id/{value}"
    return f"test -e {value}"


def build_probe_command_with_devices(devices: Sequence[Any]) -> str:
    """`build_probe_command()` ＋ `---DEVICES---` 區段（逐裝置
    `<id> present|absent`）。`devices` 是帶 `.id`/`.presence` 的物件序列
    （`app.config.DeviceSpec`；duck-typed 以免依賴方向倒轉）。空序列時回傳
    原指令不變（沒有裝置可觀測；呼叫端把 devices 狀態記成空 dict）。

    id 與 presence 在這裡**再驗證一次**（defense in depth）：任何一項不合法
    整包 raise，寧可這台機器本輪探測失敗（unknown），也不把未驗證字串放進
    SSH 指令。
    """

    return build_probe_command_with_devices_and_executables(devices)


def build_probe_command_with_devices_and_executables(
    devices: Sequence[Any],
    executables: Sequence[str] = (),
) -> str:
    """`build_probe_command()` ＋ `---DEVICES---` ＋ `---EXECUTABLES---`。

    `executables`（DG-HARDWARE-EXECUTION v1 P1，H-1）：要以唯讀
    `command -v <name>` 探測在場性的工具名（來源＝已宣告
    `executable_present` 預檢的 environment revision；名稱在宣告時已驗證，
    這裡**再驗證一次**，不合法整包 raise——寧可本輪探測失敗（unknown），
    也不把未驗證字串放進 SSH 指令）。兩段都是空序列時回傳原指令不變。
    """

    base = build_probe_command()
    if devices:
        checks: list[str] = []
        for device in devices:
            device_id = str(getattr(device, "id"))
            if not DEVICE_ID_RE.match(device_id):
                raise ValueError(f"裝置 id 不合法：{device_id!r}")
            probe = build_device_presence_check(str(getattr(device, "presence")))
            checks.append(
                f"if {probe} >/dev/null 2>&1; "
                f"then echo '{device_id} present'; else echo '{device_id} absent'; fi"
            )
        base = base + "; echo '---DEVICES---'; " + "; ".join(checks)
    if executables:
        probes: list[str] = []
        for raw_name in executables:
            name = str(raw_name)
            if not EXECUTABLE_NAME_RE.fullmatch(name):
                raise ValueError(f"工具名不合法：{name!r}")
            probes.append(
                f"if command -v {name} >/dev/null 2>&1; "
                f"then echo '{name} present'; else echo '{name} absent'; fi"
            )
        base = base + "; echo '---EXECUTABLES---'; " + "; ".join(probes)
    return base


def parse_device_probe_output(text: str) -> dict[str, str]:
    """解析 `---DEVICES---` 區段內容：每行 `<id> present|absent`；其他行
    一律忽略（容錯同 parse_nvidia_smi 慣例）。"""

    devices: dict[str, str] = {}
    for line in (text or "").strip().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        device_id, status = parts
        if status in ("present", "absent") and DEVICE_ID_RE.match(device_id):
            devices[device_id] = status
    return devices


def parse_executable_probe_output(text: str) -> dict[str, bool]:
    """解析 `---EXECUTABLES---` 區段：每行 `<name> present|absent` →
    `{name: bool}`；其他行一律忽略。"""

    executables: dict[str, bool] = {}
    for line in (text or "").strip().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        name, status = parts
        if status in ("present", "absent") and EXECUTABLE_NAME_RE.fullmatch(name):
            executables[name] = status == "present"
    return executables


def parse_capacity_probe_output_with_devices(
    text: str,
) -> tuple[
    list[GpuReading],
    Optional[float],
    Optional[int],
    Optional[int],
    Optional[int],
    Optional[int],
    Optional[dict[str, str]],
    Optional[dict[str, bool]],
]:
    """把含 `---DEVICES---`／`---EXECUTABLES---` 區段的完整輸出拆成 8-tuple。
    區段缺席該項回 None（未觀測＝unknown，不是 absent）；既有 2/3/5/6-tuple
    函式介面不變。"""

    exe_marker = "---EXECUTABLES---"
    if exe_marker in text:
        text, _, exe_part = text.partition(exe_marker)
        executables: Optional[dict[str, bool]] = parse_executable_probe_output(exe_part)
    else:
        executables = None
    marker = "---DEVICES---"
    if marker in text:
        rest, _, device_part = text.partition(marker)
        devices: Optional[dict[str, str]] = parse_device_probe_output(device_part)
    else:
        rest, devices = text, None
    gpus, load1, disk, mem_total, mem_available, cpu_count = (
        parse_capacity_probe_output_with_cpu_count(rest)
    )
    return gpus, load1, disk, mem_total, mem_available, cpu_count, devices, executables


async def probe_server(
    ssh_run,
    server_name: str,
    *,
    devices: Sequence[Any] = (),
    executables: Sequence[str] = (),
) -> ServerState:
    """對單一伺服器跑一次監控探測。

    `ssh_run` 是一個 async callable：`await ssh_run(server_name, command,
    timeout) -> CommandResult`，由 sshpool 提供；測試可以注入假的
    callable，不需要真的 SSH 連線。

    `devices`（DG-HARDWARE-EXECUTION v1 P1）：這台機器 servers.yaml 宣告的
    附掛裝置（`app.config.DeviceSpec` 序列）。有宣告時探測指令追加
    `---DEVICES---` 區段；沒宣告時指令不變、`ServerState.devices` 記空 dict。
    離線／探測失敗一律 None（未觀測＝unknown，不是 absent）。
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        command = build_probe_command_with_devices_and_executables(devices, executables)
        result = await ssh_run(server_name, command, 15)
    except Exception as exc:  # noqa: BLE001 - SSH 層可能丟出各種例外
        return ServerState(name=server_name, online=False, updated_at=now, error=str(exc))

    (
        gpus,
        load1,
        disk_avail_bytes,
        mem_total_bytes,
        mem_available_bytes,
        cpu_count,
        observed,
        observed_executables,
    ) = parse_capacity_probe_output_with_devices(result.stdout or "")
    return ServerState(
        name=server_name,
        online=True,
        gpus=gpus,
        load1=load1,
        updated_at=now,
        error=None,
        disk_avail_bytes=disk_avail_bytes,
        mem_total_bytes=mem_total_bytes,
        mem_available_bytes=mem_available_bytes,
        cpu_count=cpu_count,
        devices=(observed if devices else {}),
        executables=(observed_executables if executables else {}),
    )
