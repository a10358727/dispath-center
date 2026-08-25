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

  window.WorkspaceUI = Object.freeze({
    STATUS_LABEL,
    KIND_LABEL,
    ONE_TIME_SECRET_APPROVAL_KINDS,
    SUPPORTING_APPROVAL_CATEGORIES,
    approvalCategoryLabel,
    buildApprovalSummaryNodes,
    renderApprovalSummary,
  });
})();
