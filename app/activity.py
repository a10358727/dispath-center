"""Project Activity：唯讀探測已註冊專案在各機器上的執行近況（PLAN.md L 節，
階段 11 — `get_project_activity`，Fable 定案）。

目的：讓 ChatGPT／vLLM agent 一次拿到某專案的完整近況（含**系統外手動跑
的訓練**留下的 log），有足夠材料給優化建議。

安全邊界（違反＝實作錯誤）：
- **只探測 DB 已登記的路徑**：呼叫端（`app/main.py` 的
  `GET /projects/{name}/activity`、`app/agent_tools.py` 的
  `get_project_activity` 工具）從 `app.db.Database.list_project_instances()`
  取得 `(server, path)`，本模組的函式**絕不接受、也絕不查詢**任意路徑
  ——`probe_instance()` 只是對呼叫端已經驗證過的 `(server, path)` 組唯讀
  指令，本身不做任何白名單以外的判斷。
- **重用 `app.inventory` 的安全常數與函式**（`should_skip_secret()`／
  `DEFAULT_EXCLUDE_NAMES`／`_quote_remote_path()`）——import 重用，不複製。
- **全程唯讀**：只組 `find`（近期變動檔案）／`tail -c`（log 尾段，限
  `LOG_TAIL_BYTES`）這兩類指令，**絕不寫遠端檔案、絕不執行使用者自訂
  指令、絕不 kill 任何東西**。
- **秘密檔案永遠不會被組進任何 tail 指令**：`find` 本身不方便逐 pattern
  排除檔名（`-prune` 只能排除目錄），所以秘密過濾在 Python 層做**兩次**
  ——`parse_recent_files_output()` 解析 find 輸出時先濾一次，
  `build_log_tail_commands()` 從候選挑 log 檔時再用 `should_skip_secret()`
  濾一次（雙保險，理由同 `app.inventory.build_readme_read_command()` 的
  防線設計）。
- **固定上限，不做可調參數**（PLAN.md L.4，Fable 裁定）：`RECENT_DAYS`＝3
  天、`MAX_RECENT_FILES`＝50 筆、`MAX_LOG_FILES`＝3 個、`LOG_TAIL_BYTES`＝
  8KB——都是模組層常數，任何呼叫端（含 agent 工具）都不能把這幾個值當
  參數傳進來調大，防止模型/使用者亂開大探測範圍。

模式同 `app/inventory.py`：純函式（build command／parse）與 async 主函式
（`probe_instance()`）分離，測試注入假 `ssh_run`，不依賴真實 SSH。

PLAN.md M 節（階段 12 — AI 改碼層次一：讀檔＋diff 核准卡，Fable 定案，
使用者明確要求越過原規格「只建議不改碼」的紅線）新增本模組三組函式：

- `build_list_files_command()`／`list_instance_files()`：列出某個已登記
  instance 底下的檔案（相對路徑，秘密檔過濾、上限 200 筆），供
  `GET /projects/{name}/files`（`app/main.py`）與 `list_project_files`
  工具（MCP bridge／`app/agent_tools.py`）使用。
- `build_read_file_command()`／`read_instance_file()`：讀單一檔案內容
  （`head -c 65536`），**呼叫端傳入的 `rel_file` 一律先過
  `validate_rel_path()`**——含 `..` 片段、以 `/` 開頭（絕對路徑）、命中
  秘密檔名一律拒絕，且**不嘗試解析**（不 SSH，直接回錯誤 dict）。同一個
  `validate_rel_path()` 也被 `app/approvals.py` 的
  `request_apply_patch_approval()` 重用來檢查 diff 內的目標路徑——兩處
  共用同一份驗證邏輯，是唯一的路徑安全防線來源，不允許各自維護一份容易
  漂移的判斷。
- `resolve_project_instance()`：「依 project 名稱＋（選填）server 找唯一
  `project_instance`」的共用 helper，供 `app/main.py` 的讀檔／diff 端點與
  `app/agent_tools.py` 的對應工具共用（不各自重寫一份「省略 server 時
  剛好一個 instance 就自動選，否則要求指定」的邏輯）。找不到、或有多個
  但沒指定 server 時丟 `ProjectInstanceResolutionError`（`ValueError`
  子類別），呼叫端轉 400。

這三組函式**仍然全程唯讀**——`list_instance_files()`／
`read_instance_file()` 只組 `find`／`head -c`，不寫遠端檔案、不執行使用者
自訂指令。寫入（`apply_patch`）發生在 `app/approvals.py` 的
`approve()`，不在本模組。
"""

