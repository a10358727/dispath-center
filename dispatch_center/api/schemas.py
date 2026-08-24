"""Pydantic request contracts shared by the legacy and extracted API routers."""

from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.engineering_tasks import ENGINEERING_TASK_PROVIDER_ID
from app.node_protocol import MAX_ARTIFACT_PATH_LENGTH, MAX_ARTIFACTS_PER_REPORT


class JobCreateRequest(BaseModel):
    command: str
    type: str = "adhoc"
    project: Optional[str] = None
    require_tag: Optional[str] = None
    pin_server: Optional[str] = None
    priority: str = "normal"
    depends_on: list[int] = Field(default_factory=list)
    gpus_needed: Optional[int] = None
    #: 階段 10（PLAN.md K.1）：web/chatgpt/vllm/api，預設 "api"（未標記時
    #: 行為同現狀）。
    source: str = "api"
    #: 階段 13（PLAN.md N.7）：選填，這個任務要用某次 Codex coding run 的
    #: `changes.bundle` 當起點（下游 train／驗證任務）。驗證見
    #: `app.approvals.request_enqueue_approval()`。
    source_coding_run_id: Optional[int] = None


class StopJobRequest(BaseModel):
    """`POST /jobs/{id}/stop` 的選填 body（階段 10 前這個端點完全不吃
    body）——留空（不帶 body）時 `source` 預設 "api"，行為同現狀。"""

    source: str = "api"

    model_config = {"extra": "ignore"}


class RejectRequest(BaseModel):
    note: Optional[str] = None

    model_config = {"extra": "ignore"}


class ProjectCreateRequest(BaseModel):
    name: str
    repo_or_path: str
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    default_command: Optional[str] = None
    require_tag: Optional[str] = None
    setup_cmd: Optional[str] = None


class ProjectPatchRequest(BaseModel):
    """`PATCH /projects/{name}`（專案詳情頁計畫第 3 節）：只更新有明確帶的
    欄位（`exclude_unset`，見端點實作），空 body（沒有任何欄位）400。
    `summary` 沿用既有欄位（階段 8 第一批就有），這裡一起開放編輯，不需要
    另一支端點。"""

    goal: Optional[str] = None
    optimization_notes: Optional[str] = None
    progress: Optional[str] = None
    summary: Optional[str] = None

    model_config = {"extra": "ignore"}


class ExperimentRecordCreateRequest(BaseModel):
    """`POST /projects/{name}/records`（專案詳情頁計畫第 3 節）：手動建立
    一筆實驗紀錄。`author` 選填，省略時預設 `"user"`（網頁前端固定不帶這
    個欄位，行為等同既有設計）；有帶時必須是 `"user"` 或以 `"agent"` 開頭
    （同 `app.db._validate_record_author()` 的值域，見端點實作），這是給
    `app/mcp_bridge.py` 的 `add_experiment_record` 代理工具用的
    （帶 `"agent:chatgpt"`，PLAN.md 專案詳情頁計畫第 4 節）——本端點跟
    `app/mcp_bridge.py` 一樣是 AUTH_TOKEN 保護的可信呼叫端，`app.
    agent_tools` 的本地 vLLM 版 `add_experiment_record` 工具則完全不經過
    這支 HTTP 端點（直接呼叫 `db.insert_experiment_record()`），兩條路徑
    最終都落在同一張表、同一個值域驗證上。`job_id`／`coding_run_id`
    選填，用來把這筆紀錄掛在某次派工或 Codex 執行底下（弱關聯，同
    `app.db.insert_experiment_record()` 的既有設計——沒有 FK，不驗證所指
    id 是否存在／屬於這個專案，跟全案「弱關聯」慣例一致）。"""

    content: str
    kind: str = "note"
    title: Optional[str] = None
    author: Optional[str] = None
    job_id: Optional[int] = None
    coding_run_id: Optional[int] = None


