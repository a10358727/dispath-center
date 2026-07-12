"""稽核紀錄：append-only JSONL。

每行一筆 JSON：{ts, action, params, result}
鐵律第 3 條要求「每個動作寫稽核」：enqueue、dispatch、done、failed、
requeue、reject 等等都要呼叫 append_audit()。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_write_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_audit(
    action: str,
    params: dict[str, Any] | None = None,
    result: str = "ok",
    path: str | Path = "audit.jsonl",
) -> dict[str, Any]:
    """寫入一筆稽核紀錄，回傳寫入的 record（方便測試/呼叫端立即使用）。"""
    record = {
        "ts": now_iso(),
        "action": action,
        "params": params or {},
        "result": result,
    }
    line = json.dumps(record, ensure_ascii=False)
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return record


def read_audit(path: str | Path = "audit.jsonl") -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    records = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def tail_audit(path: str | Path = "audit.jsonl", n: int = 100) -> list[dict[str, Any]]:
    """讀 audit.jsonl 尾 n 行，回傳新到舊（GET /events 用）。

    稽核檔案量本階段不大，直接讀全部再切尾巴＋反轉即可，不做真正的
    「從檔尾往回讀」最佳化。
    """
    records = read_audit(path)
    return list(reversed(records[-n:]))
