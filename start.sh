#!/usr/bin/env bash
# AI 訓練調度中心一鍵啟動腳本
#
# 用法：
#   ./start.sh            啟動全部（調度中心 + MCP bridge + cloudflared 隧道）
#   ./start.sh stop       全部停止
#   ./start.sh restart      全部重啟（含隧道，網址會變、要回 ChatGPT 改 URL）
#   ./start.sh restart-app  只重啟調度中心+bridge（隧道不動，網址不變）←平常用這個
#   ./start.sh status     看目前狀態
#   ./start.sh url        取得 cloudflared 隧道網址（含 ChatGPT connector 完整 URL）
#   ./start.sh logs       進 tmux 看即時 log（離開按 Ctrl+B 再按 D，服務不會停）
#
# 原理：三個服務各跑在同一個 tmux session（dispatch-center）的三個視窗裡，
# 關掉這個 SSH/終端機視窗服務照跑。要長期常駐、開機自啟，還是建議照
# README §6.6 裝 systemd；這個腳本是開發期的方便工具。

set -u
cd "$(dirname "$0")"

SESSION="dispatch-center"
VENV=".venv/bin/activate"

err()  { echo "✗ $*" >&2; }
info() { echo "• $*"; }
ok()   { echo "✓ $*"; }

# 讀 .env 裡的單一變數值（不 source 整個檔，避免特殊字元出事）
env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-; }

check_prereqs() {
    [ -f .env ] || { err ".env 不存在——先 cp .env.example .env 並填好設定"; exit 1; }
    [ -f "$VENV" ] || { err ".venv 不存在——先照 README §1 建立虛擬環境"; exit 1; }
    command -v tmux >/dev/null || { err "tmux 未安裝：sudo apt install tmux"; exit 1; }
}

port_of() {  # port_of VAR 預設值
    local v; v=$(env_get "$1"); echo "${v:-$2}"
}

is_listening() { ss -tln 2>/dev/null | grep -q ":$1\b"; }

do_start() {
    check_prereqs
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        info "session 已存在，不重複啟動。目前狀態："
        do_status
        exit 0
    fi

    local api_port bridge_port secret
    api_port=$(port_of API_PORT 8000)
    bridge_port=$(port_of MCP_BRIDGE_PORT 8890)
    secret=$(env_get MCP_BRIDGE_PATH_SECRET)

    # 視窗 1：調度中心本體
    if is_listening "$api_port"; then
        err "port $api_port 已有服務在聽——調度中心可能已經在別的終端機跑著。"
        err "先把舊的停掉（或用 ./start.sh status 確認），避免起兩份。"
        exit 1
    fi
    tmux new-session -d -s "$SESSION" -n main \
        "source $VENV && python -m app.main; echo '[main 已結束，按任意鍵關閉]'; read -r -n1"
    ok "調度中心啟動中（port $api_port，tmux 視窗 main）"

    # 視窗 2：MCP bridge（有設定 PATH_SECRET 才啟動）
    if [ -n "${secret:-}" ]; then
        if is_listening "$bridge_port"; then
            info "port $bridge_port 已有服務——跳過 bridge（可能已在跑）"
        else
            tmux new-window -t "$SESSION" -n bridge \
                "source $VENV && python -m app.mcp_bridge; echo '[bridge 已結束，按任意鍵關閉]'; read -r -n1"
            ok "MCP bridge 啟動中（port $bridge_port，tmux 視窗 bridge）"
        fi
    else
        info "MCP_BRIDGE_PATH_SECRET 未設定，跳過 MCP bridge（不影響調度中心）"
    fi

    # 視窗 3：cloudflared 隧道（有裝且 bridge 有起才啟動）
    if [ -n "${secret:-}" ] && command -v cloudflared >/dev/null; then
        tmux new-window -t "$SESSION" -n tunnel \
            "cloudflared tunnel --url http://127.0.0.1:$bridge_port --http-host-header 127.0.0.1:$bridge_port; echo '[tunnel 已結束，按任意鍵關閉]'; read -r -n1"
        ok "cloudflared 隧道啟動中（tmux 視窗 tunnel）"
        info "隧道網址每次重啟都會變——看網址：./start.sh logs 切到 tunnel 視窗，"
        info "然後回 ChatGPT connector 設定更新 URL。"
    elif [ -n "${secret:-}" ]; then
        info "cloudflared 未安裝，跳過隧道（ChatGPT 串接需要它，見 README §10.3）"
    fi

    echo
    ok "完成。網頁：http://127.0.0.1:$api_port/（遠端請走 VS Code 轉發或 SSH 隧道）"
    info "看 log：./start.sh logs　　停止：./start.sh stop"
}