class ExperimentRecordPatchRequest(BaseModel):
    """`PATCH /projects/{name}/records/{id}`：只更新有明確帶的欄位
    （`exclude_unset`），空 body 400。同 `insert`，不開放改 `author`。"""

    title: Optional[str] = None
    content: Optional[str] = None
    kind: Optional[str] = None

    model_config = {"extra": "ignore"}


class DatasetDerivedFromRequest(BaseModel):
    """階段 16（PLAN.md Q.3）：資料卡的 `derived_from`——`{name, version}`
    指向另一個已註冊的資料集版本，形成 lineage 鏈。"""

    name: str
    version: str


class DatasetCreateRequest(BaseModel):
    """階段 16（PLAN.md Q.3）起，`description`／`method` **強制必填**——
    刻意的 breaking change（使用者定的規則：「資料集必須明確記錄製作方式
    與數量」）。這裡刻意宣告成 `Optional[str] = None` 而不是必填的 `str`：
    缺這兩欄時要走 `create_dataset()` 裡自訂的 400（引用使用者規則的訊息
    文字），不要走 FastAPI 預設的 422 驗證錯誤格式。"""

    name: str
    version: str
    source_path: str
    description: Optional[str] = None
    method: Optional[str] = None
    derived_from: Optional[DatasetDerivedFromRequest] = None
    counts: Optional[dict[str, Any]] = None


class DatasetCardUpdateRequest(BaseModel):
    """`PATCH /datasets/{name}/{version}/card`——補登／更新資料卡，同樣
    強制 `description`／`method`（理由同 `DatasetCreateRequest`）。"""

    description: Optional[str] = None
    method: Optional[str] = None
    derived_from: Optional[DatasetDerivedFromRequest] = None
    counts: Optional[dict[str, Any]] = None


class InventoryScanRequest(BaseModel):
    """階段 8 第二批：`project_roots` 選填——省略（`None`）時從
    `server_configs[server].project_roots` 自動代入；明確帶入（含空字串以外
    的空列表 `[]`）視為「這次只掃這幾個」的覆蓋，空列表視為不合法請求（見
    app/approvals.py 的 request_inventory_scan_approval() docstring）。"""

    server: str
    project_roots: Optional[list[str]] = None


class ManualCandidateRequest(BaseModel):
    """P.1.5（2026-07-10 追加，Fable 定案）：`POST /inventory/candidates/manual`
    的 body。`name` 選填，省略時 fallback 用 `os.path.basename(path)`（見
    `app.approvals.add_manual_candidate()`）。"""

    server: str
    path: str
    name: Optional[str] = None


class ImportProjectCandidateRequest(BaseModel):
    """全部欄位選填：省略的部分核准時會 fallback 用 candidate 本身的猜測值
    （見 request_import_project_approval()）。"""

    name: Optional[str] = None
    default_command: Optional[str] = None
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    dataset_mode: Optional[str] = None
    require_tag: Optional[str] = None
    setup_cmd: Optional[str] = None
    summary: Optional[str] = None
    #: PLAN.md 2026-07-11 版 §14 切片 3:有值 → 連結到既有 Project(名稱或
    #: UUID 皆可),不新建。與其餘欄位互斥的驗證由
    #: `request_import_project_approval()` 負責(找不到目標即 400)。
    link_to_project: Optional[str] = None

    model_config = {"extra": "ignore"}


class ApplyPatchRequest(BaseModel):
    """階段 12（PLAN.md M.2）：`POST /projects/{name}/apply-patch-request`
    的 body——`server` 必填（apply_patch 只能對準一台機器，不像讀檔端點
    的 `server` 選填自動選擇）。"""

    server: str
    diff: str
    description: Optional[str] = None