from __future__ import annotations

import fnmatch
import shlex
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from app.inventory import DEFAULT_EXCLUDE_NAMES, _quote_remote_path, should_skip_secret

if TYPE_CHECKING:  # pragma: no cover - 只給型別檢查用，避免任何執行期循環 import 疑慮
    from app.db import Database, ProjectInstance

#: `find -mtime -N`：近幾天內有變動的檔案。
RECENT_DAYS = 3
#: 近期變動檔案列表的上限筆數（`head -N`）。
MAX_RECENT_FILES = 50
#: log 尾段最多抓幾個檔案。
MAX_LOG_FILES = 3
#: 每個 log 檔尾段最多讀幾個 bytes（`tail -c N`）。
LOG_TAIL_BYTES = 8192
#: PLAN.md M.1：`list_instance_files()` 的 `find -maxdepth`。
LIST_FILES_MAX_DEPTH = 4
#: PLAN.md M.1：`list_instance_files()` 回傳檔案列表的上限筆數。
MAX_LIST_FILES = 200
#: PLAN.md M.1：`read_instance_file()` 單檔讀取上限（64KB，比 log tail 的
#: 8KB 大一點——程式碼檔案比 log 尾段需要更完整的內容才看得懂）。
MAX_READ_FILE_BYTES = 65536

#: log 候選檔名比對（`fnmatch`，比對相對路徑的 basename）：`*.log`／
#: `*.out`，或者檔名含 "train"（不分大小寫）的 `*.txt`。
_LOG_SUFFIX_PATTERNS = ["*.log", "*.out"]


# ---------------------------------------------------------------------------
# 純函式：指令組裝
# ---------------------------------------------------------------------------


def build_recent_files_command(
    path: str, exclude_names: list[str], days: int = RECENT_DAYS
) -> str:
    """組出唯讀 `find` 指令：找 `path` 底下最近 `days` 天內有變動的檔案，
    `exclude_names` 用 `-prune` 排除（不會進去那些目錄搜尋，組法同
    `app.inventory.build_find_command()`），輸出用 `-printf '%T@ %P\\n'`
    （mtime epoch + 相對 `path` 的路徑），`sort -rn` 依 mtime 新到舊排序後
    `head -N` 截斷到 `MAX_RECENT_FILES` 筆——截斷發生在遠端 shell，不是先
    整批傳回來再在 Python 層砍，避免巨型專案目錄把輸出撐爆。
    """
    root = _quote_remote_path(path)

    prune_clause = ""
    if exclude_names:
        prune_names = " -o ".join(f"-name {_q(n)}" for n in exclude_names)
        prune_clause = f"\\( {prune_names} \\) -prune -o "

    return (
        f"find {root} {prune_clause}-mtime -{days} -type f "
        f"-printf '%T@ %P\\n' 2>/dev/null | sort -rn | head -{MAX_RECENT_FILES}"
    )


def _q(value: str) -> str:
    """本模組內部小工具：`shlex.quote()` 的簡短別名——跟
    `app.inventory._quote_remote_path()` 處理 `~` 開頭路徑的邏輯是分開的
    兩件事，這裡只用來 quote 純目錄名（`exclude_names`），不會出現 `~`。"""
    return shlex.quote(value)


def parse_recent_files_output(text: str) -> list[dict]:
    """解析 `build_recent_files_command()` 的輸出：每行 `"<mtime_epoch>
    <相對路徑>"`，回傳 `[{"path": 相對路徑, "mtime_epoch": float}, ...]`。

    **秘密檔案在這裡就濾掉**（`should_skip_secret()`，比對相對路徑的
    basename）——即使遠端某個秘密檔案剛好落在 `find` 掃到的範圍內（find
    本身沒有逐檔名排除），也不會出現在回傳的近期檔案列表裡，更不會被後續
    `build_log_tail_commands()` 選中去 tail。
    """
    results: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        mtime_str, sep, rel_path = line.partition(" ")
        if not sep:
            continue
        rel_path = rel_path.strip()
        if not rel_path:
            continue
        try:
            mtime_epoch = float(mtime_str)
        except ValueError:
            continue
        if should_skip_secret(rel_path):
            continue
        results.append({"path": rel_path, "mtime_epoch": mtime_epoch})
    return results


