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
echo "== 備份資訊 =="
cat "$SRC/MANIFEST"
echo
echo "!! 請確認 dispatch-center 服務已停止(systemctl stop dispatch-center)"
read -r -p "繼續還原到 $HOME_DIR ?(yes/N) " ans
[ "$ans" = "yes" ] || { echo "已取消"; exit 1; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DISPLACED="$HOME_DIR/restore-displaced-$STAMP"
mkdir -p "$DISPLACED"

displace() {
    # 現場已有同名檔/目錄時先搬走,不覆蓋刪除。
    local target="$1"
    if [ -e "$target" ]; then
        mv "$target" "$DISPLACED/"
        echo "  現場檔案已搬到 $DISPLACED/$(basename "$target")"
    fi
}

for f in jobqueue.db audit.jsonl servers.yaml auto_approve.yaml .env; do
    if [ -f "$SRC/$f" ]; then
        displace "$HOME_DIR/$f"
        cp -p "$SRC/$f" "$HOME_DIR/$f"
        echo "ok  $f"
    fi
done

if [ -f "$SRC/hub-git.tar.gz" ]; then
    displace "$HOME_DIR/git"
    tar -xzf "$SRC/hub-git.tar.gz" -C "$HOME_DIR"
    echo "ok  git/(hub)"
fi

for d in datasets results; do
    if [ -f "$SRC/$d.tar.gz" ]; then
        displace "$HOME_DIR/$d"
        tar -xzf "$SRC/$d.tar.gz" -C "$HOME_DIR"
        echo "ok  $d/"
    fi
done

echo "還原完成。原現場檔案保留在 $DISPLACED/,確認無誤後可自行刪除。"
echo "重新啟動服務前請先核對 servers.yaml 與 .env 是否為預期版本。"