class CodingTaskRequest(BaseModel):
    """階段 13（PLAN.md N.2，Codex Worker v2）：
    `POST /projects/{name}/coding-task-request` 的 body。

    v2 不再讓呼叫端指定 Codex 執行機器——`server` 從必填改成選填，只為了
    相容 v1 舊客戶端（等於 `CODEX_RUNNER_SERVER` 才接受，見
    `app.approvals.request_coding_task_approval()` 的 `legacy_server`
    參數）；`instruction` 是自然語言需求全文；`base_branch`／
    `validation_target` 選填（後者只代表後續驗證想在哪台機器跑，不代表
    Codex 執行位置，見 PLAN.md N.2 第 2 點）。"""

    instruction: str
    base_branch: Optional[str] = None
    validation_target: Optional[str] = None
    server: Optional[str] = None


class EngineeringTaskValidationRequest(BaseModel):
    tests_lint: bool = False
    build_smoke: bool = False
    continue_fixing_failures: bool = False
    worker_validation_target: Optional[str] = None

    model_config = {"extra": "forbid"}


class EngineeringTaskExecutionPermissionsRequest(BaseModel):
    modify_project_files: bool = True
    install_dependencies: bool = False
    external_network: bool = False
    environment_references: list[str] = Field(default_factory=list)
    secret_references: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class EngineeringTaskCreateRequest(BaseModel):
    """Structured immutable request；base commit 永遠由 version id 解出。

    ``allowed_paths``／``prohibited_paths`` 是 v2 final-tree path policy 的
    machine-readable 輸入；``prohibited_changes`` 仍是給 agent／核准者看的
    自然語言要求，兩者不可互相替代。
    """

    project_version_id: str
    agent_provider_id: str = ENGINEERING_TASK_PROVIDER_ID
    objective: str
    background: Optional[str] = None
    expected_changes: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    prohibited_paths: list[str] = Field(default_factory=list)
    prohibited_changes: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    validation: EngineeringTaskValidationRequest = Field(
        default_factory=EngineeringTaskValidationRequest
    )
    execution_permissions: EngineeringTaskExecutionPermissionsRequest = Field(
        default_factory=EngineeringTaskExecutionPermissionsRequest
    )

    model_config = {"extra": "forbid"}


class AgentSessionOpenRequest(BaseModel):
    """DG-AGENT-SESSION-V1 D1：`POST /projects/{name}/agent-sessions/open-request`
    的 body。`base_version_id` 是必填——session workspace 的起點永遠是一個
    明確 pin 住的 ProjectVersion，不隱含「用目前 head」。"""

    base_version_id: str
    agent_provider_id: str = "claude-code"

    model_config = {"extra": "forbid"}


class EngineeringTaskPathPolicyCoverageRequest(BaseModel):
    """`allowed_paths`／`prohibited_paths` 對 pinned base tree 的唯讀涵蓋預檢。

    純 advisory；不建立任何 approval 或 record。commit 永遠由 version id
    解出，呼叫端不能直接餵 commit（避免把這個 endpoint 當成 commit 存在性
    oracle）。
    """

    project_version_id: str
    allowed_paths: list[str] = Field(default_factory=list)
    prohibited_paths: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class EngineeringWorkerValidationRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    pin_server: str = Field(min_length=1, max_length=128)
    gpus_needed: Optional[int] = Field(default=None, ge=0, le=64)
    priority: str = "normal"
    require_tag: Optional[str] = Field(default=None, max_length=128)

    model_config = {"extra": "forbid"}


class GitInitRequest(BaseModel):
    """階段 15 Phase B（PLAN.md P.2.1）：`POST /projects/{name}/git-init-request`
    的 body——`server` 必填（git_init 只能對準一台機器，比照
    `ApplyPatchRequest`）；`extra_ignores` 選填，每項限單行 pattern，字元集
    驗證見 `app.approvals.request_git_init_approval()`。"""

    server: str
    extra_ignores: Optional[list[str]] = None


