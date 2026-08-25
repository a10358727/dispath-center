(function () {
  "use strict";

  //: DG-UI-UNIFICATION v1 U1 (docs/DECISIONS.md 2026-08-25): feature panels
  //: ported from the legacy surface (`static/index.html` + `static/ui.js`)
  //: into the unified Product v2 Workspace. This file is a second, sibling
  //: IIFE loaded before `workspace.js` (see `workspace.html`) and hands off
  //: through `window.WorkspaceUI`, exactly like `static/ui.js` hands off
  //: through `window.DispatchUI`. No shared module system, no build step —
  //: small helper duplication across the two workspace files is expected and
  //: accepted (frontend-architecture: vanilla JS, no framework/bundler).

  //: Ported verbatim from `static/index.html` `STATUS_LABEL`/`KIND_LABEL`.
  const STATUS_LABEL = Object.freeze({
    queued: "排隊中",
    running: "執行中",
    done: "完成",
    failed: "失敗",
    blocked: "被擋住",
    cancelled: "已取消",
    pending: "待核准",
    approved: "已核准",
    rejected: "已拒絕",
  });

  const KIND_LABEL = Object.freeze({
    enqueue: "排任務",
    stop: "停止任務",
    server_add: "新增伺服器",
    server_update: "更新伺服器",
    server_disable: "停用伺服器",
    server_delete: "刪除伺服器",
    inventory_scan: "掃描候選專案",
    import_project: "匯入候選專案",
    ignore_project_candidate: "忽略候選專案",
    ignore_nested_candidates: "批次清理巢狀候選",
    apply_patch: "套用 AI 改碼 diff",
    coding_task: "AI 寫程式任務（Codex）",
    git_init: "專案 git 化",
    project_deploy: "部署專案",
    service_account_create: "建立 Service Account",
    service_token_issue: "簽發 Service Token",
    service_token_revoke: "撤銷 Service Token",
    project_membership_upsert: "新增／更新 Project Membership",
    project_membership_remove: "移除 Project Membership",
    run_profile_create: "建立 Run Profile",
    run_profile_update: "更新 Run Profile（新 revision）",
    run_profile_archive: "封存 Run Profile",
    dispatch_policy_create: "建立調度政策",
    dispatch_policy_update: "更新調度政策（新 revision）",
    dispatch_policy_archive: "封存調度政策",
    auto_placement: "自動放置提案",
    dataset_prewarm: "資料集預熱提案",
    node_enroll: "登錄 Node Agent",
    node_revoke: "撤銷 Node Agent 憑證",
    node_rotate: "換發 Node Agent 憑證",
    node_retire: "Node Agent 例行退役",
    server_bootstrap: "空伺服器開通（bootstrap）",
    plan_run: "建立 Run（Execution Plan）",
    engineering_task_promote: "AI 工程任務：Promote 為 ProjectVersion",
    agent_session_open: "開啟 AI Agent Session",
    agent_session_checkpoint: "AI Session：建立候選版本（Checkpoint）",
    engineering_task_retry: "AI 工程任務：重試",
    engineering_task_discard: "AI 工程任務：捨棄",
    engineering_command: "AI 工程任務：執行中指令核准",
    dataset_snapshot_build: "建立資料集 snapshot",
    project_role_change: "專案角色變更",
    project_bootstrap_v2: "建立新專案（Project Bootstrap）",
    environment_change_v2: "Environment 變更",
    run_template_change_v2: "Run Template 變更",
    project_defaults_change_v2: "Project Defaults 變更",
    dataset_asset_adoption_v2: "Dataset Asset 認領",
    dataset_alias_change_v2: "Dataset Alias 變更",
    dataset_share_offer_v2: "Dataset 分享提議",
    dataset_share_accept_v2: "Dataset 分享接受",
    dataset_grant_revoke_v2: "Dataset 授權變更",
    dataset_publish_v2: "Dataset 發佈",
    execution_plan_v2: "建立 Run（ExecutionPlan v2）",
    experiment_create_v2: "建立 Experiment（多組 Run）",
    project_instance_update_v2: "部署已核准版本到機器",
  });

  //: Ported verbatim from `static/index.html` `ONE_TIME_SECRET_APPROVAL_KINDS`
  //: — must stay in sync with the backend source of truth
  //: `app.db.ONE_TIME_SECRET_APPROVAL_KINDS`.
  const ONE_TIME_SECRET_APPROVAL_KINDS = Object.freeze(
    new Set(["service_token_issue", "node_enroll", "node_rotate"])
  );

  //: Ported from `static/index.html` `SUPPORTING_APPROVAL_CATEGORIES`
  //: (kept as the full legacy set rather than only the plan's headline
  //: four, since every legacy kind needs a category and none should regress
  //: to "未分類" when a matching legacy category already exists).
  const SUPPORTING_APPROVAL_CATEGORIES = Object.freeze({
    ai_engineering: {
      label: "AI 工程",
      kinds: new Set([
        "coding_task", "apply_patch", "engineering_task_promote",
        "engineering_task_retry", "engineering_task_discard",
        "engineering_command", "agent_session_open", "agent_session_checkpoint",
      ]),
    },
    runtime: {
      label: "Runtime Job",
      kinds: new Set(["enqueue", "stop", "plan_run", "execution_plan_v2", "experiment_create_v2"]),
    },
    infrastructure: {
      label: "基礎設施",
      kinds: new Set([
        "inventory_scan", "import_project", "ignore_project_candidate",
        "ignore_nested_candidates", "server_add", "server_update",
        "server_disable", "server_delete", "server_bootstrap",
        "node_enroll", "node_revoke", "node_rotate", "node_retire",
      ]),
    },
    deployment: {
      label: "部署",
      kinds: new Set(["project_deploy", "project_instance_update_v2"]),
    },
    project_version: { label: "專案版本", kinds: new Set(["git_init"]) },
    identity: {
      label: "身分與管理",
      kinds: new Set([
        "service_account_create", "service_token_issue", "service_token_revoke",
        "project_membership_upsert", "project_membership_remove",
        "project_role_change", "project_bootstrap_v2",
      ]),
    },
    automated_dispatch: {
      label: "自動調度",
      kinds: new Set([
        "run_profile_create", "run_profile_update", "run_profile_archive",
        "dispatch_policy_create", "dispatch_policy_update", "dispatch_policy_archive",
        "auto_placement", "dataset_prewarm", "dataset_snapshot_build",
      ]),
    },
    dataset: {
      label: "資料集",
      kinds: new Set([
        "dataset_asset_adoption_v2", "dataset_alias_change_v2",
        "dataset_share_offer_v2", "dataset_share_accept_v2",
        "dataset_grant_revoke_v2", "dataset_publish_v2",
      ]),
    },
    environment: {
      label: "環境與範本",
      kinds: new Set(["environment_change_v2", "run_template_change_v2", "project_defaults_change_v2"]),
    },
  });

  function approvalCategoryLabel(kind) {
    for (const category of Object.values(SUPPORTING_APPROVAL_CATEGORIES)) {
      if (category.kinds.has(kind)) return category.label;
    }
    return "未分類";
  }

  function node(tag, text, className) {
    const created = document.createElement(tag);
    if (typeof text === "string") created.textContent = text;
    if (className) created.className = className;
    return created;
  }

  function detailList(rows) {
    const dl = node("dl", null, "detail-list");
    for (const [label, value] of rows) {
      const row = node("div");
      row.append(node("dt", label), node("dd", value == null || value === "" ? "-" : String(value)));
      dl.append(row);
    }
    return dl;
  }

  function paragraph(text) {
    return node("p", text, "section-note");
  }

  function warningParagraph(text) {
    return node("p", `⚠ ${text}`, "section-note approval-summary-warning");
  }

  function commandBlock(text) {
    return node("pre", text || "", "approval-summary-pre");
  }

  function joinList(value, fallback) {
    return Array.isArray(value) && value.length ? value.join(", ") : (fallback || "-");
  }

  //: Per-kind Chinese summary — field extraction ported from
  //: `static/index.html` `renderApprovals()` (:2873-2988) and its
  //: `*BodyHtml()` helpers, rebuilt with `node()`/textContent instead of the
  //: legacy innerHTML template-string convention.
  const SUMMARY_BUILDERS = Object.freeze({
    enqueue(p) {
      const nodes = [commandBlock(p.command)];
      nodes.push(detailList([
        ["專案", p.project],
        ["優先權", p.priority || "normal"],
        ["指定機器", p.pin_server || "自動"],
        ["require_tag", p.require_tag],
      ]));
      if (p.sync_plan) {
        nodes.push(paragraph(
          `＋自動附帶同步任務：${p.sync_plan.dataset_name}@${p.sync_plan.dataset_version} → ${p.sync_plan.target_server}`
        ));
      }
      if (p.setup_plan) {
        nodes.push(paragraph(`＋自動附帶 setup 任務（首次跑此專案）：${p.setup_plan.target_server}`));
      }
      if (p.warning) nodes.push(warningParagraph(p.warning));
      return nodes;
    },
    stop(p) {
      return [paragraph(`停止任務 #${p.job_id != null ? p.job_id : "-"}`)];
    },
    server_add(p) {
      return [detailList([
        ["名稱", p.name],
        ["host", p.host],
        ["user", p.user],
        ["port", p.port],
        ["project_roots", joinList(p.project_roots)],
      ])];
    },
    server_update(p) {
      return [detailList([
        ["機器", p.name],
        ["更新欄位", JSON.stringify(p.updates || {})],
      ])];
    },
    server_disable(p) {
      return [detailList([["機器", p.name]])];
    },
    server_delete(p) {
      return [detailList([["機器", p.name]])];
    },
    server_bootstrap(p) {
      const sha = typeof p.script_sha256 === "string" ? p.script_sha256.slice(0, 12) : "-";
      return [
        detailList([
          ["目標", `${p.username || "-"}@${p.host || "-"}:${p.port != null ? p.port : "-"}`],
          ["key", p.key],
          ["元件", joinList(p.components)],
          ["GPU 檢查", p.gpu ? "是" : "否"],
          ["腳本", `${p.script_version || "-"}（SHA ${sha}…）`],
        ]),
        paragraph("核准後會 SSH 到目標機執行 bootstrap 並留下報告；系統工具缺失只會如實回報，不會提權安裝。"),
      ];
    },
    node_enroll(p) {
      return [
        detailList([["機器", p.server]]),
        warningParagraph("核准 response 會回傳一次性 token；此頁不會擷取 secret，因此只能用安全管理 client 核准。"),
      ];
    },
    node_rotate(p) {
      const mode = p.rotation_mode === "staged_activation" ? "staged activation" : "legacy overlap";
      return [
        detailList([
          ["Node", p.node_id],
          ["機器", p.server],
          ["模式", mode],
          ["舊憑證 grace", p.overlap_sec != null ? `${p.overlap_sec} 秒` : "立即"],
        ]),
        warningParagraph("核准 response 可能包含一次性 token/activation nonce；此頁不會擷取 secret，因此只能用安全管理 client 核准。"),
      ];
    },
    node_retire(p) {
      const actionLabels = {
        start_drain: "開始 drain（停止新派工，保留既有 ownership）",
        resume_assignment: "退出 drain（恢復新派工資格）",
        complete_retirement: "active=0 後完成例行退役",
      };
      return [detailList([
        ["Node", p.node_id],
        ["機器", p.server],
        ["動作", actionLabels[p.action] || p.action],
      ])];
    },
    node_revoke(p) {
      return [
        detailList([["撤銷 node", p.node_id], ["機器", p.server]]),
        warningParagraph("緊急撤權會立即拒絕該 Node 的全部憑證；active attempt 只進入 unknown/security hold，不會 failed、requeue 或 SSH fallback。"),
      ];
    },
    inventory_scan(p) {
      return [detailList([
        ["機器", p.server],
        ["project_roots", joinList(p.project_roots)],
      ])];
    },
    import_project(p) {
      return [detailList([
        ["名稱", p.name],
        ["候選 ID", p.candidate_id],
        ["dataset_mode", p.dataset_mode],
      ])];
    },
    ignore_project_candidate(p) {
      return [detailList([["候選 ID", p.candidate_id]])];
    },
    ignore_nested_candidates(p) {
      const items = Array.isArray(p.items) ? p.items : [];
      const lines = items.map((item) => `${item.name_guess || "(未命名)"} — ${item.path}`).join("\n");
      return [
        paragraph(`共 ${items.length} 筆巢狀候選將標為已忽略：`),
        commandBlock(lines),
      ];
    },
    apply_patch(p) {
      return [
        detailList([["專案", p.project], ["機器", p.server], ["說明", p.description]]),
        commandBlock(p.diff),
      ];
    },
    coding_task(p, approvalId) {
      const immutable = Boolean(
        p.engineering_task_id || p.contract_version || p.project_version_id || p.base_commit
      );
      const networkLabel = p.network_access === undefined ? "未知" : (p.network_access ? "允許" : "停用");
      if (immutable) {
        const contract = p.execution_contract || {};
        const runner = contract.runner || {};
        const dependencyLabel = p.dependency_installation === undefined
          ? "未知"
          : (p.dependency_installation ? "允許" : "停用");
        return [
          paragraph("Immutable AI Engineering Task：核准基準是下列 ProjectVersion 與 exact commit，不會在執行時重新解析 branch HEAD。"),
          detailList([
            ["專案", p.project],
            ["Agent", p.agent_provider_id],
            ["ProjectVersion ID", p.project_version_id],
            ["Exact base commit", p.base_commit],
            ["Runner", p.runner_server || runner.name],
            ["contract", p.contract_version],
            ["Execution source", contract.source_kind || p.source_kind],
            ["workspace", contract.workspace_rel],
            ["External network", networkLabel],
            ["Dependency installation", dependencyLabel],
            ["validation target", p.validation_target],
          ]),
          commandBlock(p.instruction),
        ];
      }
      return [
        detailList([
          ["專案", p.project],
          ["Codex Runner", p.runner_server || p.server],
          ["base branch", p.base_branch || "目前 HEAD（執行時解析，未固定）"],
          ["允許連網", networkLabel],
          ["預計 workspace", `tasks/${approvalId != null ? approvalId : "-"}`],
          ["validation target", p.validation_target],
        ]),
        commandBlock(p.instruction),
      ];
    },
    git_init(p) {
      return [
        detailList([["專案", p.project], ["機器", p.server]]),
        commandBlock(p.gitignore),
      ];
    },
    project_deploy(p) {
      return [detailList([
        ["專案", p.project],
        ["目標機器", p.target_server],
        ["目的路徑", p.dest_path],
        ["ref", p.ref],
        ["hub HEAD", p.hub_head],
      ])];
    },
    agent_session_open(p) {
      return [paragraph(`想為專案「${p.project || "-"}」開啟一個隔離的開發工作區（所有修改都在工作區內，發布前需你再次核准）。`)];
    },
    agent_session_checkpoint(p) {
      const shortSession = String(p.session_id || "").slice(0, 8) || "-";
      return [paragraph(`把 Session ${shortSession}（專案：${p.project_name || "-"}）的修改打包並驗證成可發布的候選版本。`)];
    },
    plan_run(p) {
      const shortPlanId = String(p.plan_id || "").slice(0, 8) || "-";
      return [paragraph(`在專案「${p.project_name || "-"}」建立並執行一次 Run（Execution Plan #${shortPlanId}）；機器與版本已在建立當下用雜湊值鎖定，核准後才會實際派發到機器，完整雜湊值可在下方「查看原始內容」查看。`)];
    },
    engineering_task_promote(p) {
      const shortCommit = String(p.git_commit || "").slice(0, 12) || "-";
      return [paragraph(`把已驗證的候選正式發布為專案「${p.project_name || "-"}」的新版本（目標 commit：${shortCommit}）。`)];
    },
    service_account_create(p) {
      return [detailList([
        ["名稱", p.name],
        ["actor ID", p.actor_id],
        ["說明", p.description || "（無）"],
      ])];
    },
    service_token_issue(p) {
      return [
        detailList([
          ["Service Account", p.service_account_actor_id],
          ["標籤", p.label || "（無）"],
          ["到期", p.expires_at],
          ["Scopes", joinList(p.scopes, "（無）")],
        ]),
        warningParagraph("Token secret 只會在核准 response 出現一次；此通用頁不會擷取 secret，因此不提供核准按鈕，請使用能安全接收一次性 response 的管理 client，或在此拒絕。"),
      ];
    },
    service_token_revoke(p) {
      return [detailList([["撤銷 token ID", p.token_id]])];
    },
    project_membership_upsert(p) {
      return [detailList([
        ["Project ID", p.project_id],
        ["Actor ID", p.actor_id],
        ["角色", p.role],
      ])];
    },
    project_membership_remove(p) {
      return [detailList([
        ["Project ID", p.project_id],
        ["移除 Actor ID", p.actor_id],
      ])];
    },
    run_profile_create(p) {
      return [
        detailList([
          ["專案", p.project_name || p.project],
          ["Run Profile", p.name],
          ["setup_cmd", p.setup_cmd || "（無）"],
          ["require_tag", p.require_tag],
        ]),
        commandBlock(p.command || "（未設定 command）"),
      ];
    },
    run_profile_update(p) {
      return [
        detailList([
          ["專案", p.project_name || p.project],
          ["Run Profile", p.name],
          ["setup_cmd", p.setup_cmd || "（無）"],
          ["require_tag", p.require_tag],
          ["基於 revision", p.based_on_revision],
        ]),
        commandBlock(p.command || "（未設定 command）"),
      ];
    },
    run_profile_archive(p) {
      return [paragraph(`封存 Run Profile：${p.name || "-"}（基於 revision ${p.based_on_revision != null ? p.based_on_revision : "-"}；封存後政策無法再解析到它）`)];
    },
    dispatch_policy_create(p) {
      return [detailList([
        ["專案", p.project_name || p.project],
        ["政策", p.name],
        ["允許機器", joinList(p.allowed_servers)],
        ["Run Profile ID", p.run_profile_id || "（用專案 default_command）"],
        ["需要資料集", p.dataset_required ? "是" : "否"],
        ["同時放置上限", p.max_concurrent_placements],
        ["有效期限", p.valid_until || "（無）"],
      ])];
    },
    dispatch_policy_update(p) {
      return [detailList([
        ["專案", p.project_name || p.project],
        ["政策", p.name],
        ["允許機器", joinList(p.allowed_servers)],
        ["Run Profile ID", p.run_profile_id || "（用專案 default_command）"],
        ["需要資料集", p.dataset_required ? "是" : "否"],
        ["同時放置上限", p.max_concurrent_placements],
        ["有效期限", p.valid_until || "（無）"],
        ["基於 revision", p.based_on_revision],
      ])];
    },
    dispatch_policy_archive(p) {
      return [paragraph(`封存調度政策：${p.name || "-"}（基於 revision ${p.based_on_revision != null ? p.based_on_revision : "-"}；封存後停止提案）`)];
    },
    auto_placement(p) {
      const sha = typeof p.command_sha256 === "string" ? p.command_sha256.slice(0, 12) : "-";
      return [
        paragraph(`系統提案：在閒置機器「${p.server || "-"}」放置專案「${p.project || "-"}」的任務`),
        commandBlock(p.command),
        detailList([
          ["政策 ID", p.policy_id],
          ["revision", p.policy_revision],
          ["指令 SHA", `${sha}…`],
          ["setup_cmd", p.setup_cmd || "（無）"],
          ["require_tag", p.require_tag],
          ["優先權", p.priority || "normal"],
        ]),
        paragraph("核准後會 enqueue 一個 job 並由 scheduler 派工；拒絕只影響本次提案（冷卻期後可能再提）。"),
      ];
    },
    dataset_prewarm(p) {
      const cachedOn = Number.isFinite(Number(p.cached_on_count)) ? Number(p.cached_on_count) : null;
      const why = cachedOn === null ? "-" : `目前有 ${cachedOn} 台其他已啟用機器已快取這個版本`;
      return [
        paragraph(`系統提案：預先同步資料集到剛開通、快取還空的機器「${p.server || "-"}」`),
        detailList([
          ["資料集", `${p.dataset || "-"}@${p.version || "-"}`],
          ["為什麼是這個", `${why}（資料引力；同分時挑較小的）`],
        ]),
        paragraph("核准後只建立一個 sync 任務（與手動派工附帶的同步走同一條路徑）；拒絕只影響本次提案，冷卻期後才可能再提。"),
      ];
    },
  });

  //: Builds the Chinese summary block for one approval detail (the response
  //: shape of `GET /api/v2/approvals/{id}`). Unknown kinds (no ported
  //: summary — the newer typed `_v2` kinds already have their own dedicated
  //: review panels in `workspace.js`, plus a residual handful of legacy
  //: kinds not yet ported) fall back to a warning line; the raw payload
  //: stays available via the caller's collapsed "查看原始內容" `<details>`.
  function buildApprovalSummaryNodes(approval) {
    const payload = (approval && approval.payload) || {};
    const builder = approval && SUMMARY_BUILDERS[approval.kind];
    if (typeof builder === "function") {
      try {
        return builder(payload, approval.id);
      } catch (_error) {
        // Fall through to the unknown-kind warning rather than breaking the
        // whole review panel on an unexpected payload shape.
      }
    }
    return [warningParagraph("此 approval kind 尚無專用摘要；核准前請檢查下方「查看原始內容」的完整 payload。")];
  }

  function renderApprovalSummary(container, approval) {
    container.replaceChildren();
    for (const child of buildApprovalSummaryNodes(approval)) {
      container.append(child);
    }
  }

  //: DG-UI-UNIFICATION v1 U3 (docs/DECISIONS.md 2026-08-25): jobs/runtime
  //: panel ported from `static/index.html` `elapsed()`/`renderJobs()`
  //: (:3315-3364) and `static/ui.js`'s job-results panel (:4591-4684). Pure
  //: business logic (elapsed formatting, which actions apply to a job, the
  //: v2 result-download href) lives here so it stays testable/pinnable
  //: independent of the DOM-node/event-wiring in `workspace.js`.

  function jobElapsed(job) {
    if (!job || !job.started_at) return "-";
    const start = new Date(job.started_at);
    const end = job.finished_at ? new Date(job.finished_at) : new Date();
    const secs = Math.max(0, Math.round((end - start) / 1000));
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    return `${m}分${s}秒`;
  }

  //: Ported verbatim from `renderJobs()`'s per-row branching: 查看日誌
  //: always available; 取消 only `queued` non-engineering-owned jobs; 停止
  //: only `running` jobs; 診斷 only `failed` jobs that are not engineering-
  //: protected. Engineering-owned/validation-linked jobs get a placeholder
  //: note instead of a 重跑 button -- rerun itself is not ported until U6.
  function jobRowActions(job) {
    const engineeringOwned = Boolean(job && job.engineering_task_id);
    const engineeringProtected = engineeringOwned
      || Boolean(job && job.engineering_validation_request_id);
    return {
      log: true,
      cancel: Boolean(job) && job.status === "queued" && !engineeringOwned,
      stop: Boolean(job) && job.status === "running",
      diagnose: Boolean(job) && job.status === "failed" && !engineeringProtected,
      engineering: engineeringProtected,
    };
  }

  //: Ported from `static/ui.js` `jobResultDownloadHref()`, retargeted at the
  //: `/api/v2/jobs` wrapper surface (DG-UI-UNIFICATION v1 U3).
  function jobResultDownloadHref(jobId, path) {
    const segments = String(path)
      .split("/")
      .map((segment) => encodeURIComponent(segment));
    return `/api/v2/jobs/${encodeURIComponent(jobId)}/results/${segments.join("/")}`;
  }

  //: DG-UI-UNIFICATION v1 U4 (docs/DECISIONS.md 2026-08-25): infrastructure
  //: panel (workers/idle-summary/inventory/codex runner) ported from
  //: `static/index.html`'s `renderServerConfigTable()` (:6944-7012) and
  //: `renderCodingRunnerInfrastructureStatus()` (:6840-6909). Pure business
  //: logic (health-line text, idle status label, the exact
  //: 未連線/未設定/離線/探測失敗/未安裝/未登入/正常 branch order) lives here,
  //: matching the jobs-panel split above.

  function formatGiB(bytes) {
    if (typeof bytes !== "number" || !Number.isFinite(bytes)) return "未知";
    return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
  }

  //: One line merging the live `ServerState` health fields (from
  //: `GET /api/v2/servers`) into the worker table row, matching the plan's
  //: "text is fine" allowance instead of a GPU bar widget.
  function serverHealthLine(serverState) {
    if (!serverState || serverState.online !== true) return "（無即時觀測資料）";
    const gpu = serverState.gpu_util_max == null ? "無 GPU" : `${serverState.gpu_util_max}%`;
    const load = serverState.load1 == null ? "未知" : String(serverState.load1);
    return `GPU 使用率 ${gpu}｜負載 ${load}｜磁碟餘量 ${formatGiB(serverState.disk_avail_bytes)}`;
  }

  const IDLE_SUMMARY_STATUS_LABEL = Object.freeze({
    unknown: "未知（無樣本）",
    ok: "有樣本",
  });

  function idleSummaryStatusLabel(status) {
    return IDLE_SUMMARY_STATUS_LABEL[status] || status;
  }

  //: Ported branch order verbatim from `renderCodingRunnerInfrastructureStatus()`
  //: (`static/index.html` :6849-6908): 未連線（fetch 本身失敗）／未設定／
  //: 離線／探測失敗／未安裝／未登入／正常（busy 也是成功狀態，不是
  //: unavailable）。回傳純資料描述，DOM 節點交給呼叫端建立。
  function codexRunnerStatusView(status, connectionFailed) {
    if (connectionFailed) {
      return { variant: "disconnected", title: "Coding Runner 狀態端點無法連線", note: "無法連線不代表 Coding Task failed。" };
    }
    const st = status || {};
    if (!st.configured) {
      return { variant: "empty", title: "Coding Runner 尚未設定", note: "未設定 CODEX_RUNNER_SERVER。" };
    }
    if (st.online !== true) {
      return { variant: "disconnected", title: "Coding Runner 離線", note: "Runner 無法連線；不代表 Codex 未安裝，也不代表既有任務 failed。" };
    }
    if (st.probe_status === "probe_failed") {
      return { variant: "disconnected", title: "Runner 線上，但 Codex 能力探測失敗", note: "目前無法確認安裝或登入狀態；不會誤報為 Codex 未安裝。" };
    }
    if (st.codex_installed === false) {
      return { variant: "error", title: "Runner 線上，但 Codex 尚未安裝", note: "" };
    }
    if (st.authenticated !== true) {
      return { variant: "error", title: "Runner 線上，但 Codex 尚未登入", note: "" };
    }
    return {
      variant: "success",
      title: st.busy ? "Coding Runner 忙碌；仍可送出核准並等待排程" : "Coding Runner 可用",
      note: "Runner 與 Codex 登入狀態已確認；busy 不等於 unavailable。",
      summary: [
        ["Server", st.server || "-"],
        ["探測狀態", st.probe_status || "online"],
        ["Codex 版本", st.codex_version || "版本未知"],
        ["認證模式", st.auth_mode || "已驗證"],
        ["併發上限", st.max_concurrency == null ? "1" : String(st.max_concurrency)],
        ["執行中 job", st.running_job_id == null ? "無" : String(st.running_job_id)],
      ],
    };
  }

  //: DG-UI-UNIFICATION v1 U5: `project_instances.state`（背景 reconcile
  //: 落地，見 `app/project_instances.py`）中文標籤，ported from legacy
  //: `instanceStateBadgeHtml()`'s badge vocabulary
  //: (available/missing/dirty/diverged/unknown).
  const INSTANCE_STATE_LABEL = Object.freeze({
    available: "可用",
    missing: "缺",
    dirty: "有未提交",
    diverged: "分歧",
    unknown: "未知",
  });

  function instanceStateLabel(value) {
    return INSTANCE_STATE_LABEL[value] || `未知（${value}）`;
  }

  //: DG-UI-UNIFICATION v1 U6a (docs/DECISIONS.md 2026-08-25): AI 工程
  //: (Engineering Task wizard + task detail) ported from `static/ui.js`'s
  //: wizard logic (:229-421, 754-...) and detail-tab renderers (:1778-2452).
  //: `renderEngineeringTaskInstruction()` below is ported **byte-for-byte**
  //: (English section headings/sentences unchanged) -- it builds the exact
  //: machine-readable instruction text sent to the agent
  //: (`app.engineering_tasks.render_engineering_task_instruction()` is the
  //: server-side twin re-derivation used at approval time); only the
  //: surrounding wizard UI labels are Chinese. `tests/test_identity_workspace_v2.py`
  //: pins one exact sentence from this function to guard the contract.
  const ENGINEERING_INSTRUCTION_LIMIT = 4000;

  function engineeringCodePointLength(value) {
    return Array.from(String(value || "")).length;
  }

  function engineeringBulletItems(rawValue) {
    const trimmed = String(rawValue || "").trim();
    if (!trimmed) return [];
    return trimmed
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function engineeringCompareUtf8(left, right) {
    const encoder = new TextEncoder();
    const a = encoder.encode(left);
    const b = encoder.encode(right);
    const length = Math.min(a.length, b.length);
    for (let index = 0; index < length; index += 1) {
      if (a[index] !== b[index]) return a[index] - b[index];
    }
    return a.length - b.length;
  }

  function engineeringPathScopeMatches(scope, path) {
    if (scope === ".") return true;
    if (scope.endsWith("/")) {
      return path === scope.slice(0, -1) || path.startsWith(scope);
    }
    return path === scope;
  }

  function engineeringCanonicalPathItems(rawValue) {
    const unique = Array.from(new Set(engineeringBulletItems(rawValue))).sort(engineeringCompareUtf8);
    if (unique.includes(".")) return ["."];
    const subtrees = [];
    unique.filter((item) => item.endsWith("/")).forEach((scope) => {
      const root = scope.slice(0, -1);
      if (!subtrees.some((parent) => engineeringPathScopeMatches(parent, root))) {
        subtrees.push(scope);
      }
    });
    const exact = unique.filter(
      (scope) => !scope.endsWith("/") &&
        !subtrees.some((parent) => engineeringPathScopeMatches(parent, scope))
    );
    return [...subtrees, ...exact].sort(engineeringCompareUtf8);
  }

  function engineeringPathScopeError(scope) {
    const encoder = new TextEncoder();
    if (
      scope.normalize("NFC") !== scope ||
      /[\u0000-\u001f\u007f]/.test(scope) ||
      scope.startsWith("/") ||
      scope.startsWith("~") ||
      /^[A-Za-z]:/.test(scope) ||
      scope.includes("\\") ||
      encoder.encode(scope).length > 1024
    ) {
      return true;
    }
    if (scope === ".") return false;
    const path = scope.endsWith("/") ? scope.slice(0, -1) : scope;
    if (!path) return true;
    const components = path.split("/");
    return components.some(
      (component) => !component || component === "." || component === ".." ||
        component.toLocaleLowerCase("en-US") === ".git"
    );
  }

  function validateEngineeringPathPolicyInputs(values) {
    const allowed = engineeringBulletItems(values.allowedPaths);
    const prohibited = engineeringBulletItems(values.prohibitedPaths);
    const all = [...allowed, ...prohibited];
    const encoder = new TextEncoder();
    if (all.length > 256 ||
        all.reduce((total, scope) => total + encoder.encode(scope).length, 0) > 24 * 1024) {
      return { field: "allowed", message: "Path policy 最多 256 項，總 UTF-8 大小不得超過 24 KiB。" };
    }
    if (allowed.some(engineeringPathScopeError)) {
      return {
        field: "allowed",
        message: "允許路徑必須是 canonical relative POSIX path；目錄以 / 結尾，整個 repository 用 .。",
      };
    }
    if (prohibited.some(engineeringPathScopeError)) {
      return {
        field: "prohibited",
        message: "禁止路徑必須是 canonical relative POSIX path；不可使用絕對路徑、..、反斜線或 .git。",
      };
    }
    return null;
  }

  function addEngineeringInstructionSection(parts, heading, items) {
    if (!items.length) return;
    parts.push(`${heading}:\n${items.map((item) => `- ${item}`).join("\n")}`);
  }

  /**
   * Ported verbatim from `static/ui.js` `renderStructuredInstruction()`
   * (English text is a machine contract sent to the agent; pinned server-side
   * by `tests/test_engineering_tasks.py::test_structured_renderer_has_fixed_order_and_normalizes_items`
   * and client-side by `tests/test_identity_workspace_v2.py`). Deterministic,
   * pure function of the wizard's field values -- no value here is
   * interpreted as a command or inserted into a shell.
   */
  function renderEngineeringTaskInstruction(values, { enforceFinalGitPaths = false } = {}) {
    const parts = ["AI Engineering Task"];
    addEngineeringInstructionSection(parts, "Task objective", engineeringBulletItems(values.objective));
    addEngineeringInstructionSection(parts, "Background and relevant context", engineeringBulletItems(values.background));
    addEngineeringInstructionSection(parts, "Expected changes", engineeringBulletItems(values.expectedChanges));
    addEngineeringInstructionSection(parts, "Non-goals", engineeringBulletItems(values.nonGoals));
    addEngineeringInstructionSection(
      parts,
      "Allowed modification scope",
      enforceFinalGitPaths
        ? engineeringCanonicalPathItems(values.allowedPaths)
        : engineeringBulletItems(values.allowedPaths)
    );
    addEngineeringInstructionSection(
      parts,
      "Prohibited paths",
      enforceFinalGitPaths
        ? engineeringCanonicalPathItems(values.prohibitedPaths)
        : engineeringBulletItems(values.prohibitedPaths)
    );
    addEngineeringInstructionSection(parts, "Prohibited changes", engineeringBulletItems(values.prohibitedChanges));
    addEngineeringInstructionSection(parts, "Acceptance criteria", engineeringBulletItems(values.acceptanceCriteria));

    const validation = [];
    if (values.runTests) {
      validation.push(
        "Run the repository's relevant tests and lint checks only inside this current sandboxed agent turn; the outer Runner will not execute repository code after the turn."
      );
    }
    if (values.runBuild) {
      validation.push(
        "Run relevant build and smoke checks only inside this current sandboxed agent turn."
      );
    }
    if (values.autoFix) {
      validation.push("Where feasible in the current agent turn, analyze and fix validation failures.");
    }
    if (values.workerValidation && values.validationTarget) {
      validation.push(
        `Record ${values.validationTarget} as a worker validation preference; do not assume a worker Job exists.`
      );
    }
    addEngineeringInstructionSection(parts, "Validation strategy", validation);

    const execution = [
      "Modify project files only inside the existing isolated Git worktree.",
      "Dependency installation is not authorized by this form; do not install dependencies.",
      "External network access is not authorized by this form; do not access external networks.",
    ];
    execution.push(
      enforceFinalGitPaths
        ? "The final Git diff is technically checked against the approved path policy on the Runner before bundling and independently on Server A before acceptance."
        : "Allowed/prohibited path entries are advisory approval requirements in this compatibility mode; platform path enforcement is not available."
    );
    if (enforceFinalGitPaths) {
      execution.push(
        "This final-result policy does not claim turn-time filesystem confinement; temporary writes during the agent turn remain outside this guarantee."
      );
    }
    addEngineeringInstructionSection(parts, "Requested execution behavior", execution);
    return parts.join("\n\n");
  }

  //: Short display id: native tasks use a UUID, legacy adapter rows use
  //: `legacy-coding-run-{n}` (see `app.engineering_presentation.
  //: legacy_coding_run_to_engineering_task`) -- both are shown truncated so
  //: the task list table stays scannable.
  function engineeringTaskShortId(id) {
    const text = String(id || "");
    if (text.startsWith("legacy-coding-run-")) return text;
    return text.length > 12 ? `${text.slice(0, 12)}…` : text;
  }

  //: `task.available_actions` is always the plain dict shape produced by
  //: `app.engineering_presentation.engineering_available_actions()`
  //: (`{action_name: {enabled, reason, ...}}`); this is a defensive read,
  //: not a second source of truth -- every button stays disabled unless the
  //: server's own dict says `enabled === true`.
  function engineeringAction(task, name) {
    const actions = task && task.available_actions;
    if (!actions || typeof actions !== "object") return null;
    const action = actions[name];
    return action && typeof action === "object" ? action : null;
  }

  function engineeringActionEnabled(task, name) {
    const action = engineeringAction(task, name);
    return Boolean(action && action.enabled === true);
  }

  window.WorkspaceUI = Object.freeze({
    STATUS_LABEL,
    KIND_LABEL,
    INSTANCE_STATE_LABEL,
    instanceStateLabel,
    ONE_TIME_SECRET_APPROVAL_KINDS,
    SUPPORTING_APPROVAL_CATEGORIES,
    approvalCategoryLabel,
    buildApprovalSummaryNodes,
    renderApprovalSummary,
    jobElapsed,
    jobRowActions,
    jobResultDownloadHref,
    formatGiB,
    serverHealthLine,
    idleSummaryStatusLabel,
    codexRunnerStatusView,
    ENGINEERING_INSTRUCTION_LIMIT,
    engineeringCodePointLength,
    engineeringBulletItems,
    engineeringCanonicalPathItems,
    engineeringPathScopeError,
    validateEngineeringPathPolicyInputs,
    renderEngineeringTaskInstruction,
    engineeringTaskShortId,
    engineeringAction,
    engineeringActionEnabled,
  });
})();
