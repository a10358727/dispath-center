"""確定性閒置摘要服務（Goal 2 Slice 2，
docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md）：把 `server_observations` 的一段
歷史，摺成一份可解釋、可查詢的「這台機器有多閒」結論。

純函式模組——不 import app.main/app.scheduler/app.sshpool，不做任何排程
決策（`is_idle()`/`pick_job()` 完全不讀這個模組）。這裡只做「把已經落地
的歷史事實摺出摘要」，缺樣本一律標 unknown，不腦補。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from app.db import ServerObservation


@dataclass(frozen=True)
class IdleSummary:
    """某台伺服器在某個觀測視窗內的閒置摘要。`status == "unknown"` 時所有
    數值欄位都是 None——沒有樣本就是沒有證據，不代表「閒」或「忙」。"""

    server_name: str
    window_hours: int
    sample_count: int
    online_ratio: Optional[float]
    gpu_util_p50: Optional[float]
    gpu_util_p95: Optional[float]
    load1_p50: Optional[float]
    load1_p95: Optional[float]
    continuous_idle_seconds: Optional[int]
    freshness_seconds: Optional[int]
    status: str


def percentile(values: list[float], pct: float) -> Optional[float]:
    """線性內插百分位數（linear interpolation，同 numpy 預設的
    `interpolation="linear"`），不是 nearest-rank。空 list 回傳 None。輸入
    不需要事先排序，這裡自己排。"""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = (pct / 100.0) * (n - 1)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    frac = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * frac


def _parse_observed_at(value: str) -> Optional[datetime]:
    """`observed_at` 一律是 `now_iso()` 產生的 ISO 字串，格式異常的列（理論
    上不該發生，但防禦性處理）視為「無法確認時間」，回傳 None。"""
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def summarize_observations(
    server_name: str,
    observations: Sequence[ServerObservation],
    *,
    window_hours: int,
    now_iso: str,
    gpu_server: bool,
    idle_gpu_util: float,
    idle_load: float,
) -> IdleSummary:
    """把某台伺服器一段觀測列（`observations` 必須是
    `list_server_observations()` 回傳的**最新在前**順序）摺成 `IdleSummary`。

    - `online_ratio`：online=true 樣本的比例。
    - 各百分位只吃該欄位不是 None 的樣本；全部是 None 時該百分位是 None。
    - `continuous_idle_seconds`：從最新樣本往回走，只要樣本連續符合
      「online 且 probe_ok 且（GPU 機：gpu_util_max 有讀到且 <
      idle_gpu_util；CPU 機：load1 有讀到且 < idle_load）」就算連續閒置；
      任何一項未知（None）或不符合門檻就中斷——同 `app.monitor.is_idle()`
      的保守（fail-closed）精神：讀不到就不算閒。時長＝最新樣本
      observed_at 減「連續閒置streak 中最舊那筆」observed_at 的秒數；
      streak 長度 1（最新樣本本身閒置但再往前一筆就中斷，或只有一筆樣本）
      時時長是 0（一個時間點沒有「持續時間」）；最新樣本本身就不閒置
      （streak 長度 0）也回傳 0；完全沒有樣本回傳 None。

      刻意**不**參考 `has_running_job`——這是純粹的歷史觀測證據層，不是
      排程決策；是否有本系統派發的 running job 由呼叫端（例如未來的
      Slice 4 政策評估）自行疊加判斷，不在這個函式的職責內。
    - `freshness_seconds`：`now_iso` 減最新樣本 `observed_at` 的秒數。`
      observed_at` 格式異常（理論上不會發生，`now_iso()` 保證合法）時視為
      「這筆時間不可信」，跳過整條 streak 累積與 freshness 計算（保守
      處理，不猜測）。
    """
    if not observations:
        return IdleSummary(
            server_name=server_name,
            window_hours=window_hours,
            sample_count=0,
            online_ratio=None,
            gpu_util_p50=None,
            gpu_util_p95=None,
            load1_p50=None,
            load1_p95=None,
            continuous_idle_seconds=None,
            freshness_seconds=None,
            status="unknown",
        )

    sample_count = len(observations)
    online_count = sum(1 for o in observations if o.online)
    online_ratio = online_count / sample_count

    gpu_util_values = [o.gpu_util_max for o in observations if o.gpu_util_max is not None]
    load1_values = [o.load1 for o in observations if o.load1 is not None]

    gpu_util_p50 = percentile(gpu_util_values, 50)
    gpu_util_p95 = percentile(gpu_util_values, 95)
    load1_p50 = percentile(load1_values, 50)
    load1_p95 = percentile(load1_values, 95)

    newest = observations[0]
    newest_time = _parse_observed_at(newest.observed_at)
    now = _parse_observed_at(now_iso)
    freshness_seconds: Optional[int] = None
    if newest_time is not None and now is not None:
        freshness_seconds = int((now - newest_time).total_seconds())

    def _is_idle_like(obs: ServerObservation) -> bool:
        if not obs.online or not obs.probe_ok:
            return False
        if gpu_server:
            return obs.gpu_util_max is not None and obs.gpu_util_max < idle_gpu_util
        return obs.load1 is not None and obs.load1 < idle_load

    continuous_idle_seconds: Optional[int] = None
    if newest_time is not None:
        if not _is_idle_like(newest):
            continuous_idle_seconds = 0
        else:
            oldest_in_streak_time = newest_time
            for obs in observations[1:]:
                if not _is_idle_like(obs):
                    break
                obs_time = _parse_observed_at(obs.observed_at)
                if obs_time is None:
                    # 時間戳不可信一律中斷 streak，不猜測它落在哪個時間點。
                    break
                oldest_in_streak_time = obs_time
            continuous_idle_seconds = int(
                (newest_time - oldest_in_streak_time).total_seconds()
            )

    return IdleSummary(
        server_name=server_name,
        window_hours=window_hours,
        sample_count=sample_count,
        online_ratio=online_ratio,
        gpu_util_p50=gpu_util_p50,
        gpu_util_p95=gpu_util_p95,
        load1_p50=load1_p50,
        load1_p95=load1_p95,
        continuous_idle_seconds=continuous_idle_seconds,
        freshness_seconds=freshness_seconds,
        status="ok",
    )
