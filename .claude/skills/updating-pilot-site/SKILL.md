---
name: updating-pilot-site
description: Use when 使用者要求更新網站/部署新版本/rollback 運行中的站台，或提到 pilot-run、dispatch-center-web 服務重啟、換版。
---

# Updating the Pilot Site

運行架構：`/home/aied/dispath-center`＝開發 repo；`/home/aied/pilot-run`＝運行
worktree（資料檔 `.env`/`servers.yaml`/`jobqueue.db`/`audit.jsonl`/`results/`
為未追蹤檔案，checkout 不會碰它們）；服務＝systemd user unit
`dispatch-center-web`（port 8000，OIDC，Tailscale 對外）。

## 更新四步（依序，不可跳過測試）

```bash
cd /home/aied/dispath-center
make test                                   # 1. 必須全綠（平行，約 3 分鐘；失敗要重現順序時用 make test-serial）
git add -A && git commit -m "..."           # 2. commit（已綠才 commit）
git -C /home/aied/pilot-run checkout <commit>   # 3. 運行目錄切版
systemctl --user restart dispatch-center-web    # 4. 重啟（秒級中斷）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/   # 200＝成功
```

依賴有變（`requirements.lock` 改動）時，第 3 步後加：
`.venv/bin/python -m pip install --require-hashes -r requirements.lock`
（兩目錄共用同一 venv）。

Studio（`studio/` 有改動，或 pilot-run 尚無 `static/studio/`）時，第 3 步後加：

```bash
cd /home/aied/dispath-center/studio && npm ci --no-audit --no-fund && npm run build   # 產物在 static/studio/（gitignored）
rsync -a --delete /home/aied/dispath-center/static/studio/ /home/aied/pilot-run/static/studio/
```

Studio 網址：`https://<site>/static/studio/`（走既有的 `/static/` 公開前綴，沒有新增驗證豁免；
未登入只會看到登入卡）。

## Rollback

`git -C /home/aied/pilot-run checkout <前一 commit>` → 重啟。資料不受影響；
migration 是 additive，舊 code 讀新 DB 相容。

## 紅線

- 測試沒全綠不部署；不直接在 pilot-run 改程式碼（一律經開發 repo commit）。
- 永不刪改 `jobqueue.db`/`audit.jsonl`/`results/`；重啟不影響工作機上執行中
  的任務（哨兵協議＋reconcile 會收斂）。
- log：`journalctl --user -u dispatch-center-web -f`。
