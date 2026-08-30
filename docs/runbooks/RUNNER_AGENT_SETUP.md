# Runner agent 安裝（dispatch-agent）— 草稿

> 狀態：草稿。DG-AGENT-RUNTIME-V3 Phase 1a 落地時補齊實際指令與輸出範例；在那之前本檔只列步驟骨架。

runner（例：`worker_5090_106`，使用者 `bgab141`）上要做的事，全部以該使用者身分、**非 root**：

1. **Claude 訂閱憑證**：`claude login`（互動一次）→ `claude setup-token` 產生一年期 OAuth token → 存到
   `~/.config/dispatch-agent/claude.env`（`CLAUDE_CODE_OAUTH_TOKEN=…`，0600）。Server A 永不持有它（INV-AGENT-1）。
2. **安裝服務**：`python3 -m pip install --user dispatch-agent-<version>.whl`（wheel 由 CI 建置；內含 Claude Agent SDK，
   SDK 自帶 Claude Code 引擎，不需另裝 CLI）。
3. **登錄**：在 Studio「伺服器與硬體」對該機器按「登錄 runner agent」→ 核准卡 `agent_runner_enroll` → 核准後一次性顯示
   credential → 貼進 runner 的 `~/.config/dispatch-agent/agent.env`（`DISPATCH_AGENT_CREDENTIAL=…`，0600）。
4. **設定**：`~/.config/dispatch-agent/config.toml`：`server_url = "https://formosa-desktop.tail552541.ts.net"`（runner 出站
   連入的網址）、`workspace_root = "~/dispatch_workspaces"`、`validation_allowlist = ["pytest", "ruff", "mypy", "make test"]`。
5. **自檢與啟動**：`dispatch-agent --check`（印出：非 root、憑證檔權限、SDK 版本、可連 Server A、Agent Card 探測）→
   `systemctl --user enable --now dispatch-agent`（unit template 隨 wheel 提供）。
6. **驗收**：Studio 伺服器頁顯示「agent 已連線」與 Agent Card（GPU／工具鏈）；開一個 session 跑一回合。

撤銷：Server A 上建 `agent_runner_revoke` 卡並核准 → runner 下次心跳被拒 → 停服務、刪 `agent.env`。