class HubSyncRequest(BaseModel):
    """階段 15 Phase B（PLAN.md P.2.2）：`POST /projects/{name}/hub-sync`
    的 body。"""

    server: str


class ProjectDeployRequest(BaseModel):
    """階段 15 Phase C（PLAN.md P.3）：`POST /projects/{name}/deploy-request`
    的 body——`target_server` 必填；`dest_path`／`ref` 選填，沒給時由
    `app.hub.request_project_deploy_approval()` 算出預設值（見該函式
    docstring）。"""

    target_server: str
    dest_path: Optional[str] = None
    ref: Optional[str] = None


class ServerConfigPayload(BaseModel):
    """一組完整的 server 設定（新增/測試 SSH 用）。故意用寬鬆的 dict-like
    模型（`extra="allow"`），實際驗證交給 `app.server_config.
    validate_server_config()`（單一驗證來源，不在 Pydantic 層重複規則）。
    """

    name: str
    host: str
    user: str
    key: str
    gpu: bool = False
    idle_gpu_util: float = 15.0
    idle_load: float = 2.0
    tags: list[str] = Field(default_factory=list)
    port: int = 22
    project_roots: list[str] = Field(default_factory=list)
    dataset_roots: list[str] = Field(default_factory=list)
    project_embedded_dataset_names: Optional[list[str]] = None
    project_exclude_names: Optional[list[str]] = None
    enabled: bool = True
    note: Optional[str] = None

    model_config = {"extra": "allow"}


class ServerUpdateRequest(BaseModel):
    name: str
    updates: dict = Field(default_factory=dict)

    model_config = {"extra": "ignore"}


class ServerNameRequest(BaseModel):
    name: str

    model_config = {"extra": "ignore"}


class ServiceAccountCreateRequest(BaseModel):
    """Non-secret input for an approval-gated service account creation."""

    name: str
    description: Optional[str] = None

    model_config = {"extra": "ignore"}


class ServiceTokenIssueRequest(BaseModel):
    """Non-secret token policy stored in the immutable approval payload."""

    label: Optional[str] = None
    scopes: list[str]
    expires_at: str

    model_config = {"extra": "ignore"}


class ProjectMembershipRequest(BaseModel):
    """Requested durable role for one actor in the route's project."""

    actor_id: str
    role: str

    model_config = {"extra": "ignore"}


class RunProfileCreateRequest(BaseModel):
    """D5 Run Profile v1 (docs/DECISIONS.md): restricted typed-parameter
    fields only, mirroring the existing Project.default_command/setup_cmd/
    require_tag legacy fields — never a free-form execution plan."""

    name: str
    command: Optional[str] = None
    setup_cmd: Optional[str] = None
    require_tag: Optional[str] = Field(default=None, max_length=128)

    model_config = {"extra": "ignore"}


class RunProfileUpdateRequest(BaseModel):
    """Proposes a new immutable revision superseding the current head."""

    command: Optional[str] = None
    setup_cmd: Optional[str] = None
    require_tag: Optional[str] = Field(default=None, max_length=128)

    model_config = {"extra": "ignore"}


class NodeEnrollRequest(BaseModel):
    """Goal 3 C2（INV-NODE-1）：替一台既有工作機登錄 Node Agent 身分。
    憑證在**核准當下**才產生，這個請求裡沒有任何 secret。"""

    server: str

    model_config = {"extra": "ignore"}


class NodeRevokeRequest(BaseModel):
    """Goal 3 C2（INV-NODE-1）：撤銷單一 node 憑證（不影響其他 node）。"""

    node_id: str
    #: Routine rotation keeps the outgoing credential valid while the agent
    #: reloads its new token.  Revoke ignores this field.
    overlap_sec: Optional[int] = Field(default=None, ge=1, le=86400)
    #: A response-lost staged delivery can only be replaced by a new approval
    #: that explicitly pins the current pending credential.
    replace_pending: bool = False

    model_config = {"extra": "ignore"}


