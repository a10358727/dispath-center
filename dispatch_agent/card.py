"""Agent Card: what this runner can do, from a closed set of read-only probes."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from typing import Any, Callable, Optional

TOOLCHAINS = ("python3", "git", "nvidia-smi", "vivado", "quartus", "yosys", "openocd", "esptool", "esptool.py", "platformio", "arm-none-eabi-gcc", "make", "cmake")
_NVIDIA_ARGV = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]

Which = Callable[[str], Optional[str]]
Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603


def probe_agent_card(*, runner_name: str, version: str, which: Which = shutil.which, run: Runner = _run) -> dict[str, Any]:
    toolchains = sorted(name for name in TOOLCHAINS if which(name))
    gpus: list[dict[str, Any]] = []
    if "nvidia-smi" in toolchains:
        try:
            result = run(_NVIDIA_ARGV, 10.0)
        except Exception:  # noqa: BLE001 - probe failure is not an error
            result = None
        if result is not None and result.returncode == 0:
            for line in (result.stdout or "").splitlines():
                parts = [part.strip() for part in line.split(",")]
                if len(parts) >= 2:
                    try:
                        gpus.append({"name": parts[0][:64], "memory_mb": int(parts[1])})
                    except ValueError:
                        continue
    return {
        "name": runner_name,
        "version": version,
        "protocol": "dispatch-agent/1",
        "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "capabilities": {"toolchains": toolchains, "gpus": gpus, "devices": []},
        "user": os.environ.get("USER", ""),
    }


__all__ = ["TOOLCHAINS", "probe_agent_card"]
