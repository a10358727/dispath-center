"""卡死偵測（stall detection，PLAN.md E 節）：純函式判斷 `job.log` 是否
卡住不動。

- 大小增長（`current_size > prev_size`，或是第一次看到、`prev_size is
  None`）→ 重置 `changed_at`＝現在，判定不卡。
- 未增長（大小相同或變小）且距離上次「有變化」已經超過 `stall_minutes`
  分鐘 → 判定卡住。
- `current_size` 讀不到（`None`，例如 `stat` 失敗、檔案還不存在）→ 不改
  判定，原樣把 `prev_size`/`prev_changed_at` 傳回去、`is_stalled` 給
  `False`——呼叫端（`app/scheduler.py`）在拿到 `current_size is None` 時
  會直接跳過整輪（不呼叫這個函式、不更新 DB 的任何欄位、不動既有的
  `stalled_suspect` 旗標），這裡的 `None` 分支主要是讓這個行為本身也能被
  直接單元測試釘住。

只是旗標、不改 `job.status`：卡住有可能只是訓練跑得比較慢，系統不應該
自作主張判定失敗或殺任務，只提醒使用者自行判斷（見 README 設計取捨）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def parse_log_size(output: str) -> Optional[int]:
    """解析 `stat -c %s job.log 2>/dev/null` 的輸出：成功是一行數字（bytes）；
    檔案不存在或指令失敗時 stdout 是空字串，回傳 `None`。"""
    text = (output or "").strip()
    if not text:
        return None
    first_line = text.splitlines()[0].strip()
    try:
        return int(first_line)
    except ValueError:
        return None


def update_stall_state(
    prev_size: Optional[int],
    prev_changed_at: Optional[str],
    current_size: Optional[int],
    now: datetime,
    stall_minutes: int,
) -> tuple[Optional[int], Optional[str], bool]:
    """回傳 `(new_size, new_changed_at, is_stalled)`。`now` 應該是
    timezone-aware（跟 `prev_changed_at` 存的 ISO 字串一致，都用
    `app.audit.now_iso()` 的格式），否則時間相減可能因為 naive/aware 混用
    而丟例外。
    """
    if current_size is None:
        return prev_size, prev_changed_at, False

    if prev_size is None or current_size > prev_size:
        return current_size, now.isoformat(), False

    # current_size <= prev_size：未增長，檢查距離上次「有變化」多久了。
    changed_at = prev_changed_at or now.isoformat()
    elapsed_minutes = (now - datetime.fromisoformat(changed_at)).total_seconds() / 60.0
    is_stalled = elapsed_minutes >= stall_minutes
    return current_size, changed_at, is_stalled