class NodeRetireRequest(BaseModel):
    """One approved step in the routine drain/retirement lifecycle."""

    node_id: str
    action: Literal[
        "start_drain",
        "resume_assignment",
        "complete_retirement",
    ]

    model_config = {"extra": "forbid"}


class NodePollRequest(BaseModel):
    """agent → control plane 的出站輪詢（INV-NODE-1：只有出站，工作機不開
    任何入站埠）。

    DG-NODE-V2 N-1：**agent 不再指定 `job_id`**。它只問「有我的工作嗎」，
    由 control plane 用與 SSH 路徑相同的資格規則自己挑。`extra="forbid"`
    讓仍然送出 `job_id` 的舊 agent 直接被拒絕，而不是被默默忽略——默默
    忽略會讓人以為舊行為仍然有效。
    """

    #: Deliberately diverges from the `extra="ignore"` convention the other
    #: node models use: silently accepting `job_id` would leave a v1 agent
    #: believing it still chooses its own work.
    model_config = ConfigDict(extra="forbid")

    agent_version: Optional[str] = Field(default=None, min_length=1, max_length=128)


class NodeAckRequest(BaseModel):
    """agent 的 acknowledge（INV-NODE-2/3）：必須帶指令 digest,不符不執行。"""

    attempt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")
    command_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    model_config = ConfigDict(extra="forbid")


