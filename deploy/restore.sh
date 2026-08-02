#!/usr/bin/env bash
# 從 deploy/backup.sh 產生的備份目錄還原 dispatch-center 持久狀態。
#
# 用法:
#   deploy/restore.sh <備份目錄>          # 例:deploy/restore.sh backups/20260712T030000Z
#
# 環境變數:
#   LOCAL_HOME_DIR  還原目的地(同 .env,預設 .)。
#
# 安全規則:
#   1. 必須先停止服務(systemctl stop dispatch-center)再還原,腳本只提醒
#      不強制——由操作者確認。
#   2. 還原前把現場既有檔案搬到 <目的地>/restore-displaced-<時間戳>/,
#      不直接覆蓋刪除;確認無誤後再自行清除。
set -euo pipefail

cd "$(dirname "$0")/.."
HOME_DIR="${LOCAL_HOME_DIR:-.}"
SRC="${1:?用法:deploy/restore.sh <備份目錄>}"

[ -f "$SRC/MANIFEST" ] || { echo "錯誤:$SRC 沒有 MANIFEST,不是 backup.sh 產生的備份"; exit 1; }
[ -f "$SRC/CHECKSUMS.sha256" ] || {
    echo "錯誤:$SRC 沒有 CHECKSUMS.sha256,無法驗證備份完整性"
    exit 1
}

# 驗證一定發生在 prompt 與 displace 之前；損毀或被替換的備份不能碰現場。
EXPECTED_CHECKSUMS_SHA256="$(
    sed -n 's/^checksums_sha256=//p' "$SRC/MANIFEST" | tail -n 1
)"
[ -n "$EXPECTED_CHECKSUMS_SHA256" ] || {
    echo "錯誤:MANIFEST 沒有 checksums_sha256"
    exit 1
}
ACTUAL_CHECKSUMS_SHA256="$(sha256sum "$SRC/CHECKSUMS.sha256" | awk '{print $1}')"
[ "$EXPECTED_CHECKSUMS_SHA256" = "$ACTUAL_CHECKSUMS_SHA256" ] || {
    echo "錯誤:CHECKSUMS.sha256 與 MANIFEST 不符"
    exit 1
}
(
    cd "$SRC"
    sha256sum --check --strict CHECKSUMS.sha256
) || {
    echo "錯誤:備份內容 checksum 驗證失敗"
    exit 1
}

echo "== 備份資訊 =="
cat "$SRC/MANIFEST"
echo

[ -d "$HOME_DIR" ] || {
    echo "錯誤:還原目的地 $HOME_DIR 不存在"
    exit 1
}

# Materialize the entire candidate into a private staging directory before
# prompting or moving any current state. The extractor rejects traversal,
# links/devices and duplicate paths; a bad archive therefore cannot partially
# replace the live tree.
STAGING="$(mktemp -d "$HOME_DIR/.restore-staging.XXXXXX")"
chmod 700 "$STAGING"
cleanup_staging() {
    rm -rf -- "$STAGING"
}
trap cleanup_staging EXIT

for f in jobqueue.db audit.jsonl servers.yaml auto_approve.yaml .env; do
    if [ -f "$SRC/$f" ]; then
        cp -p "$SRC/$f" "$STAGING/$f"
    fi
done
if [ -f "$SRC/hub-git.tar.gz" ]; then
    python3 scripts/safe_extract_backup.py \
        "$SRC/hub-git.tar.gz" "$STAGING" >/dev/null
fi
for d in datasets results; do
    if [ -f "$SRC/$d.tar.gz" ]; then
        python3 scripts/safe_extract_backup.py \
            "$SRC/$d.tar.gz" "$STAGING" >/dev/null
    fi
done

echo "!! 請確認 dispatch-center 服務已停止(systemctl stop dispatch-center)"
read -r -p "繼續還原到 $HOME_DIR ?(yes/N) " ans
[ "$ans" = "yes" ] || { echo "已取消"; exit 1; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DISPLACED="$(mktemp -d "$HOME_DIR/restore-displaced-${STAMP}.XXXXXX")"
chmod 700 "$DISPLACED"

displace() {
    # 現場已有同名檔/目錄時先搬走,不覆蓋刪除。
    local target="$1"
    if [ -e "$target" ]; then
        mv "$target" "$DISPLACED/"
        echo "  現場檔案已搬到 $DISPLACED/$(basename "$target")"
    fi
}

for f in jobqueue.db audit.jsonl servers.yaml auto_approve.yaml .env; do
    if [ -f "$STAGING/$f" ]; then
        displace "$HOME_DIR/$f"
        mv "$STAGING/$f" "$HOME_DIR/$f"
        echo "ok  $f"
    fi
done

if [ -d "$STAGING/git" ]; then
    displace "$HOME_DIR/git"
    mv "$STAGING/git" "$HOME_DIR/git"
    echo "ok  git/(hub)"
fi

for d in datasets results; do
    if [ -d "$STAGING/$d" ]; then
        displace "$HOME_DIR/$d"
        mv "$STAGING/$d" "$HOME_DIR/$d"
        echo "ok  $d/"
    fi
done

echo "還原完成。原現場檔案保留在 $DISPLACED/,確認無誤後可自行刪除。"
echo "重新啟動服務前請先核對 servers.yaml 與 .env 是否為預期版本。"
