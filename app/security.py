"""危險指令攔截（鐵律第 2 條）。

`is_dangerous()` 是一個**防呆黑名單**，不是完整的 shell 解析器，也不防
「惡意」繞過（例如刻意編碼過的指令、變數組合出來的指令字串等）。
目的是擋掉「使用者手滑貼錯指令」等級的意外，POST /jobs 入列時直接呼叫。

設計選擇（明文記錄，對應 PLAN.md 測試清單第 5 點）：
- 用空白/分隔符號簡單斷詞，不逐字元解析 shell 語法（quoting、變數展開等）。
- 因此像 `echo rm -rf /tmp/x`、`grep 'rm -rf'` 這種「文字上出現 rm -rf
  但實際上不會刪檔」的指令，也會被判定為危險而擋下（false positive）。
  這是刻意的取捨：寧可誤殺、不要漏放，階段 1 的攔截器只求「防呆」，
  不追求精準。若要精準判斷，之後可以換成真正的 shell AST 解析。
"""

from __future__ import annotations

import re

# 以 ; && || | 換行 當作指令邊界，粗略切成多個「片段」分別檢查。
_SEGMENT_SPLIT_RE = re.compile(r";|&&|\|\||\||\n")

_SIMPLE_WORD_PATTERNS = {
    "dd": re.compile(r"\bdd\b"),
    "mkfs": re.compile(r"\bmkfs(\.\w+)?\b"),
    "shutdown": re.compile(r"\bshutdown\b"),
    "reboot": re.compile(r"\breboot\b"),
    "userdel": re.compile(r"\buserdel\b"),
}

_DEV_SD_REDIRECT_RE = re.compile(r">>?\s*/dev/sd[a-z0-9]*")


def _strip_quotes(token: str) -> str:
    return token.strip("'\"")


def _segment_has_rm_rf(segment: str) -> bool:
    tokens = [_strip_quotes(t) for t in segment.split()]
    if "rm" not in tokens:
        return False
    has_r = False
    has_f = False
    for tok in tokens:
        if tok.startswith("--"):
            if tok == "--recursive":
                has_r = True
            if tok == "--force":
                has_f = True
        elif tok.startswith("-") and len(tok) > 1:
            flags = tok[1:]
            if "r" in flags or "R" in flags:
                has_r = True
            if "f" in flags:
                has_f = True
    return has_r and has_f


def is_dangerous(command: str) -> tuple[bool, str]:
    """回傳 (是否危險, 原因)。不危險時原因為空字串。"""
    if command is None:
        return False, ""
    normalized = re.sub(r"\s+", " ", command).strip()
    if not normalized:
        return False, ""

    if _DEV_SD_REDIRECT_RE.search(normalized):
        return True, "偵測到覆寫區塊裝置（> /dev/sd*）"

    for name, pattern in _SIMPLE_WORD_PATTERNS.items():
        if pattern.search(normalized):
            return True, f"偵測到黑名單指令：{name}"

    segments = _SEGMENT_SPLIT_RE.split(normalized)
    for segment in segments:
        if _segment_has_rm_rf(segment):
            return True, "偵測到 rm 同時帶 recursive 與 force 旗標（rm -rf 類）"

    return False, ""