def _is_log_candidate(rel_path: str) -> bool:
    base = rel_path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(base, pattern) for pattern in _LOG_SUFFIX_PATTERNS):
        return True
    if fnmatch.fnmatch(base, "*.txt") and "train" in base.lower():
        return True
    return False


def build_log_tail_commands(path: str, candidate_files: list[dict]) -> dict[str, str]:
    """從 `candidate_files`（`parse_recent_files_output()` 的結果，已經過
    一次 `should_skip_secret()` 過濾）挑出 log 候選檔：檔名符合 `*.log`／
    `*.out`，或者檔名含 "train" 的 `*.txt`；依 `mtime_epoch` 新到舊排序，
    取至多 `MAX_LOG_FILES` 個。

    **這裡再用 `should_skip_secret()` 過濾一次**（雙保險，模組 docstring
    說明的鐵律：秘密檔案永遠不會被組進任何讀取指令，即使上游呼叫端不小心
    傳了未經過濾的候選列表進來，這一層仍然擋得住）。

    回傳 `{相對路徑: tail 指令}`；每條指令都是
    `tail -c {LOG_TAIL_BYTES} {quoted_full_path} 2>/dev/null || true`
    （容錯：檔案在探測與 tail 之間被刪除/搬走也不會讓整條指令失敗）。
    """
    candidates = [
        f
        for f in candidate_files
        if _is_log_candidate(f.get("path", "")) and not should_skip_secret(f.get("path", ""))
    ]
    candidates.sort(key=lambda f: f.get("mtime_epoch") or 0, reverse=True)
    top = candidates[:MAX_LOG_FILES]

    base = path.rstrip("/")
    commands: dict[str, str] = {}
    for f in top:
        rel_path = f["path"]
        full_path = f"{base}/{rel_path}"
        quoted = _quote_remote_path(full_path)
        commands[rel_path] = f"tail -c {LOG_TAIL_BYTES} {quoted} 2>/dev/null || true"
    return commands


# ---------------------------------------------------------------------------
# async 主函式：對單一 project_instance 做唯讀探測
# ---------------------------------------------------------------------------

SSHRunCallable = Callable[[str, str, float], Awaitable[Any]]


async def probe_instance(
    ssh_run: SSHRunCallable,
    server: str,
    path: str,
    exclude_names: Optional[list[str]] = None,
) -> dict:
    """對某個已在 DB 登記的 `(server, path)` 做一次唯讀探測：近期變動檔案
    列表（至多 `MAX_RECENT_FILES` 筆）＋至多 `MAX_LOG_FILES` 個 log 檔的尾段
    內容。

    回傳 `{"recent_files": [...], "log_tails": {相對路徑: 內容}}`；SSH 失敗
    （連不上、逾時等）**不拋例外**，回傳 `{"error": "..."}`——呼叫端
    （`GET /projects/{name}/activity`、`get_project_activity` 工具）可能一次
    對多個 instance 探測，單一台失敗不該讓整個請求掛掉。

    `exclude_names` 省略時用 `app.inventory.DEFAULT_EXCLUDE_NAMES`（呼叫端
    通常會傳該機器 `servers.yaml` 設定的 `project_exclude_names`）。
    """
    exclude_names = exclude_names if exclude_names is not None else DEFAULT_EXCLUDE_NAMES

    find_cmd = build_recent_files_command(path, exclude_names)
    try:
        find_result = await ssh_run(server, find_cmd, 20)
    except Exception as exc:  # noqa: BLE001 - 單一 instance 探測失敗不影響其他 instance
        return {"error": f"探測 {server}:{path} 失敗：{exc}"}

    recent_files = parse_recent_files_output(find_result.stdout or "")
    log_commands = build_log_tail_commands(path, recent_files)

    log_tails: dict[str, str] = {}
    for rel_path, tail_cmd in log_commands.items():
        try:
            tail_result = await ssh_run(server, tail_cmd, 15)
            log_tails[rel_path] = tail_result.stdout or ""
        except Exception as exc:  # noqa: BLE001
            log_tails[rel_path] = f"（讀取失敗：{exc}）"

    return {"recent_files": recent_files, "log_tails": log_tails}


# ---------------------------------------------------------------------------
# PLAN.md M.1（階段 12）：讀檔工具——列檔案／讀單一檔案，全程唯讀
# ---------------------------------------------------------------------------


