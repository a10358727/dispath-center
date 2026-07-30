#!/usr/bin/env bash
# 備份 dispatch-center 的持久狀態(PLAN.md Phase 0 backup 安全網)。
#
# 用法:
#   deploy/backup.sh [--with-data] [備份根目錄]
#
#   --with-data   連同 datasets/ 與 results/ 一起備份(可能很大,預設不含;
#                 大目錄日常建議另外用 rsync 增量備份,見 README)。
#   備份根目錄    預設 ./backups;實際輸出到 <根目錄>/<UTC 時間戳>/。
#
# 環境變數:
#   LOCAL_HOME_DIR  執行期資料所在目錄(同 .env,預設 .)。
#
# 備份內容:
#   jobqueue.db      用 sqlite3 online backup(服務運行中也一致)
#   audit.jsonl      append-only 稽核流水
#   servers.yaml / auto_approve.yaml / .env   實際執行設定(.env 含密鑰,
#                    備份目錄權限設 700)
#   git/             專案中央 hub(bare repos)
#
# 不備份:hub_bundles/(可重建的暫存)、.venv、程式碼(進 git 版控)。
set -euo pipefail

cd "$(dirname "$0")/.."
HOME_DIR="${LOCAL_HOME_DIR:-.}"

WITH_DATA=0
if [ "${1:-}" = "--with-data" ]; then
    WITH_DATA=1
    shift
fi
if [ "$#" -eq 0 ] && [ -z "${BACKUP_ROOT:-}" ] \
    && [ "${REQUIRE_BACKUP_ROOT:-false}" = "true" ]; then
    echo "錯誤:自動備份要求明確設定 BACKUP_ROOT（應位於 Server A 之外）"
    exit 1
fi
BACKUP_ROOT="${1:-${BACKUP_ROOT:-./backups}}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

[ -f "$HOME_DIR/jobqueue.db" ] || {
    echo "錯誤:$HOME_DIR/jobqueue.db 不存在；拒絕產生不可還原的空備份"
    exit 1
}

mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"
# Concurrent/manual backups within the same UTC second must never overwrite or
# merge into one another. The timestamp stays human-readable; mktemp supplies
# the collision-proof publication identity.
DEST="$(mktemp -d "$BACKUP_ROOT/${STAMP}.XXXXXX")"
chmod 700 "$DEST"

# SQLite:online backup 保證一致快照;服務運行中執行也安全。使用 Python
# stdlib，避免 release/restore gate 額外依賴系統 sqlite3 CLI。
python3 scripts/sqlite_online_backup.py \
    "$HOME_DIR/jobqueue.db" "$DEST/jobqueue.db"
echo "ok  jobqueue.db"

for f in audit.jsonl servers.yaml auto_approve.yaml .env; do
    if [ -f "$HOME_DIR/$f" ]; then
        cp -p "$HOME_DIR/$f" "$DEST/$f"
        echo "ok  $f"
    else
        echo "skip $f(不存在)"
    fi
done

# 中央 hub(bare repos):tar 保留權限;bare repo 沒有 working tree,
# 服務運行中打包的風險視同一般 git 伺服器備份。
if [ -d "$HOME_DIR/git" ]; then
    tar -czf "$DEST/hub-git.tar.gz" -C "$HOME_DIR" git
    python3 scripts/safe_extract_backup.py \
        --validate-only "$DEST/hub-git.tar.gz" >/dev/null
    echo "ok  git/(hub)"
else
    echo "skip git/(不存在)"
fi

if [ "$WITH_DATA" = "1" ]; then
    for d in datasets results; do
        if [ -d "$HOME_DIR/$d" ]; then
            tar -czf "$DEST/$d.tar.gz" -C "$HOME_DIR" "$d"
            python3 scripts/safe_extract_backup.py \
                --validate-only "$DEST/$d.tar.gz" >/dev/null
            echo "ok  $d/"
        else
            echo "skip $d/(不存在)"
        fi
    done
fi

# 對每個實際產物做 checksum。MANIFEST 再 pin 住整份 checksum 清單；
# restore 會在移動任何現場狀態之前驗證兩層 digest。
CHECKSUM_TMP="$DEST/.CHECKSUMS.sha256.tmp"
: > "$CHECKSUM_TMP"
for entry in \
    jobqueue.db audit.jsonl servers.yaml auto_approve.yaml .env \
    hub-git.tar.gz datasets.tar.gz results.tar.gz
do
    if [ -f "$DEST/$entry" ]; then
        (
            cd "$DEST"
            sha256sum "$entry"
        ) >> "$CHECKSUM_TMP"
    fi
done
mv "$CHECKSUM_TMP" "$DEST/CHECKSUMS.sha256"
CHECKSUMS_SHA256="$(sha256sum "$DEST/CHECKSUMS.sha256" | awk '{print $1}')"

# 記錄本次備份的來源與內容,restore 時核對。
{
    echo "created_at=$STAMP"
    echo "home_dir=$(cd "$HOME_DIR" && pwd)"
    echo "with_data=$WITH_DATA"
    echo "git_head=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "checksums_sha256=$CHECKSUMS_SHA256"
} > "$DEST/MANIFEST"

echo "備份完成:$DEST"
