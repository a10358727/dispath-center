# Runner agent 安裝（dispatch-agent）— Phase 1a

> 狀態：Phase 1a 程式碼已落地（`dispatch_agent/`、gateway、Studio 骨架）；本檔是 pilot runner
> （`worker_5090_106`，使用者 `bgab141`）的實際安裝步驟。全部以該使用者身分、**非 root**（INV-AGENT-1）。

## 0. Server A 端（一次）

`.env` 加上（pilot 已有 `AGENT_SESSION_V1_ENABLED=true`、`CODEX_RUNNER_SERVER`）：

```
AGENT_RUNTIME_V3_ENABLED=true
ASSISTANT_TOOLS_V1_ENABLED=true
ASSISTANT_TOOLS_DISPATCH_BASE_URL=https://formosa-desktop.tail552541.ts.net
```

`ASSISTANT_TOOLS_*` 讓 session 拿到平台工具（MCP `request_run`／`request_experiment`／查詢），
沒設也能開 session，只是 agent 沒有平台工具。重啟服務後 Studio 在
`https://formosa-desktop.tail552541.ts.net/static/studio/`。

wheel：CI 會建；本機 `python -m build --no-isolation --outdir dist dispatch_agent`
（或直接用 `dist/dispatch_agent-0.1.0-py3-none-any.whl`）。

## 1. runner 端

```bash
# Server A → runner
scp dist/dispatch_agent-0.1.0-py3-none-any.whl bgab141@140.130.21.106:~/

# runner（bgab141）
python3 -m pip install --user ~/dispatch_agent-0.1.0-py3-none-any.whl   # 帶入 claude-agent-sdk、websockets、httpx
python3 -c "import claude_agent_sdk, mcp, httpx; print('sdk ok')"       # mcp 缺就 pip install --user mcp
mkdir -p ~/.config/dispatch-agent ~/dispatch_workspaces

# Claude 訂閱憑證（只在 runner；Server A 永不持有）
claude setup-token                                # 產生一年期 OAuth token
install -m 600 /dev/null ~/.config/dispatch-agent/claude.env
echo 'CLAUDE_CODE_OAUTH_TOKEN=<貼上 token>' > ~/.config/dispatch-agent/claude.env

# 非秘密設定
cat > ~/.config/dispatch-agent/config.json <<'JSON'
{
  "server_url": "https://formosa-desktop.tail552541.ts.net",
  "runner_name": "worker_5090_106",
  "workspace_root": "~/dispatch_workspaces"
}
JSON
```

可選鍵：`validation_allowlist`（預設 `pytest`、`ruff`、`mypy`、`python -m pytest`、`make test`、`npm test`、
`git status`、`git diff`、`git log` 的前綴；其餘 Bash 一律彈提示）、`max_turns`（50）、`max_budget_usd`、
`heartbeat_sec`（15）、`permission_timeout_sec`（300，逾時＝拒絕）、`model`、`runner_python`（`python3`）、
`extra_mcp_servers`（P2-5，operator 自管的額外 stdio MCP server；名稱小寫、不可叫 `dispatch`，例：
`{"docs": {"command": "npx", "args": ["-y", "some-mcp"]}}`——這些工具是 `mcp__<name>__*`，每次呼叫都會彈權限提示）。

Phase 2 補充：Studio 開 session 時可選 模型／effort／thinking／權限模式（`default`＝逐條提示、
`acceptEdits`＝工作區編輯不問、`plan`＝先規劃；`bypassPermissions` 永遠不存在）；session 中可即時切模型
與權限模式；工作區的 `CLAUDE.md` 會進 system prompt，`.claude/skills`／`.claude/commands` 以「消毒後外掛」
載入（`allowed-tools`／hooks／`!`\`\`\` 前處理一律剝除）；可貼圖（≤4 張、各 ≤3MB）、`@` 引用檔案、`/` 呼叫
skills 與內建指令（`/compact`、`/context`）；「分支」可從既有對話 fork 新 session。

## 2. 登錄（Studio）

Studio →「伺服器與硬體」→「登錄 runner agent」：伺服器名稱 `worker_5090_106` → 建立核准卡 → 核准 →
畫面**只顯示一次** `dar_…` 憑證：

```bash
install -m 600 /dev/null ~/.config/dispatch-agent/agent.env
echo 'DISPATCH_AGENT_CREDENTIAL=dar_<貼上>' > ~/.config/dispatch-agent/agent.env
```

## 3. 自檢與啟動

```bash
~/.local/bin/dispatch-agent --check     # 每行 ok：config／non-root／claude token／claude-agent-sdk／agent card；任一 FAIL 就停
mkdir -p ~/.config/systemd/user
cp "$(python3 -c 'import dispatch_agent, os; print(os.path.dirname(dispatch_agent.__file__))')/dispatch-agent.service" ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now dispatch-agent
loginctl enable-linger bgab141          # 讓 user unit 不因登出而停（需一次）
journalctl --user -u dispatch-agent -f
```

Studio 伺服器頁應顯示「agent 已連線」。憑證與 token 只在上述兩個 0600 檔；unit 不用 `Environment=`（`--check`
會把環境裡的 `DISPATCH_AGENT_*` 視為 FAIL）。

## 4. 驗收回合（Phase 1 完成定義）

1. Studio → 專案 → expdemo → ＋新 session（版本＋runner）→ 核准卡就地核准 → 啟動 session。
2. 「把 README 加一段」→ 串流文字、Edit 工具卡、Changes 面板看到 diff。
3. 「跑 pytest」→ allowlist 直接執行。
4. 「pip install rich」→ 權限提示 → 允許 → 執行；再來一次 → 拒絕 → agent 收到拒絕。
5. 「幫我建一個實驗」→ agent 只會經 MCP `request_experiment` 建卡；卡在核准匣就地決定。agent 永遠不能自己核准。
6. Server A 重啟後事件仍在、串流續接；停 runner 服務 → session 變 `unknown`（不判失敗）。

## 撤銷

Studio／API 建 `agent_runner_revoke` 卡並核准 → runner 下次連線被拒（1008）→ `systemctl --user disable --now dispatch-agent`
→ 刪 `agent.env`。工作區在 `~/dispatch_workspaces/<session>/`，刪不刪由人決定。