def build_list_files_command(path: str, exclude_names: list[str]) -> str:
    """組出唯讀 `find` 指令：列出 `path` 底下至多 `LIST_FILES_MAX_DEPTH`
    層、至多 `MAX_LIST_FILES` 筆檔案的**相對路徑**（`-printf '%P\\n'`，同
    `build_recent_files_command()` 的相對路徑輸出慣例）。`exclude_names`
    用 `-prune` 排除（組法同 `app.inventory.build_find_command()`）。截斷
    （`head -N`）發生在遠端 shell，不是先整批傳回來再在 Python 層砍。

    **秘密檔案不在這裡過濾**（`find` 本身不方便逐 pattern 排除檔名，`-prune`
    只能排除目錄）——過濾交給 `parse_list_files_output()`，同
    `parse_recent_files_output()` 的雙層設計。
    """
    root = _quote_remote_path(path)

    prune_clause = ""
    if exclude_names:
        prune_names = " -o ".join(f"-name {_q(n)}" for n in exclude_names)
        prune_clause = f"\\( {prune_names} \\) -prune -o "

    return (
        f"find {root} {prune_clause}-maxdepth {LIST_FILES_MAX_DEPTH} -type f "
        f"-printf '%P\\n' 2>/dev/null | head -{MAX_LIST_FILES}"
    )


def parse_list_files_output(text: str) -> list[str]:
    """解析 `build_list_files_command()` 的輸出：每行一個相對路徑。秘密
    檔案（`should_skip_secret()`，比對 basename）在這裡濾掉——即使遠端
    `find` 掃到落在範圍內的秘密檔，也不會出現在回傳的檔案列表裡。"""
    results: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if should_skip_secret(line):
            continue
        results.append(line)
    return results


def build_read_file_command(path: str, rel_file: str) -> str:
    """組出唯讀 `head -c MAX_READ_FILE_BYTES` 指令，讀 `path` 底下
    `rel_file`（相對路徑）這個檔案的前 64KB 內容。

    **呼叫端必須先呼叫 `validate_rel_path(rel_file)` 確認回傳 `None`（合法）
    才能呼叫這個函式**——這裡本身不做任何驗證，只負責組指令（同
    `app.inventory.build_readme_read_command()` 只組指令、驗證在呼叫端的
    分工慣例，唯一差異是這裡的驗證函式獨立成 `validate_rel_path()` 供
    `app/approvals.py` 重用，見模組 docstring）。
    """
    base = path.rstrip("/")
    full_path = f"{base}/{rel_file}"
    quoted = _quote_remote_path(full_path)
    return f"head -c {MAX_READ_FILE_BYTES} {quoted} 2>/dev/null || true"


def validate_rel_path(rel_file: str) -> Optional[str]:
    """驗證 `rel_file` 是不是一個「安全的、instance 路徑底下的相對路徑」。

    合法回傳 `None`；不合法回傳一句人類可讀的錯誤說明（呼叫端直接用這句
    話回 400/錯誤 dict，不用另外組訊息）。三種情況一律拒絕：

    - **空字串**（或只有空白）。
    - **以 `/` 開頭**（絕對路徑）——不嘗試判斷絕對路徑是否「剛好」落在
      instance 路徑底下，一律直接拒絕（鐵律：呼叫端不用、也不該自己做
      任何路徑正規化/解析再放行）。
    - **含 `..` 片段**——**逐段檢查**（`rel_file.split("/")`，不是字串
      `"..".in(rel_file)` 的粗略 contains 比對）：只有『整段等於 `..`』
      才算，例如 `a/..b/x`（檔名剛好含兩個點但不是路徑穿越）不會被誤判。
    - **命中秘密檔名 pattern**（`app.inventory.should_skip_secret()`，比對
      basename）。

    這個函式**不嘗試解析或正規化路徑**（不用 `os.path.normpath()`）——
    呼叫端（`read_instance_file()`／`app.approvals.request_apply_patch_approval()`）
    在驗證失敗時直接回錯誤，不 SSH、不建 approval。
    """
    if not rel_file or not rel_file.strip():
        return "檔案路徑不可為空"
    if rel_file.startswith("/"):
        return "檔案路徑不可是絕對路徑"
    if any(part == ".." for part in rel_file.split("/")):
        return "檔案路徑不可包含 .. 片段"
    if should_skip_secret(rel_file):
        return "此檔案為秘密檔案，拒絕讀取"
    return None


