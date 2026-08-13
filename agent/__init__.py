"""Goal 3 C2：Node Agent 套件（工作機端）。

這個套件是**獨立於 control plane** 的：它只透過 HTTP 講話，不 import
`app.*` 任何模組——工作機上不會、也不該有 control plane 的程式碼或 DB。
唯一共用的是協議常數與 digest 演算法，刻意各自實作（見
`agent.client` 的 `command_digest`），避免在工作機上引入整包相依。

安全姿態（INV-NODE-1）：

- 只發起**出站** HTTPS；agent 不開任何 listener、不綁任何埠；
- 以**非 root** 執行；
- 憑證從環境變數或 0600 檔案讀入，永遠不寫進日誌（只用 redacted 形式）。
"""

#: 套件版本（roadmap Phase 3 要求「versioned Node Agent package」）。agent
#: 每次 poll/心跳都會回報它，control plane 存進 `nodes.agent_version`，讓
#: 操作者看得出哪台跑的是哪版——canary 期間出問題時要能指認版本。
#: 協議有不相容變更時**必須**同時 bump 這個值。
__version__ = "1.0.0"

#: 這個版本實作的協議面（與 control plane 的 `/node-agent/*` 對應）。
#: 缺少其中任何一項的 control plane 都不該被這版 agent 連上。
SUPPORTED_PROTOCOL_OPERATIONS = (
    "probe",
    "poll",
    "ack",
    "heartbeat",
    "terminal",
    "stop-ack",
    "artifacts",
)

# Explicit wire-contract metadata. The control plane has the same constants
# in ``app.node_protocol``; cross-package tests keep the independently
# installable agent honest.
NODE_PROTOCOL_VERSION = "2.0"
NODE_PROTOCOL_VERSION_HEADER = "X-Node-Protocol-Version"

__all__ = [
    "client",
    "runner",
    "__version__",
    "SUPPORTED_PROTOCOL_OPERATIONS",
    "NODE_PROTOCOL_VERSION",
    "NODE_PROTOCOL_VERSION_HEADER",
]