class NodeHeartbeatRequest(BaseModel):
    """心跳（INV-NODE-4：只是觀測，永不改任務狀態）。"""

    attempt_id: Optional[str] = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$"
    )
    agent_version: Optional[str] = Field(default=None, min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid")


class NodeArtifactEntry(BaseModel):
    """一筆 artifact **中繼資料**（Goal 3 C3）。沒有檔案內容欄位——這是
    刻意的：不傳位元組就不需要決定儲存位置與配額政策。``kind`` 也屬
    immutable identity，重送時不可改寫。"""

    path: str = Field(min_length=1, max_length=MAX_ARTIFACT_PATH_LENGTH)
    kind: str = Field(
        default="file",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    size_bytes: int = Field(strict=True, ge=0, le=9_223_372_036_854_775_807)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    model_config = ConfigDict(extra="forbid")


class NodeArtifactsRequest(BaseModel):
    attempt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")
    artifacts: list[NodeArtifactEntry] = Field(
        default_factory=list, max_length=MAX_ARTIFACTS_PER_REPORT
    )

    model_config = ConfigDict(extra="forbid")


class NodeAckStopRequest(BaseModel):
    """agent 對停止請求的送達回執（Goal 3 C3）。"""

    attempt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")

    model_config = ConfigDict(extra="forbid")


class NodeTerminalRequest(BaseModel):
    """終態回報（INV-NODE-4：狀態收斂的唯一依據之一）。"""

    attempt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")
    exit_code: int = Field(strict=True, ge=0, le=255)
    log_tail: str = Field(default="", max_length=16 * 1024)

    model_config = ConfigDict(extra="forbid")

    @field_validator("log_tail")
    @classmethod
    def _bound_log_tail_utf8(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 16 * 1024:
            raise ValueError("log_tail exceeds 16384 UTF-8 bytes")
        return value


class ServerBootstrapRequest(BaseModel):
    """Goal 3 Phase B B1（docs/GOAL_3_FUTURE_WORK_PLAN.md；DG-B 核准見
    docs/DECISIONS.md 2026-07-19）：typed 欄位 only——`key` 是 Server A 上的
    私鑰**路徑字串**（限 `~/.ssh/` 直接子路徑），永遠不是私鑰內容。"""

    host: str
    username: str
    key: str
    components: list[str]
    port: int = 22
    gpu: bool = False

    model_config = {"extra": "ignore"}


class DispatchPolicyCreateRequest(BaseModel):
    """Goal 2 Slice 3 (docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md): restricted
    typed-parameter fields only, mirroring Run Profile v1's pattern. This
    slice's policy object has zero runtime effect — scheduler never reads it."""

    name: str
    allowed_servers: list[str]
    require_tag: Optional[str] = Field(default=None, max_length=128)
    run_profile_id: Optional[str] = None
    dataset_required: bool = False
    max_concurrent_placements: int = 1
    valid_until: Optional[str] = None

    model_config = {"extra": "ignore"}


class DispatchPolicyUpdateRequest(BaseModel):
    """Proposes a new immutable revision superseding the current head."""

    allowed_servers: list[str]
    require_tag: Optional[str] = Field(default=None, max_length=128)
    run_profile_id: Optional[str] = None
    dataset_required: bool = False
    max_concurrent_placements: int = 1
    valid_until: Optional[str] = None

    model_config = {"extra": "ignore"}


class ExecutionPlanPreviewRequest(BaseModel):
    command: str
    project_version_id: Optional[str] = None
    run_profile_id: Optional[str] = None
    dataset_snapshot_id: Optional[str] = None
    dataset_none: bool = False
    server_config_revision_id: Optional[str] = None
    require_reproducible: bool = True


class ServerConfigRecoveryRequest(BaseModel):
    observed_yaml_sha256: str
    resolution: str


class AgentChatRequest(BaseModel):
    text: str


class AgentCmdRequest(BaseModel):
    cmd: str


ProjectRoleV2Value = Literal[
    "owner",
    "operator",
    "reviewer",
    "dataset_manager",
    "viewer",
]


class ProjectRoleChangeRequest(BaseModel):
    """Strict Product v2 request for an immutable high-risk role delta."""

    actor_id: str = Field(min_length=36, max_length=36)
    add_roles: list[ProjectRoleV2Value] = Field(max_length=5)
    remove_roles: list[ProjectRoleV2Value] = Field(max_length=5)
    expected_roles_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    model_config = ConfigDict(extra="forbid")

    @field_validator("actor_id")
    @classmethod
    def _canonical_actor_id(cls, value: str) -> str:
        try:
            normalized = str(uuid.UUID(value))
        except (ValueError, AttributeError):
            raise ValueError("actor_id must be a canonical UUID") from None
        if normalized != value:
            raise ValueError("actor_id must be a canonical UUID")
        return value

    @model_validator(mode="after")
    def _canonical_role_sets(self) -> "ProjectRoleChangeRequest":
        if self.add_roles != sorted(set(self.add_roles)):
            raise ValueError("add_roles must be sorted and unique")
        if self.remove_roles != sorted(set(self.remove_roles)):
            raise ValueError("remove_roles must be sorted and unique")
        if set(self.add_roles) & set(self.remove_roles):
            raise ValueError("add_roles and remove_roles must be disjoint")
        if not self.add_roles and not self.remove_roles:
            raise ValueError("at least one role change is required")
        return self


class ProjectConversationMessageRequest(BaseModel):
    """DG-CONVERSATION-V1 CV-2a: one user turn on a project's main AI
    conversation. ``content`` size/emptiness are deliberately *not* enforced
    here via a pydantic validator (which would surface as 422) — the route
    handler checks against
    ``app.db.Database.AI_CONVERSATION_MESSAGE_MAX_BYTES`` and raises the
    documented 400, matching the rest of this endpoint's error contract."""

    content: str

    model_config = ConfigDict(extra="forbid")


class ProjectRoleDecisionRequest(BaseModel):
    """Approve or reject one Product v2 role-change approval."""

    decision: Literal["approve", "reject"]
    note: Optional[str] = Field(default=None, max_length=2000)

    model_config = ConfigDict(extra="forbid")

    @field_validator("note")
    @classmethod
    def _bound_note_utf8(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value.encode("utf-8")) > 4000:
            raise ValueError("note exceeds 4000 UTF-8 bytes")
        return value