async def list_instance_files(
    ssh_run: SSHRunCallable,
    server: str,
    path: str,
    exclude_names: Optional[list[str]] = None,
) -> dict:
    """對某個已在 DB 登記的 `(server, path)` 列出檔案（相對路徑，至多
    `MAX_LIST_FILES` 筆，秘密檔已過濾）。SSH 失敗**不拋例外**，回傳
    `{"error": "..."}`（同 `probe_instance()` 的容錯慣例）。
    """
    exclude_names = exclude_names if exclude_names is not None else DEFAULT_EXCLUDE_NAMES

    cmd = build_list_files_command(path, exclude_names)
    try:
        result = await ssh_run(server, cmd, 20)
    except Exception as exc:  # noqa: BLE001 - 單一 instance 探測失敗不影響其他呼叫
        return {"error": f"列出 {server}:{path} 的檔案失敗：{exc}"}

    return {"files": parse_list_files_output(result.stdout or "")}


async def read_instance_file(
    ssh_run: SSHRunCallable,
    server: str,
    path: str,
    rel_file: str,
) -> dict:
    """讀取某個已在 DB 登記的 `(server, path)` 底下 `rel_file` 這個檔案的
    前 `MAX_READ_FILE_BYTES` 位元組。`rel_file` 先過 `validate_rel_path()`
    ——**驗證失敗直接回錯誤 dict，完全不發送任何 SSH 指令**（鐵律：路徑
    穿越/絕對路徑/秘密檔名一律在 SSH 之前擋下）。SSH 失敗（連線問題，不是
    路徑驗證問題）**不拋例外**，回傳 `{"error": "..."}`。
    """
    err = validate_rel_path(rel_file)
    if err is not None:
        return {"error": err}

    cmd = build_read_file_command(path, rel_file)
    try:
        result = await ssh_run(server, cmd, 15)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"讀取 {server}:{path}/{rel_file} 失敗：{exc}"}

    return {"content": result.stdout or ""}


# ---------------------------------------------------------------------------
# PLAN.md M.1（階段 12）：依 project 名稱找唯一 project_instance 的共用 helper
# ---------------------------------------------------------------------------


class ProjectInstanceResolutionError(ValueError):
    """依 project／server 找不到唯一的 `project_instance`——沒有任何登記的
    instance、指定的 server 沒有登記、或省略 server 但有多個 instance
    存在（無法自動選擇）。`ValueError` 子類別，呼叫端（`app/main.py`）轉
    400。"""


def resolve_project_instance(
    db: "Database", project_name: str, server: Optional[str] = None
) -> "ProjectInstance":
    """依 `project_name`（＋選填 `server`）從 `db.list_project_instances()`
    找出唯一一筆 `ProjectInstance`。

    - 沒有任何已登記的 instance -> `ProjectInstanceResolutionError`。
    - 有帶 `server`：只接受那台機器上登記的 instance；那台機器沒有登記
      -> `ProjectInstanceResolutionError`（訊息附上目前有哪些機器可選）。
    - 沒帶 `server` 且剛好只有一個 instance -> 自動選那一個。
    - 沒帶 `server` 但有多個 instance -> `ProjectInstanceResolutionError`
      （要求呼叫端明確指定 `server`，訊息附上目前有哪些機器可選）。

    只透過 `db.list_project_instances(project_name)` 這一個既有方法取得
    候選列表，不自己組任何 SQL——「本模組只探測 DB 已登記的路徑」的鐵律
    延伸（見模組 docstring）。呼叫端仍應該自己先確認 `project_name` 對應
    的專案存在（`db.get_project()`），這裡不重複那個檢查（`project` 不存在
    與「沒有 instance」在既有 `GET /projects/{name}/activity` 的慣例裡是
    分開的兩種情況：前者 404，後者是「沒有可探測的機器」）。
    """
    instances = db.list_project_instances(project_name)
    if not instances:
        raise ProjectInstanceResolutionError(
            f"專案 {project_name} 沒有已登記的機器/路徑（project_instances 為空）"
        )

    if server:
        matched = [i for i in instances if i.server == server]
        if not matched:
            available = ", ".join(sorted({i.server for i in instances}))
            raise ProjectInstanceResolutionError(
                f"專案 {project_name} 在機器 {server} 沒有已登記的路徑"
                f"（可用機器：{available}）"
            )
        return matched[0]

    if len(instances) > 1:
        available = ", ".join(sorted({i.server for i in instances}))
        raise ProjectInstanceResolutionError(
            f"專案 {project_name} 有多個已登記的機器（{available}），請指定 server"
        )
    return instances[0]
