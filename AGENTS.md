# Sol–Luna workflow

`PLAN.md` 是已核准的實作計畫。

## Sol 主代理負責

- 選擇下一個可執行任務
- 判斷任務依賴
- 架構、API、schema、安全及相容性決策
- 處理 Luna 回報的 blocker
- 檢查 git diff
- 執行整合測試
- 最終驗收

## Luna 子代理負責

- 實作 Sol 指定的單一任務
- 修改指定範圍內的檔案
- 撰寫與執行測試
- 回報修改內容及驗證結果

## 執行規則

1. 預設一次只執行一個寫入型 Luna。
2. 不允許多個代理同時修改相同檔案。
3. Luna 回傳 NEEDS_SOL_DECISION 時，由 Sol 做出明確決策。
4. Sol 必須把決策傳回同一個 Luna thread。
5. Luna 回傳 TASK_COMPLETE 後，Sol 必須親自審查 diff。
6. 任務通過 Sol 審查後，才能開始下一個任務。
7. 所有任務完成後，執行完整整合測試。