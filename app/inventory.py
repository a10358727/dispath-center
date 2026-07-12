"""Project Inventory：唯讀掃描器（PLAN.md I.2 節，階段 8 第一批）。

安全邊界（違反＝實作錯誤）：
- **整個模組唯讀**：只組 `find`/`cat`（限 8KB）/`git branch|rev-parse|remote`/
  `du`/`wc` 這類唯讀指令，**絕不寫遠端檔案、絕不執行使用者自訂指令**。
- **禁止路徑清單在 Python 層擋兩次**：`is_forbidden_root()` 在「建立
  inventory_scan approval 時」（`app/approvals.py`）與「核准後 `scan_server()`
  真正發指令前」（本模組）都會被呼叫——後者是這裡的責任，`scan_server()`
  對每個 `project_root` 都先檢查，不合法就跳過該 root（記警告），不是整批
  失敗，但也絕不會對禁止路徑發出任何指令。
- **檔名/目錄黑名單在組指令的 Python 層過濾，不靠遠端執行時才擋**：
  `should_skip_secret()` 命中的檔名（`.env`/`*.pem`/`*.key`/`id_rsa*`/
  `id_ed25519*`/`secrets*`/`credentials*`）根本不會被組進任何 `cat`/`head`
  指令；`DEFAULT_EXCLUDE_NAMES` 的目錄在 `find` 組指令時用 `-prune` 排除，
  不會進去那些目錄搜尋。
- **embedded dataset 只統計,不讀內容**：對命中的資料目錄只用
  `find`/`du -sb`/`wc -l`/`awk` 這類指令統計 `size_bytes`/`file_count`/
  `top_level_names`/`extension_summary`，不 `cat`、不算 hash、不 rsync、不
  自動註冊 `datasets` 表——那些是「已知的、可信任的、要跨機同步」的資料集
  才有的待遇；inventory 掃到的 embedded 資料只是「確認存在、給使用者看規模
  大小」，見 `app/approvals.py` 的 `import_project` 分支說明。
- **git remote sanitize**：`sanitize_git_remote()` 把
  `scheme://userinfo@host/...` 形式裡的 `userinfo`（可能是 token/密碼）換成
  `***`，落地到 `project_candidates`/`project_instances` 前都要經過這一關。

本模組**不碰 DB**：`scan_server()` 只回傳結構化的 `CandidateResult` 列表，
落地到 `project_candidates`（upsert）是呼叫端（`app/approvals.py` 的
`inventory_scan` 核准分支）的責任。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

#: 硬編碼禁止掃描清單（PLAN.md I.2，Fable 裁定版：兩級規則）：
#: - 純系統目錄（`/`、`/etc`、`/var`、`/root`、`/usr`、`/opt`、`/tmp`）：精確
#:   符合或其子路徑都禁止——沒有正當情境會把 `project_root` 指到這底下。
#: - `/home`／`~`：**只禁止精確符合本身**，不禁止子路徑——`/home/<user>/...`
#:   （列出所有使用者才是 `/home` 本身的問題）與 `~/...`（`~` 本身等於整個
#:   家目錄）才是這個功能唯一合理的真實使用情境。原規格在這裡寫太嚴（會擋掉
#:   規格自己舉例的 `~/projects`），Fable 已裁定修正，實際判斷邏輯見
#:   `is_forbidden_root()`。
FORBIDDEN_SCAN_ROOTS = {"/", "~", "/home", "/etc", "/var", "/root", "/usr", "/opt", "/tmp"}

#: `FORBIDDEN_SCAN_ROOTS` 裡「精確符合與子路徑都禁止」的子集合。
_FORBIDDEN_SUBTREE_ROOTS = {"/", "/etc", "/var", "/root", "/usr", "/opt", "/tmp"}

#: `FORBIDDEN_SCAN_ROOTS` 裡「只禁止精確符合本身，不禁止子路徑」的絕對路徑
#: 子集合（`/home`）。`~` 走純字串比對，不在這裡用 `os.path` 正規化——見
#: `is_forbidden_root()` 的說明。
_FORBIDDEN_EXACT_ONLY_ROOTS = {"/home"}

#: embedded dataset 偵測的預設目錄名（`project_embedded_dataset_names`，
#: servers.yaml 沒設時的預設值）。
DEFAULT_EMBEDDED_DATASET_NAMES = ["data", "dataset", "datasets", "input", "inputs"]

#: `find` 組指令時用 `-prune` 排除的目錄名預設值
#: （`project_exclude_names`，servers.yaml 沒設時的預設值）。
DEFAULT_EXCLUDE_NAMES = [
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "wandb",
    "mlruns",
    "runs",
    "outputs",
    "results",
    "checkpoints",
    "logs",
    ".cache",
    ".pytest_cache",
    ".ipynb_checkpoints",
    ".idea",
    ".vscode",
]

#: `find` 找候選專案時比對的標記檔名/目錄名。
MARKER_FILES = [
    ".git",
    "README.md",
    "README.rst",
    "pyproject.toml",
    "requirements.txt",
    "environment.yml",
    "package.json",
    "train.py",
    "scripts",
    "configs",
]

#: 檔名黑名單（`fnmatch` 比對 basename），命中就不產生任何讀取指令。
SECRET_NAME_PATTERNS = [
    ".env",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "secrets*",
    "credentials*",
]

#: README 只讀前 8KB（`head -c 8192`）。
MAX_READ_BYTES = 8192

#: `find` 掃描候選專案的預設最大深度。
MAX_SCAN_DEPTH = 3


# ---------------------------------------------------------------------------
# 純函式：安全邊界判斷
# ---------------------------------------------------------------------------


def is_forbidden_root(path: str) -> bool:
    """判斷 `path` 是否禁止掃描（PLAN.md I.2，Fable 裁定版：兩級規則）。

    - 純系統目錄（`_FORBIDDEN_SUBTREE_ROOTS`：`/`、`/etc`、`/var`、`/root`、
      `/usr`、`/opt`、`/tmp`）：絕對路徑用 `os.path.normpath` 正規化後，
      精確符合或其子路徑都禁止。
    - `/home`：只禁止精確符合本身（列出所有使用者才是問題），**不禁止
      子路徑**——`/home/<user>/...` 是合理使用情境。
    - `~`：**不用 `os.path.expanduser()`**——那是展開 Server A 本機當前
      使用者的家目錄，跟遠端工作機的路徑語意完全無關，是額外的誤判來源
      （`os.path.expanduser("~")` 在 Server A 上永遠回傳 Server A 自己的
      家目錄，不會是遠端工作機使用者的家目錄）。改用純字串比對：裸 `~`
      或 `~/`（結尾沒有更多路徑片段）禁止；`~/任何東西` 允許（唯一合理
      的真實使用情境）。

    純函式，兩個時間點都要呼叫（建立 approval 時、核准後真正掃描前，見
    模組 docstring）。
    """
    if not path or not path.strip():
        return True
    raw = path.strip()

    if raw == "~" or raw == "~/":
        return True
    if raw.startswith("~/"):
        return False

    if raw.startswith("/"):
        normalized = os.path.normpath(raw)
        if normalized in _FORBIDDEN_EXACT_ONLY_ROOTS:
            return True
        for forbidden in _FORBIDDEN_SUBTREE_ROOTS:
            if normalized == forbidden:
                return True
            if forbidden == "/":
                # "/" 本身已經在上面的相等判斷處理過；不用前綴比對整個
                # 系統，否則會把所有絕對路徑都判為禁止。
                continue
            prefix = forbidden.rstrip("/") + "/"
            if normalized.startswith(prefix):
                return True
        return False

    # 相對路徑（不是 `/` 或 `~` 開頭）：不落在任何禁止清單的絕對路徑前綴
    # 之下，視為允許（實際執行仍在遠端使用者自己的 home 目錄下）。
    return False


def should_skip_secret(filename: str) -> bool:
    """比對 `SECRET_NAME_PATTERNS`（`fnmatch`），命中就不該產生任何讀取
    這個檔案的指令。"""
    base = os.path.basename(filename.rstrip("/"))
    return any(fnmatch.fnmatch(base, pattern) for pattern in SECRET_NAME_PATTERNS)


_GIT_REMOTE_USERINFO_RE = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^@/]+)@(?P<rest>.+)$"
)


def sanitize_git_remote(url: Optional[str]) -> Optional[str]:
    """把 `scheme://userinfo@host/...` 形式裡的 `userinfo`（常常是
    token/密碼）換成 `***`；沒有 userinfo 的 URL（例如 `git@host:org/repo.git`
    這種 SSH 簡寫，或普通的 `https://host/...`）原樣返回。"""
    if not url:
        return url
    m = _GIT_REMOTE_USERINFO_RE.match(url.strip())
    if not m:
        return url
    return f"{m.group('scheme')}***@{m.group('rest')}"


# ---------------------------------------------------------------------------
# 純函式：指令組裝 / 輸出解析
# ---------------------------------------------------------------------------


def _quote_remote_path(path: str) -> str:
    """`shlex.quote()` 整段字串會讓開頭的 `~` 失去遠端 shell 的 tilde
    expansion 效果（`~` 只有出現在 unquoted 字首時才會被展開成使用者自己的
    home 目錄，單引號包住整段之後就變成字面上的波浪號，遠端會找不到叫
    `~` 的目錄）。這裡對 `~`／`~/...` 開頭的路徑只保留 `~`（或 `~/`）不加
    引號，其餘部分照常 `shlex.quote()`——bash 對「word 開頭是 unquoted `~`、
    後面接 quoted 片段」這種混合寫法一樣會正確展開（例如 `~/'my project'`
    等同 `~/my project` 展開後的結果）。純絕對路徑（不是 `~` 開頭）行為
    不變，照舊整段 `shlex.quote()`。"""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        rest = path[2:]
        return "~/" + shlex.quote(rest) if rest else "~/"
    return shlex.quote(path)


def build_find_command(
    project_root: str, exclude_names: list[str], max_depth: int = MAX_SCAN_DEPTH
) -> str:
    """組出 find 指令：找 `MARKER_FILES` 裡出現的項目（存在性），
    `exclude_names` 用 `-prune` 排除（不會進去那些目錄）。輸出每行一個
    完整路徑（標記檔案/目錄本身），交給 `parse_find_output()` 解析。
    `max_depth` 是「候選專案目錄」相對 `project_root` 的深度；標記本身
    可能比專案目錄深一層（例如 `proj/.git`），所以實際 `find -maxdepth`
    用 `max_depth + 1`。

    `project_root` 若以 `~` 開頭，遠端 shell 會先展開成使用者自己的 home
    目錄再交給 `find` 執行，因此 `find` 輸出的候選路徑一律是展開後的絕對
    路徑——後續組 README/git/embedded dataset 指令時直接用這些候選路徑，
    不需要再處理 `~`。
    """
    root = _quote_remote_path(project_root)
    marker_clause = " -o ".join(f"-name {shlex.quote(m)}" for m in MARKER_FILES)
    depth = max(1, max_depth) + 1

    prune_clause = ""
    if exclude_names:
        prune_names = " -o ".join(f"-name {shlex.quote(n)}" for n in exclude_names)
        prune_clause = f"\\( {prune_names} \\) -prune -o "

    return f"find {root} {prune_clause}-maxdepth {depth} \\( {marker_clause} \\) -print 2>/dev/null"


def parse_find_output(text: str) -> list[dict]:
    """解析 `build_find_command()` 的輸出：同一個路徑（標記的所在目錄）出現
    多個標記時要合併成一筆，`markers` 排序後的列表。"""
    grouped: dict[str, set] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        stripped = line.rstrip("/")
        marker = os.path.basename(stripped)
        path = os.path.dirname(stripped)
        if not path:
            continue
        grouped.setdefault(path, set()).add(marker)
    return [{"path": p, "markers": sorted(m)} for p, m in sorted(grouped.items())]


def _candidate_path(item: Any) -> str:
    """相容兩種候選資料形狀：帶 `path` 屬性的物件（例如
    `CandidateResult`）與帶 `path` 鍵的 dict（例如 `parse_find_output()` 的
    產出）。`prune_nested_candidates()` 唯一需要知道的就是每個候選的路徑
    字串，不需要理解其餘欄位。"""
    if isinstance(item, dict):
        return item["path"]
    return item.path


def prune_nested_candidates(candidates: list) -> list:
    """巢狀候選剔除（PLAN.md P.1.2）：掃描常常在同一個真實專案底下的
    `data`/`scripts`/`.pytest_cache` 等子目錄也各自命中標記檔，變成一堆
    彼此巢狀的候選噪音（例如 FPAD 底下的 data/scripts 各算一個候選）。這裡
    把候選按路徑排序後，凡是路徑位於**另一個候選路徑之下**的都剔除，只留
    頂層專案。

    取捨（Fable 定案）：子目錄如果其實是一個真正獨立的專案（不只是噪音），
    這批不嘗試分辨——先讓頂層候選勝出、使用者匯入頂層之後，子目錄有需要
    再另行處理（手動 inventory scan 指定該子目錄為 project_root，或後續
    批次再解決）。這裡的目標單純是把「一個專案底下巢狀出好幾個候選」的
    噪音收斂成一筆，不是做語意分析。

    判定用 `path.startswith(other + "/")`（先各自 `rstrip("/")` 正規化），
    刻意不用裸字串前綴比對——`"/a/bc".startswith("/a/b")` 會誤判 `/a/bc`
    是 `/a/b` 的子目錄，兩者其實是同層的不同專案，必須先確認前綴後面接的
    是路徑分隔符號才算「在其之下」。

    輸入項目可以是物件（帶 `path` 屬性，例如 `CandidateResult`）或 dict
    （帶 `path` 鍵），見 `_candidate_path()`。回傳保留下來的候選（原始
    物件，不是路徑字串），維持排序後的順序。
    """
    sorted_candidates = sorted(candidates, key=_candidate_path)
    kept: list = []
    kept_paths: list[str] = []
    for cand in sorted_candidates:
        normalized = _candidate_path(cand).rstrip("/")
        is_nested = any(
            normalized == kp or normalized.startswith(kp + "/") for kp in kept_paths
        )
        if is_nested:
            continue
        kept.append(cand)
        kept_paths.append(normalized)
    return kept


def build_readme_read_command(path: str) -> Optional[str]:
    """只讀 README 前 `MAX_READ_BYTES`（`head -c 8192`）。`path` 是 README
    檔案本身的完整路徑；命中秘密黑名單（理論上 README 檔名不會，但保留這道
    防線）就回傳 `None`，呼叫端不該送出任何指令。"""
    if should_skip_secret(path):
        return None
    return f"head -c {MAX_READ_BYTES} {shlex.quote(path)} 2>/dev/null || true"


def build_git_info_commands(path: str) -> dict[str, str]:
    """branch/commit/remote 三條唯讀指令，每條都加 `2>/dev/null || true`
    容錯（不是 git repo、無 remote 都不會讓整個掃描失敗）。"""
    p = shlex.quote(path)
    return {
        "branch": f"git -C {p} branch --show-current 2>/dev/null || true",
        "commit": f"git -C {p} rev-parse HEAD 2>/dev/null || true",
        "remote": f"git -C {p} remote get-url origin 2>/dev/null || true",
    }


def build_embedded_dataset_scan_command(
    project_path: str,
    embedded_names: list[str],
    exclude_names: Optional[list[str]] = None,
) -> str:
    """對專案目錄下命中 `embedded_names`（如 data/dataset/datasets/input/
    inputs）的**直接子目錄**只統計 `size_bytes`（`du -sb`）、`file_count`
    （`find | wc -l`）、`top_level_names`（`find -maxdepth 1`）、
    `extension_summary`（副檔名計數），**不讀取任何檔案內容**（沒有
    `cat`/`head` 對資料檔案本身）。輸出用 `PATH:`/`FILES:`/`BYTES:`/
    `TOP:`/`EXT:`/`---` 的固定格式，交給 `parse_embedded_dataset_output()`
    解析。`exclude_names` 目前保留參數位（第一批先不深入巢狀 exclude，
    embedded 目錄本身用途單純，統計時不特別排除子目錄）。
    """
    root = shlex.quote(project_path)
    name_clause = " -o ".join(f"-name {shlex.quote(n)}" for n in embedded_names)
    find_dirs = (
        f"find {root} -mindepth 1 -maxdepth 1 -type d \\( {name_clause} \\) 2>/dev/null"
    )
    loop_body = (
        'echo "PATH:$d"; '
        'echo "FILES:$(find "$d" -type f 2>/dev/null | wc -l)"; '
        'echo "BYTES:$(du -sb "$d" 2>/dev/null | cut -f1)"; '
        "echo \"TOP:$(find \"$d\" -mindepth 1 -maxdepth 1 -printf '%f,' 2>/dev/null)\"; "
        "echo \"EXT:$(find \"$d\" -type f -name '*.*' 2>/dev/null | awk -F. '{print $NF}' "
        "| sort | uniq -c | awk '{print $2\":\"$1}' | tr '\\n' ',')\"; "
        'echo "---";'
    )
    return f"for d in $({find_dirs}); do {loop_body} done"


def parse_embedded_dataset_output(text: str) -> list[dict]:
    """解析 `build_embedded_dataset_scan_command()` 的輸出，回傳
    `[{"path", "file_count", "size_bytes", "top_level_names", "extension_summary"}, ...]`。
    """
    results: list[dict] = []
    current: dict = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "---":
            if current:
                results.append(current)
            current = {}
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key == "PATH":
            current["path"] = value
        elif key == "FILES":
            try:
                current["file_count"] = int(value.strip() or 0)
            except ValueError:
                current["file_count"] = 0
        elif key == "BYTES":
            try:
                current["size_bytes"] = int(value.strip() or 0)
            except ValueError:
                current["size_bytes"] = 0
        elif key == "TOP":
            current["top_level_names"] = [v for v in value.split(",") if v]
        elif key == "EXT":
            summary: dict[str, int] = {}
            for pair in value.split(","):
                if not pair or ":" not in pair:
                    continue
                ext, _, cnt = pair.partition(":")
                try:
                    summary[ext] = int(cnt)
                except ValueError:
                    continue
            current["extension_summary"] = summary
    if current:
        results.append(current)
    return results


# ---------------------------------------------------------------------------
# 純函式：confidence / command 猜測（不用太講究，合理即可）
# ---------------------------------------------------------------------------

_MARKER_CONFIDENCE_WEIGHTS = {
    ".git": 0.4,
    "README.md": 0.3,
    "README.rst": 0.3,
    "pyproject.toml": 0.2,
    "requirements.txt": 0.2,
    "environment.yml": 0.2,
    "package.json": 0.2,
    "train.py": 0.2,
    "scripts": 0.1,
    "configs": 0.1,
}


def estimate_confidence(markers: list[str]) -> float:
    """簡單規則：命中越多、越強的標記，confidence 越高。例如
    `.git`+`README.md`+`requirements.txt` 三個都有 → 約 0.9；只有一個較弱的
    標記 → 約 0.3。夾在 `[0.1, 0.95]`。"""
    if not markers:
        return 0.1
    total = sum(_MARKER_CONFIDENCE_WEIGHTS.get(m, 0.05) for m in set(markers))
    return round(min(max(total, 0.1), 0.95), 2)


def guess_command(markers: list[str], path: str) -> Optional[str]:
    """簡單規則：有 `train.py` → `python train.py`；其餘（例如只有
    `package.json`，需要讀檔內容才能猜 npm script，這批掃描不讀取檔案內容）
    → `None`，留給使用者在匯入時填寫。"""
    m = set(markers)
    if "train.py" in m:
        return "python train.py"
    return None


# ---------------------------------------------------------------------------
# CandidateResult ＋ scan_server 主流程
# ---------------------------------------------------------------------------


@dataclass
class CandidateResult:
    """對應 `project_candidates` 的欄位（不含 db 專屬的 id/created_at/
    updated_at，那些留給呼叫端組，見 `app/db.py` 的
    `upsert_project_candidate()`）。"""

    server: str
    path: str
    name_guess: Optional[str]
    kind: str
    git_remote: Optional[str]
    git_branch: Optional[str]
    git_commit: Optional[str]
    markers: list[str] = field(default_factory=list)
    readme_excerpt: Optional[str] = None
    command_guess: Optional[str] = None
    embedded_data_paths: list[str] = field(default_factory=list)
    embedded_data_summary: Optional[str] = None
    estimated_data_bytes: Optional[int] = None
    excluded_paths: list[str] = field(default_factory=list)
    confidence: float = 0.1


SSHRunCallable = Callable[[str, str, float], Awaitable[Any]]


async def scan_server(
    ssh_run: SSHRunCallable,
    server_name: str,
    project_roots: list[str],
    *,
    embedded_dataset_names: Optional[list[str]] = None,
    exclude_names: Optional[list[str]] = None,
    max_depth: int = MAX_SCAN_DEPTH,
) -> list[CandidateResult]:
    """主流程：對每個 `project_root` 先 `is_forbidden_root()` 檢查（不合法
    直接跳過該 root、記警告，不整體失敗）→ 跑 find → parse → 對每個候選跑
    README/git info/embedded dataset 指令 → 組出 `CandidateResult` 列表。

    `ssh_run` 是 async callable，介面同 `app.sshpool.SSHPool.run()`：
    `await ssh_run(server_name, command, timeout) -> CommandResult`（有
    `.stdout` 屬性即可），測試可以注入假的，不需要真 SSH。單一候選的某個
    子步驟（README/git/embedded dataset）失敗不影響其他候選，只記警告、該
    欄位留空。
    """
    embedded_dataset_names = embedded_dataset_names or DEFAULT_EMBEDDED_DATASET_NAMES
    exclude_names = exclude_names or DEFAULT_EXCLUDE_NAMES

    results: list[CandidateResult] = []

    for root in project_roots:
        if is_forbidden_root(root):
            logger.warning(
                "跳過禁止掃描的 project_root：%s（server=%s）", root, server_name
            )
            continue

        find_cmd = build_find_command(root, exclude_names, max_depth=max_depth)
        try:
            find_res = await ssh_run(server_name, find_cmd, 30)
        except Exception as exc:  # noqa: BLE001 - 單一 root 探測失敗不影響其他 root
            logger.warning("掃描 %s:%s 的 find 失敗：%s", server_name, root, exc)
            continue

        candidates = parse_find_output(find_res.stdout or "")

        for cand in candidates:
            path = cand["path"]
            markers = cand["markers"]

            readme_excerpt: Optional[str] = None
            for readme_name in ("README.md", "README.rst"):
                if readme_name not in markers:
                    continue
                readme_path = f"{path.rstrip('/')}/{readme_name}"
                cmd = build_readme_read_command(readme_path)
                if cmd is None:
                    continue
                try:
                    readme_res = await ssh_run(server_name, cmd, 15)
                    readme_excerpt = (readme_res.stdout or "")[:MAX_READ_BYTES]
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "讀取 %s:%s 的 %s 失敗：%s", server_name, path, readme_name, exc
                    )
                break

            git_remote: Optional[str] = None
            git_branch: Optional[str] = None
            git_commit: Optional[str] = None
            if ".git" in markers:
                git_cmds = build_git_info_commands(path)
                try:
                    branch_res = await ssh_run(server_name, git_cmds["branch"], 10)
                    git_branch = (branch_res.stdout or "").strip() or None
                except Exception as exc:  # noqa: BLE001
                    logger.warning("讀取 %s:%s 的 git branch 失敗：%s", server_name, path, exc)
                try:
                    commit_res = await ssh_run(server_name, git_cmds["commit"], 10)
                    git_commit = (commit_res.stdout or "").strip() or None
                except Exception as exc:  # noqa: BLE001
                    logger.warning("讀取 %s:%s 的 git commit 失敗：%s", server_name, path, exc)
                try:
                    remote_res = await ssh_run(server_name, git_cmds["remote"], 10)
                    raw_remote = (remote_res.stdout or "").strip() or None
                    git_remote = sanitize_git_remote(raw_remote)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("讀取 %s:%s 的 git remote 失敗：%s", server_name, path, exc)

            embedded_data_paths: list[str] = []
            embedded_data_summary: Optional[str] = None
            estimated_data_bytes: Optional[int] = None
            embed_cmd = build_embedded_dataset_scan_command(
                path, embedded_dataset_names, exclude_names
            )
            try:
                embed_res = await ssh_run(server_name, embed_cmd, 30)
                parsed = parse_embedded_dataset_output(embed_res.stdout or "")
                if parsed:
                    embedded_data_paths = [p["path"] for p in parsed]
                    estimated_data_bytes = sum(p.get("size_bytes") or 0 for p in parsed)
                    embedded_data_summary = json.dumps(parsed, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "掃描 %s:%s 的 embedded dataset 失敗：%s", server_name, path, exc
                )

            name_guess = os.path.basename(path.rstrip("/")) or path

            results.append(
                CandidateResult(
                    server=server_name,
                    path=path,
                    name_guess=name_guess,
                    kind="project",
                    git_remote=git_remote,
                    git_branch=git_branch,
                    git_commit=git_commit,
                    markers=markers,
                    readme_excerpt=readme_excerpt,
                    command_guess=guess_command(markers, path),
                    embedded_data_paths=embedded_data_paths,
                    embedded_data_summary=embedded_data_summary,
                    estimated_data_bytes=estimated_data_bytes,
                    excluded_paths=list(exclude_names),
                    confidence=estimate_confidence(markers),
                )
            )

    return results
