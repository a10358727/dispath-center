"""自動核准規則引擎（PLAN.md K.3，階段 10：核准流減摩擦）。

規則檔 `auto_approve.yaml`（路徑由呼叫端決定，通常是
`app.config.AppConfig.auto_approve_rules_path`）：**檔案不存在＝沒有規則＝
一切照舊出核准卡**（安全預設）。附 `auto_approve.yaml.example`（見專案根
目錄），內含一組**註解掉**的範例規則，不會被自動採用。

規則欄位（全部選填，有填的欄位全部符合才算命中＝AND；規則之間 OR，取
第一條命中的）：
    source        web / chatgpt / vllm / api / any（萬用）
    kind          enqueue / stop / any（萬用）
    command_regex 用 `re.match()`，從頭比對；只對 kind=enqueue 有意義
    project       精確比對 payload 的 project 欄位
    pin_server    精確比對 payload 的 pin_server 欄位

欄位就這五個，不要加。

**順序保證（鐵律第 2 條的延伸，務必讀完再改這個模組）**：
`app.security.is_dangerous()` 的黑名單檢查發生在
`app.approvals.request_enqueue_approval()` 建立 approval **之前**——危險
指令在那一步就已經被無條件拒絕（400 + 稽核 `reject`），approval 列根本
不會被建立。本模組刻意**不 import `app.security`／`app.approvals`**：
`evaluate()`/`match_rule()` 只可能在「已經通過黑名單檢查、approval 已經
成功建立」之後，才會被呼叫端（`app.approvals.maybe_auto_approve()`）拿去
諮詢——規則救不回已經被擋下的危險指令，因為這個模組壓根拿不到那些指令
（它們從沒有變成 approval，也就沒有機會被 `evaluate()` 看到）。
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

#: `get_rules()` 的 mtime 快取：key 是**正規化後**的絕對路徑字串（見
#: `_cache_key()`）——不能直接用呼叫端傳進來的原始字串當 key，否則不同測試
#: /不同工作目錄下的同名相對路徑（例如 "auto_approve.yaml"）會互相撞到彼此
#: 快取的內容（`tests/conftest.py` 的 `_isolate_cwd` 每個測試都會
#: `monkeypatch.chdir(tmp_path)`，同一個相對路徑字串在不同測試裡指向完全不
#: 同的實體檔案）。
_cache: dict[str, tuple[Optional[float], list[dict]]] = {}


def _cache_key(path: str) -> str:
    return os.path.abspath(path)


def load_rules(path: str) -> list[dict]:
    """讀取規則檔，回傳規則 dict 列表。

    檔案不存在 -> 空列表（不是錯誤，這是安全預設）。YAML 解析失敗、頂層不
    是物件、`rules` 欄位不是列表 -> 記 log、回空列表，**不擋服務啟動/評估**
    （呼叫端不應該因為使用者手滑改壞規則檔就整個炸掉）。列表裡任何一項不是
    物件（dict）-> 略過那一項並記 log，其餘規則照常生效。
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 解析失敗降級，不擋服務
        logger.warning("讀取自動核准規則檔 %s 失敗，視為沒有規則：%s", path, exc)
        return []

    if not isinstance(data, dict):
        logger.warning("自動核准規則檔 %s 格式不正確（頂層不是物件），視為沒有規則", path)
        return []

    raw_rules = data.get("rules")
    if raw_rules is None:
        return []
    if not isinstance(raw_rules, list):
        logger.warning("自動核准規則檔 %s 的 rules 欄位不是列表，視為沒有規則", path)
        return []

    rules: list[dict] = []
    for item in raw_rules:
        if isinstance(item, dict):
            rules.append(item)
        else:
            logger.warning("自動核准規則檔 %s 有一條規則不是物件，已略過：%r", path, item)
    return rules


def get_rules(path: str) -> list[dict]:
    """帶 mtime 快取的 `load_rules()`：檔案 mtime 沒變就回傳快取內容，變了
    才重新讀取（不用重啟服務就能改規則）。檔案不存在時 mtime 視為 `None`，
    之後檔案被建立/mtime 改變都會被偵測到並觸發重讀。"""
    key = _cache_key(path)
    try:
        mtime: Optional[float] = os.path.getmtime(path)
    except OSError:
        mtime = None

    cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    rules = load_rules(path)
    _cache[key] = (mtime, rules)
    return rules


@lru_cache(maxsize=256)
def _compile_regex(pattern: str):
    try:
        return re.compile(pattern)
    except re.error as exc:
        logger.warning(
            "自動核准規則的 command_regex 無效（%r），此規則永遠不會命中：%s", pattern, exc
        )
        return None


def match_rule(
    rule: dict,
    *,
    source: str,
    kind: str,
    command: Optional[str],
    project: Optional[str],
    pin_server: Optional[str],
) -> bool:
    """單一規則是否命中：規則裡**有填**的欄位全部符合才算命中（AND）。
    `source`/`kind` 的值為 `"any"` 時視為萬用（不比對）；`command_regex`
    用 `re.match()`（從頭比對，不是 `re.search()`），只對 kind=enqueue 有
    意義（kind=stop 的規則若也填了 command_regex，因為 stop 的
    `command` 一律是 `None`，只會比對失敗，不會誤命中）。regex 編譯失敗
    -> 這條規則永遠不命中（記 log，不拋例外，不影響其他規則）。
    """
    rule_source = rule.get("source")
    if rule_source and rule_source != "any" and rule_source != source:
        return False

    rule_kind = rule.get("kind")
    if rule_kind and rule_kind != "any" and rule_kind != kind:
        return False

    rule_project = rule.get("project")
    if rule_project and rule_project != project:
        return False

    rule_pin_server = rule.get("pin_server")
    if rule_pin_server and rule_pin_server != pin_server:
        return False

    pattern = rule.get("command_regex")
    if pattern:
        compiled = _compile_regex(pattern)
        if compiled is None:
            return False
        if not compiled.match(command or ""):
            return False

    return True


def evaluate(
    rules: list[dict],
    *,
    source: str,
    kind: str,
    command: Optional[str] = None,
    project: Optional[str] = None,
    pin_server: Optional[str] = None,
) -> Optional[int]:
    """依序比對規則列表，回傳**第一條**命中的規則索引（0-based）；沒有命中
    回傳 `None`。規則之間是 OR（第一條命中就採用，不繼續比對後面的規則）。
    """
    for idx, rule in enumerate(rules):
        if match_rule(
            rule,
            source=source,
            kind=kind,
            command=command,
            project=project,
            pin_server=pin_server,
        ):
            return idx
    return None
