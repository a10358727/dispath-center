"""中央 hub 同步（PLAN.md P.2 節背景／P.2.2 定案）：**Server A bare repo**
（`~/git/{project}.git`）——不依賴外網、LAN 速度、私有專案不出門；GitHub
之後可另加為備份 remote（選配，P.4，本階段不實作）。

傳輸方向現實：只有 Server A 有全部工作機的 SSH 權限、workers 互不相通也
連不回 Server A，因此跨機傳遞一律走 bundle、Server A 中轉（複用 N.7 已建好
的機制：`git bundle create --all` → rsync 拉回 → 本地 `git init --bare` →
`git --git-dir=<repo> bundle verify` → `git fetch`。**`bundle verify`
必須帶 `--git-dir`**——沒有 repo context 會直接失敗（「error: need a
repository to verify a bundle」），這是規格原文沒寫、實跑才發現的前提，
見 `sync_project_to_hub()` docstring 第 4 步）。

`sync_project_to_hub()` 是**直接執行的 web 動作＋稽核，不出核准卡**——對
worker 只建 bundle 暫存檔（無害，不影響 instance 的 working tree／branch）、
對 Server A 是本地 bare repo fetch（冪等，重覆同步同一個 commit 不會有
副作用），風險等級跟既有 `GET /projects/{name}/activity`／
`POST /coding-runs/{id}/cleanup` 這類「唯讀或低風險直接執行」端點相同（見
`app/approvals.py` 的 `cleanup_coding_run()` 模式），不需要人工核准。

`get_project_hub_info()` 供 `GET /projects/matrix`（PLAN.md P.2.3）附加
`hub` 欄——純本地檔案系統/git 查詢，不對任何工作機發起 SSH，維持矩陣端點
「唯讀、不即時 SSH」的既有原則。

**階段 15 Phase C（PLAN.md P.3 節）**：`request_project_deploy_approval()`
建立 kind=`project_deploy` 的核准請求——把中央 hub 目前的某個 ref 部署成
另一台機器上一個全新的 `project_instance`（**來源固定是 Server A hub**，
不支援「從任意 instance commit 直接部署」——要部署先 `hub-sync`）。跟
`sync_project_to_hub()` 不同，這是**要走核准流**的動作（會在目標機新建
一個目錄／git repo），真正執行發生在 `app.approvals.approve()` 的
`project_deploy` 分支（比照 `git_init` 分支「inline 執行、全部在
approve() 當下完成」的模式，不像 `coding_task` 那樣派一個背景 job）。這裡
只放「建立請求時的驗證＋純函式」，跟 `app.hub` 模組其餘函式風格一致；
`approve()` 分支重用本模組的 `hub_repo_path()`／`build_deploy_push_command()`
／`local_deploy_bundle_path()`。
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.activity import ProjectInstanceResolutionError, resolve_project_instance
from app.audit import append_audit, audit_actor_from_request_context
from app.config import AppConfig
from app.datasets import build_ssh_opts
from app.db import Approval, Database
from app.inventory import is_forbidden_root
from app.identity import RequestContext


class HubSyncError(ValueError):
    """`sync_project_to_hub()` 的驗證/執行失敗（指定的 instance 不是 git
    repo，或本地/遠端指令執行失敗）——呼叫端（`app/main.py`）轉 400。

    **不包含**「instance 不存在」——那個情況讓
    `app.activity.ProjectInstanceResolutionError` 直接往上傳，呼叫端轉
    404（PLAN.md P.2.2 明確要求兩者對應不同的 HTTP 狀態碼：instance 不存在
    404、不是 git repo 400）。
    """


class InvalidProjectDeployRequestError(ValueError):
    """`request_project_deploy_approval()` 的驗證失敗（PLAN.md P.3）——呼叫端
    （`app/main.py`）轉 400，不建立 approval。**不包含**「呼叫端沒有提供
    `ssh_run`／`local_run`」這種呼叫端自身的組裝錯誤——那兩種情況丟一般
    `ValueError`（同 `request_git_init_approval()`／`add_manual_candidate()`
    的既有慣例，理論上不會在正常呼叫路徑發生，防禦性處理）。"""


def hub_repo_path(project: str, local_home_dir: str) -> str:
    """專案在 Server A 本地的 hub bare repo 路徑：`{home}/git/{project}.git`
    （相對 `local_home_dir` 解析，路徑慣例同 `app.results.local_result_dir()`
    ）。"""
    base = (local_home_dir or ".").rstrip("/") or "."
    return f"{base}/git/{project}.git"


def local_hub_bundle_dir(project: str, local_home_dir: str) -> str:
    """從工作機拉回的 bundle 在 Server A 本地的暫存目錄：
    `{home}/hub_bundles/{project}/`（路徑慣例同
    `app.results.local_result_dir()`）。"""
    base = (local_home_dir or ".").rstrip("/") or "."
    return f"{base}/hub_bundles/{project}/"


def build_hub_pull_command(project: str, server_cfg, local_home_dir: str) -> str:
    """組出「從工作機拉 `hub_bundles/{project}.bundle`（home 相對）回本地
    `hub_bundles/{project}/`」的 rsync 指令：先在本地 `mkdir -p` 目的地
    目錄，成功後才 rsync 拉過來，模仿
    `app.results.build_result_pull_command()`。`server_cfg` 是
    `app.config.ServerConfig`，port 統一由 `build_ssh_opts()` 處理（非 22
    時附加 `-p {port}`，2026-07-10 pro6000 32221 埠修復把入口收斂到這裡）。
    """
    dest = local_hub_bundle_dir(project, local_home_dir)
    ssh_opts = build_ssh_opts(server_cfg.key_path, server_cfg.port)
    remote_src = f"{server_cfg.user}@{server_cfg.host}:hub_bundles/{project}.bundle"
    return (
        f"mkdir -p {shlex.quote(dest)} && "
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(remote_src)} {shlex.quote(dest)}"
    )


async def sync_project_to_hub(
    db: Database,
    project: str,
    server: str,
    *,
    ssh_run,
    local_run,
    config: AppConfig,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> dict:
    """`POST /projects/{name}/hub-sync`（PLAN.md P.2.2）：把某個 project
    instance 目前的 git 狀態同步進 Server A 的中央 hub bare repo。**直接
    執行，不走核准流**（見模組 docstring 的風險說明）。

    - instance 不存在 -> `app.activity.ProjectInstanceResolutionError`
      往上傳，呼叫端轉 404。
    - instance 不是 git repo（SSH 唯讀確認 `test -d {path}/.git`）->
      `HubSyncError`，呼叫端轉 400——不會嘗試建立 bundle。
    - `server` 沒有對應的 `config.servers` 設定 -> `HubSyncError`（理論上
      不會發生，`resolve_project_instance()` 找到的 instance.server 一定
      是曾經登記過的機器名，防禦性處理）。

    五步（PLAN.md P.2.2 原文）：
    1. SSH 確認 instance 是 git repo（上述）。
    2. worker `git bundle create $HOME/hub_bundles/{project}.bundle --all`
       （home 相對）。
    3. `build_hub_pull_command()` 組的 rsync，用 `local_run` 在 Server A
       本地執行，把 bundle 拉回 `hub_bundles/{project}/`。
    4. Server A 本地（都用 `local_run`）：`git init --bare
       ~/git/{project}.git`（冪等——已存在的 bare repo 不會被破壞，
       `git init` 對已存在的 repo 是安全的 no-op 式重新初始化）→
       `git --git-dir={repo} bundle verify <bundle>`（**必須帶
       `--git-dir`**——`git bundle verify` 需要在一個 repo context 底下
       才能核對 bundle 的前置 commit 是否可解析，沒帶會直接失敗回
       「error: need a repository to verify a bundle」，PLAN.md P.2.2
       原文沒寫這個前提，實跑才發現，這裡用剛 `init --bare` 保證存在的
       hub repo 當 context）→ `git --git-dir ... fetch <bundle>
       '+refs/heads/*:refs/heads/*'`。
    5. 稽核 `hub_sync` {project, server, head}；`head` 是 hub bare repo
       目前 `HEAD` 的短 commit（拿不到就是 `None`，不影響同步本身視為
       成功——bare repo 的 `HEAD` 是否能解析取決於預設分支名稱是否剛好
       跟 worker 的一致，這個邊界情況不應該讓整次同步失敗）。

    任何一步本地/遠端指令失敗（非 0 exit status）-> `HubSyncError`（附
    stderr 尾段），不繼續往下走、不寫 `hub_sync` 稽核（同既有 `pull_job_
    results()` 的截斷慣例，避免把整包很長的錯誤輸出塞進例外訊息）。
    """
    instance = resolve_project_instance(db, project, server)

    server_cfg = next((s for s in config.servers if s.name == server), None)
    if server_cfg is None:
        raise HubSyncError(f"未知的機器設定：{server}")

    remote_path = shlex.quote(instance.path)

    check_res = await ssh_run(server, f"test -d {remote_path}/.git && echo GIT_OK", 10)
    if "GIT_OK" not in (check_res.stdout or ""):
        raise HubSyncError(f"專案 {project} 在機器 {server} 不是 git repo，請先執行 git 化")

    await ssh_run(server, "mkdir -p hub_bundles", 10)
    await ssh_run(
        server,
        f"git -C {remote_path} bundle create $HOME/hub_bundles/{project}.bundle --all",
        120,
    )

    def _stderr_tail(result) -> str:
        text = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
        return text[-500:] or f"exit_status={getattr(result, 'exit_status', '?')}"

    pull_cmd = build_hub_pull_command(project, server_cfg, config.local_home_dir)
    pull_result = await local_run(pull_cmd, config.result_pull_timeout_sec)
    if pull_result.exit_status != 0:
        raise HubSyncError(f"拉取 bundle 失敗：{_stderr_tail(pull_result)}")

    bundle_local_path = f"{local_hub_bundle_dir(project, config.local_home_dir)}{project}.bundle"
    repo_path = hub_repo_path(project, config.local_home_dir)

    init_result = await local_run(f"git init --bare {shlex.quote(repo_path)}", 30)
    if init_result.exit_status != 0:
        raise HubSyncError(f"git init --bare 失敗：{_stderr_tail(init_result)}")

    # `git bundle verify` 需要在一個 repo context 底下跑，不然真實環境會回
    # 「error: need a repository to verify a bundle」（規格原文沒寫這個
    # 前提，實跑才發現）——這裡用 `--git-dir={repo_path}` 帶入剛剛
    # `git init --bare` 保證存在的 hub repo 當 context，不是隨便找一個
    # repo（bundle verify 只是核對 bundle 內容自身的完整性/前置 commit
    # 是否可解析，不會真的改動這個 repo）。
    verify_result = await local_run(
        f"git --git-dir={shlex.quote(repo_path)} bundle verify {shlex.quote(bundle_local_path)}",
        30,
    )
    if verify_result.exit_status != 0:
        raise HubSyncError(f"git bundle verify 失敗：{_stderr_tail(verify_result)}")

    fetch_cmd = (
        f"git --git-dir {shlex.quote(repo_path)} fetch {shlex.quote(bundle_local_path)} "
        "'+refs/heads/*:refs/heads/*'"
    )
    fetch_result = await local_run(fetch_cmd, 30)
    if fetch_result.exit_status != 0:
        raise HubSyncError(f"git fetch 失敗：{_stderr_tail(fetch_result)}")

    head_result = await local_run(
        f"git --git-dir {shlex.quote(repo_path)} rev-parse --short HEAD", 15
    )
    head = (head_result.stdout or "").strip() if head_result.exit_status == 0 else None
    head = head or None

    #: PLAN.md 2026-07-11 版 §14 切片 4(canonical version service)：hub
    #: 同步成功後把**完整** commit（`--short` 只夠顯示,不夠當跨表外鍵/
    #: 精確比對用）登記成一筆不可變 ProjectVersion,`source_instance_id`
    #: 記錄是哪個 instance 的 git 狀態同步上來的。`full_head` 拿不到
    #: （理論上不會,hub 剛 fetch 成功一定有 HEAD;防禦性處理)就不建立,
    #: 不讓 hub_sync 本身的成敗被這個附加動作拖累。
    full_head_result = await local_run(
        f"git --git-dir {shlex.quote(repo_path)} rev-parse HEAD", 15
    )
    full_head = (
        (full_head_result.stdout or "").strip() if full_head_result.exit_status == 0 else None
    ) or None
    version_id = None
    if full_head:
        version = db.get_or_create_project_version(
            project,
            full_head,
            git_ref=instance.git_branch,
            source_instance_id=instance.id,
        )
        version_id = version.id

    append_audit(
        "hub_sync",
        {"project": project, "server": server, "head": head, "version_id": version_id},
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return {"project": project, "server": server, "head": head, "version_id": version_id}


async def get_project_hub_info(project: str, local_home_dir: str, *, local_run) -> dict:
    """`GET /projects/matrix`（PLAN.md P.2.3）的 `hub` 欄：`{exists, head,
    last_sync}`。純本地檔案系統/git 查詢，**不對任何工作機發起 SSH**，維持
    矩陣端點「唯讀、不即時 SSH」的既有原則；**不暴露絕對路徑**（回傳值
    只有這三個欄位，`hub_repo_path()` 組出的路徑只在函式內部使用）。

    - `exists`：本地 `{local_home_dir}/git/{project}.git` 目錄是否存在。
    - `head`：`git --git-dir ... rev-parse --short HEAD`（用 `local_run`
      執行；不存在或指令失敗一律回 `None`，不拋例外——矩陣是唯讀彙總
      端點，任何一個專案的 hub 探測失敗不該讓整個請求掛掉）。
    - `last_sync`：hub bare repo 目錄本身的 mtime（ISO 字串）——簡單可靠的
      「最後同步時間」代理指標：`sync_project_to_hub()` 每次 `git fetch`
      成功都會更新這個目錄底下的內容，進而更新目錄本身的 mtime。
    """
    repo_path = hub_repo_path(project, local_home_dir)
    repo_dir = Path(repo_path)
    if not repo_dir.is_dir():
        return {"exists": False, "head": None, "last_sync": None}

    head: Optional[str] = None
    try:
        result = await local_run(
            f"git --git-dir {shlex.quote(repo_path)} rev-parse --short HEAD", 5
        )
        if getattr(result, "exit_status", None) == 0:
            head = (result.stdout or "").strip() or None
    except Exception:  # noqa: BLE001 - 矩陣端點的 hub 探測失敗不應該讓整個請求掛掉
        head = None

    last_sync: Optional[str] = None
    try:
        mtime = repo_dir.stat().st_mtime
        last_sync = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except OSError:
        last_sync = None

    return {"exists": True, "head": head, "last_sync": last_sync}


# ---------------------------------------------------------------------------
# 階段 15 Phase C（PLAN.md P.3 節）：kind=project_deploy 核准請求——把中央
# hub 目前的某個 ref 部署成另一台機器上一個全新的 project_instance。
# ---------------------------------------------------------------------------

#: `ref` 若提供，必須符合這個白名單——它會被嵌進 `git bundle create`／
#: `git clone -b` 等指令。跟 `app.approvals._BASE_BRANCH_RE` 完全相同的
#: 字元集（PLAN.md P.3 原文：「字元集同 base_branch 白名單」），這裡獨立
#: 定義一份是為了不讓 `app.hub` 反過來 import `app.approvals`（`app.
#: approvals` 已經 import `app.hub`，避免循環 import）。
_DEPLOY_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def local_deploy_bundle_path(approval_id: int, local_home_dir: str) -> str:
    """某筆 `project_deploy` approval 在 Server A 本地暫存的 bundle 路徑：
    `{home}/hub_bundles/deploy_{approval_id}.bundle`（路徑慣例同
    `hub_repo_path()`／`local_hub_bundle_dir()`；用 approval id 定址，不用
    project 名稱——同一個專案可以有多筆不同目標機的部署請求，各自獨立的
    bundle 檔不會互相覆蓋）。"""
    base = (local_home_dir or ".").rstrip("/") or "."
    return f"{base}/hub_bundles/deploy_{approval_id}.bundle"


def build_deploy_push_command(
    approval_id: int, project: str, target_cfg, local_home_dir: str
) -> str:
    """組出「把某筆 `project_deploy` approval 的本地 bundle
    （`local_deploy_bundle_path()`）推到目標機 `deploy_bundles/{approval_id}
    .bundle`（home 相對）」的 rsync 指令：先在目標機 `mkdir -p
    deploy_bundles`，成功後才 rsync 推過去，慣例模仿
    `app.results.build_bundle_push_command()`（同樣是「本地 bundle 推到
    工作機」的方向）。

    - `project`：目前只用於函式簽章跟呼叫端（`approve()` 的 project_deploy
      分支）語意對齊，實際指令組裝不需要（bundle 檔名用 `approval_id`
      定址，見 `local_deploy_bundle_path()`），保留參數是為了跟
      `build_bundle_push_command(run_job_id, coding_run_id, ...)` 同一種
      「定址 id 顯式帶入」的慣例，方便日後若改成含專案名的路徑規則時
      不必更動呼叫端。
    - `target_cfg.port`：port 統一由 `build_ssh_opts()` 處理，不在這裡另外
      組裝（2026-07-10 pro6000 32221 埠修復把入口收斂到 `build_ssh_opts()`
      的既有慣例）。
    """
    del project  # 見上方 docstring：目前只用於語意對齊，不影響指令內容。
    src = local_deploy_bundle_path(approval_id, local_home_dir)
    ssh_opts = build_ssh_opts(target_cfg.key_path, target_cfg.port)
    remote = f"{target_cfg.user}@{target_cfg.host}"
    remote_mkdir = f"{ssh_opts} {shlex.quote(remote)} {shlex.quote('mkdir -p deploy_bundles')}"
    rsync_cmd = (
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(src)} "
        f"{shlex.quote(f'{remote}:deploy_bundles/{approval_id}.bundle')}"
    )
    return f"{remote_mkdir} && {rsync_cmd}"


async def request_project_deploy_approval(
    db: Database,
    project: str,
    target_server: str,
    dest_path: Optional[str] = None,
    ref: Optional[str] = None,
    *,
    config: AppConfig,
    server_configs: dict,
    ssh_run,
    local_run,
    audit_path: str = "audit.jsonl",
    request_context: Optional[RequestContext] = None,
) -> Approval:
    """建立 kind=`project_deploy` 的核准請求，不真的動任何檔案（PLAN.md
    P.3）：真正的 bundle 建立／rsync 推送／目標機 clone 發生在
    `POST /approve/{id}`（見 `app.approvals.approve()` 的 `project_deploy`
    分支）。**來源固定是 Server A 中央 hub**——不支援「從任意 instance
    commit 直接部署」，要部署先對該專案執行 `hub-sync`。

    建立請求當下（不通過就對應例外，呼叫端轉 400，不建 approval，鐵律第
    2 條的延伸）驗證順序：

    1. `target_server` 必須存在於 `server_configs` 且 `enabled=True`
       （同 `add_manual_candidate()` 的既有慣例）。
    2. 該專案在 `target_server` **尚無** `project_instance`——已有 →
       「已存在 instance；更新既有 instance 屬後續功能」（PLAN.md P.3
       明確裁定本階段只做全新部署，不做既有 instance 更新）。
    3. hub 存在：本地 `{local_home_dir}/git/{project}.git` 目錄要存在
       （`hub_repo_path()` ＋ `Path.is_dir()`，純本地檔案系統檢查，不
       SSH）——不存在 →「請先對該專案執行 hub 同步」。
    4. `ref`：
       - 有提供 → 先過 `_DEPLOY_REF_RE`（同 `app.approvals._BASE_BRANCH_RE`
         字元集）；再用 `local_run` 跑 `git --git-dir={hub}
         rev-parse --verify refs/heads/{ref}` 確認可解析，解不出 → 拒絕。
       - 沒提供 → 用 hub 目前 HEAD 的 branch 名當預設：先試
         `git --git-dir={hub} symbolic-ref --short HEAD`，解不出（例如
         hub 剛 `init --bare` 完、還沒 fetch 過任何東西）再退而求其次試
         `git --git-dir={hub} rev-parse --abbrev-ref HEAD`；兩者都解不出
         （或解出來是字面上的 `"HEAD"`，代表 detached）→「無法解析
         hub 的預設分支，請先執行 hub 同步」。
    5. `dest_path`：
       - 沒提供 → 用 `{target_server 的 project_roots[0]}/{project}`
         當預設；`target_server` 沒有設定 `project_roots` → 必填，拒絕
         「請明確提供 dest_path」。
       - 必須是絕對路徑（`/` 開頭）、不得含 `..` 片段、正規化後（去掉
         結尾 `/`）不得命中 `app.inventory.is_forbidden_root()`（跟
         `add_manual_candidate()` 的路徑驗證慣例一致）。
       - **SSH 唯讀確認不存在或為空目錄**：`test -e {dest} || echo
         NOT_EXIST`——輸出含 `NOT_EXIST` 就放行（目錄還不存在，`approve()`
         的 `git clone` 會自己建）；否則再跑 `ls -A {dest}`，輸出為空（且
         指令本身成功）才放行（空目錄可以被 `git clone` 進去），非空或
         `ls` 本身失敗（例如 `dest` 其實是個檔案）→ 拒絕「目標路徑已存在
         且非空目錄」。

    payload 存 `project`／`target_server`／`dest_path`（正規化後）／
    `ref`（解析後的最終值）／`hub_head`（hub 目前 `HEAD` 的完整 commit，
    純顯示用，`local_run` 拿不到就是 `None`，不影響核准請求本身是否成立）
    ，核准卡顯示這四項＋hub_head。
    """
    server_configs = server_configs or {}
    target_cfg = server_configs.get(target_server)
    if target_cfg is None or not getattr(target_cfg, "enabled", True):
        raise InvalidProjectDeployRequestError(f"未知或未啟用的機器：{target_server}")

    existing = [
        inst for inst in db.list_project_instances(project) if inst.server == target_server
    ]
    if existing:
        raise InvalidProjectDeployRequestError(
            f"專案 {project} 在機器 {target_server} 已存在 instance；"
            "更新既有 instance 屬後續功能"
        )

    repo_path = hub_repo_path(project, config.local_home_dir)
    if not Path(repo_path).is_dir():
        raise InvalidProjectDeployRequestError(
            f"專案 {project} 尚未同步到中央 hub，請先對該專案執行 hub 同步"
        )

    if local_run is None:
        raise ValueError("request_project_deploy_approval 需要 local_run，呼叫端未提供")
    if ssh_run is None:
        raise ValueError("request_project_deploy_approval 需要 ssh_run，呼叫端未提供")

    if ref is not None:
        if not ref or not _DEPLOY_REF_RE.match(ref):
            raise InvalidProjectDeployRequestError(f"ref 含不合法字元：{ref!r}")
        ref_path = shlex.quote(f"refs/heads/{ref}")
        verify_result = await local_run(
            f"git --git-dir={shlex.quote(repo_path)} rev-parse --verify {ref_path}", 10
        )
        if getattr(verify_result, "exit_status", 1) != 0:
            raise InvalidProjectDeployRequestError(f"ref 在 hub 找不到：{ref}")
        resolved_ref = ref
    else:
        symbolic_result = await local_run(
            f"git --git-dir={shlex.quote(repo_path)} symbolic-ref --short HEAD", 10
        )
        candidate = ""
        if getattr(symbolic_result, "exit_status", 1) == 0:
            candidate = (symbolic_result.stdout or "").strip()
        if not candidate:
            abbrev_result = await local_run(
                f"git --git-dir={shlex.quote(repo_path)} rev-parse --abbrev-ref HEAD", 10
            )
            if getattr(abbrev_result, "exit_status", 1) == 0:
                candidate = (abbrev_result.stdout or "").strip()
        if not candidate or candidate == "HEAD":
            raise InvalidProjectDeployRequestError(
                f"無法解析專案 {project} hub 的預設分支，請先對該專案執行 hub 同步"
            )
        resolved_ref = candidate

    head_result = await local_run(f"git --git-dir={shlex.quote(repo_path)} rev-parse HEAD", 10)
    hub_head = (
        (head_result.stdout or "").strip() if getattr(head_result, "exit_status", 1) == 0 else None
    ) or None

    if dest_path:
        raw_dest = dest_path.strip()
    else:
        roots = list(getattr(target_cfg, "project_roots", []) or [])
        if not roots:
            raise InvalidProjectDeployRequestError(
                f"機器 {target_server} 未設定 project_roots，請明確提供 dest_path"
            )
        raw_dest = f"{roots[0].rstrip('/')}/{project}"

    if not raw_dest.startswith("/"):
        raise InvalidProjectDeployRequestError("dest_path 必須是絕對路徑（以 / 開頭）")
    if ".." in raw_dest.split("/"):
        raise InvalidProjectDeployRequestError("dest_path 不可包含 .. 片段")
    normalized_dest = raw_dest.rstrip("/") or "/"
    if is_forbidden_root(normalized_dest):
        raise InvalidProjectDeployRequestError(f"禁止部署到的路徑：{normalized_dest}")

    exist_check = await ssh_run(
        target_server, f"test -e {shlex.quote(normalized_dest)} || echo NOT_EXIST", 10
    )
    if "NOT_EXIST" not in (exist_check.stdout or ""):
        empty_check = await ssh_run(target_server, f"ls -A {shlex.quote(normalized_dest)}", 10)
        if getattr(empty_check, "exit_status", 1) != 0 or (empty_check.stdout or "").strip():
            raise InvalidProjectDeployRequestError(
                f"目標路徑已存在且非空目錄：{normalized_dest}"
            )

    payload = {
        "project": project,
        "target_server": target_server,
        "dest_path": normalized_dest,
        "ref": resolved_ref,
        "hub_head": hub_head,
    }
    approval_id = db.insert_approval(
        kind="project_deploy",
        payload=payload,
        requester_actor_id=(
            request_context.actor_id if request_context is not None else None
        ),
    )
    append_audit(
        "approval_requested",
        {
            "approval_id": approval_id,
            "kind": "project_deploy",
            "project": project,
            "target_server": target_server,
            "dest_path": normalized_dest,
            "ref": resolved_ref,
        },
        path=audit_path,
        actor=audit_actor_from_request_context(request_context),
    )
    return db.get_approval(approval_id)
