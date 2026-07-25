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

__all__ = ["client", "runner"]