do_stop() {
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux kill-session -t "$SESSION"
        ok "已停止（tmux session 已關閉）"
    else
        info "沒有在跑（找不到 tmux session：$SESSION）"
    fi
}

do_restart_app() {
    # 只重啟調度中心與 bridge（改了程式碼/.env 之後用），**隧道不動**——
    # quick tunnel 的網址不會變，不用回 ChatGPT 改 connector URL。
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        err "服務沒有在跑，直接 ./start.sh 即可"; exit 1
    fi
    local api_port bridge_port secret
    api_port=$(port_of API_PORT 8000)
    bridge_port=$(port_of MCP_BRIDGE_PORT 8890)
    secret=$(env_get MCP_BRIDGE_PATH_SECRET)

    tmux kill-window -t "$SESSION:main" 2>/dev/null
    tmux kill-window -t "$SESSION:bridge" 2>/dev/null
    sleep 1
    tmux new-window -t "$SESSION" -n main \
        "source $VENV && python -m app.main; echo '[main 已結束，按任意鍵關閉]'; read -r -n1"
    ok "調度中心重啟中（port $api_port）"
    if [ -n "${secret:-}" ]; then
        tmux new-window -t "$SESSION" -n bridge \
            "source $VENV && python -m app.mcp_bridge; echo '[bridge 已結束，按任意鍵關閉]'; read -r -n1"
        ok "MCP bridge 重啟中（port $bridge_port）"
    fi
    ok "隧道未動，ChatGPT connector URL 不用改"
}

do_url() {
    # 從 tunnel 視窗的輸出裡撈 trycloudflare 網址（cloudflared 啟動時印的那個框）
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        err "服務沒有在跑（./start.sh 先啟動）"; exit 1
    fi
    local url secret
    url=$(tmux capture-pane -t "$SESSION:tunnel" -p -S -300 2>/dev/null \
          | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)
    if [ -z "$url" ]; then
        err "還沒抓到隧道網址——隧道可能還在啟動（等幾秒再試），"
        err "或 tunnel 視窗不存在（cloudflared 沒起來，看 ./start.sh status）"
        exit 1
    fi
    secret=$(env_get MCP_BRIDGE_PATH_SECRET)
    ok "隧道網址：$url"
    if [ -n "${secret:-}" ]; then
        echo
        echo "ChatGPT connector 要填的完整 URL（含路徑機密，複製這行）："
        echo "  $url/mcp-$secret"
    fi
}

do_status() {
    local api_port bridge_port
    api_port=$(port_of API_PORT 8000)
    bridge_port=$(port_of MCP_BRIDGE_PORT 8890)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        ok "tmux session 存在，視窗：$(tmux list-windows -t "$SESSION" -F '#W' | tr '\n' ' ')"
    else
        info "tmux session 不存在"
    fi
    is_listening "$api_port"    && ok "調度中心：port $api_port 在聽"    || err "調度中心：port $api_port 沒有服務"
    is_listening "$bridge_port" && ok "MCP bridge：port $bridge_port 在聽" || info "MCP bridge：port $bridge_port 沒有服務"
    pgrep -f "cloudflared tunnel" >/dev/null && ok "cloudflared：執行中" || info "cloudflared：未執行"
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; sleep 1; do_start ;;
    status)      do_status ;;
    url)         do_url ;;
    restart-app) do_restart_app ;;
    logs)        exec tmux attach -t "$SESSION" ;;
    *) echo "用法: $0 [start|stop|restart|restart-app|status|logs|url]"; exit 1 ;;
esac
