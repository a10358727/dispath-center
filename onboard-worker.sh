#!/usr/bin/env bash
# Interactive, operator-run bootstrap for an agentless SSH worker.
#
# The remote account password is handled only by ssh-copy-id's terminal prompt.
# This script never accepts a password argument, stores a password, edits
# servers.yaml, installs a Node Agent, or enables a worker.

set -Eeuo pipefail
umask 077

HOST=""
REMOTE_USER=""
SERVER_NAME=""
PORT="22"
KEY_PATH="${HOME}/.ssh/dispatch_worker"
GPU=0
TEMP_PUBLIC=""

info() { printf '• %s\n' "$*"; }
ok() { printf '✓ %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

cleanup() {
    if [[ -n "$TEMP_PUBLIC" && -f "$TEMP_PUBLIC" && ! -L "$TEMP_PUBLIC" ]]; then
        rm -f -- "$TEMP_PUBLIC"
    fi
}
trap cleanup EXIT

usage() {
    cat <<'EOF'
用法：
  ./onboard-worker.sh --host HOST --user USER --name NAME [選項]

必要參數：
  --host HOST       工作機的 Tailscale／私網 IP 或 hostname
  --user USER       工作機上的非 root Linux 帳號
  --name NAME       Dispatch Center 顯示名稱（英數、_、-）

選項：
  --port PORT       SSH port（預設 22）
  --key PATH        Server A 上的專用私鑰（預設 ~/.ssh/dispatch_worker）
  --gpu             另外確認工作機有 nvidia-smi
  -h, --help        顯示說明

流程：
  1. 缺少專用 key 時，在 Server A 建立無 passphrase 的 Ed25519 key。
  2. 執行 ssh-copy-id；密碼只由系統工具在終端機互動讀取。
  3. 用 key-only SSH 唯讀確認 bash、tmux、rsync（GPU 再加 nvidia-smi）。
  4. 印出 disabled 的 YAML 範例，供你到網站建立並核准新增請求。

安全限制：
  * 不接受 --password，也不把密碼寫入參數、環境、檔案或 log。
  * 不修改 servers.yaml，不自動啟用機器，不派工，不安裝 Node Agent。
  * key 只允許放在目前使用者的 ~/.ssh 直接子路徑。
  * 沿用應用既有的 known_hosts=None 政策；只應用於可信 Tailscale／私網。
EOF
}

require_value() {
    local option="$1" value="${2-}"
    [[ -n "$value" && "$value" != -* ]] || die "$option 缺少值"
}

parse_args() {
    while (($# > 0)); do
        case "$1" in
            --host)
                require_value "$1" "${2-}"
                HOST="$2"
                shift 2
                ;;
            --user)
                require_value "$1" "${2-}"
                REMOTE_USER="$2"
                shift 2
                ;;
            --name)
                require_value "$1" "${2-}"
                SERVER_NAME="$2"
                shift 2
                ;;
            --port)
                require_value "$1" "${2-}"
                PORT="$2"
                shift 2
                ;;
            --key)
                require_value "$1" "${2-}"
                KEY_PATH="$2"
                shift 2
                ;;
            --gpu)
                GPU=1
                shift
                ;;
            --password|--password-file|--pass)
                die "拒絕接收密碼；請讓 ssh-copy-id 直接在終端機互動詢問"
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "未知參數：$1（請用 --help）"
                ;;
        esac
    done
}

validate_inputs() {
    [[ -n "$HOST" ]] || die "缺少 --host"
    [[ -n "$REMOTE_USER" ]] || die "缺少 --user"
    [[ -n "$SERVER_NAME" ]] || die "缺少 --name"

    [[ "$HOST" =~ ^[A-Za-z0-9._:-]+$ && "$HOST" != -* && "$HOST" != "0.0.0.0" ]] \
        || die "host 只能是簡單 hostname／IP，且不可為 0.0.0.0"
    [[ "$REMOTE_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] \
        || die "user 必須是小寫 Linux 帳號名稱"
    [[ "$REMOTE_USER" != "root" ]] \
        || die "拒絕 root；請建立專用的非 root 工作帳號"
    [[ "$SERVER_NAME" =~ ^[A-Za-z0-9_-]+$ ]] \
        || die "name 只能包含英數字、底線（_）、連字號（-）"
    [[ "$PORT" =~ ^[0-9]+$ && ${#PORT} -le 5 ]] \
        && ((10#$PORT >= 1 && 10#$PORT <= 65535)) \
        || die "port 必須介於 1-65535"

    if [[ "$KEY_PATH" == "~/"* ]]; then
        KEY_PATH="$HOME/${KEY_PATH:2}"
    fi
    [[ "$KEY_PATH" == /* ]] || die "key 必須是絕對路徑或 ~/.ssh 下的路徑"
    local ssh_dir="$HOME/.ssh" key_leaf=""
    [[ "$KEY_PATH" == "$ssh_dir/"* ]] \
        || die "key 只允許放在 $ssh_dir 底下"
    key_leaf="${KEY_PATH#"$ssh_dir/"}"
    [[ "$key_leaf" =~ ^[A-Za-z0-9._-]+$ && "$key_leaf" != *.pub ]] \
        || die "key 必須是 ~/.ssh 的直接子檔案，且不可指向 .pub"
}

require_local_tools() {
    local tool
    for tool in ssh ssh-keygen ssh-copy-id timeout stat chmod mkdir mktemp rm; do
        command -v "$tool" >/dev/null 2>&1 || die "找不到必要工具：$tool"
    done
}

prepare_ssh_directory() {
    local ssh_dir="$HOME/.ssh"
    if [[ -L "$ssh_dir" ]]; then
        die "拒絕 symlink SSH 目錄：$ssh_dir"
    fi
    mkdir -p -- "$ssh_dir"
    [[ -d "$ssh_dir" && ! -L "$ssh_dir" ]] || die "SSH 目錄不是一般目錄：$ssh_dir"
    [[ "$(stat -c '%u' "$ssh_dir")" == "$(id -u)" ]] \
        || die "SSH 目錄不屬於目前使用者：$ssh_dir"
    chmod 700 -- "$ssh_dir"
}

verify_existing_keypair() {
    local public_path="$KEY_PATH.pub" derived_type="" derived_key=""
    local public_type="" public_key=""
    [[ -f "$KEY_PATH" && ! -L "$KEY_PATH" ]] \
        || die "既有 key 不是一般私鑰檔案：$KEY_PATH"
    [[ -f "$public_path" && ! -L "$public_path" ]] \
        || die "已有私鑰但缺少一般公鑰檔案：$public_path"
    TEMP_PUBLIC="$(mktemp "${TMPDIR:-/tmp}/dispatch-worker-public.XXXXXX")" \
        || die "無法建立暫存檔"
    if ! ssh-keygen -y -P '' -f "$KEY_PATH" >"$TEMP_PUBLIC" 2>/dev/null; then
        die "既有私鑰無效或需要 passphrase；無人值守派工需要專用的無 passphrase key"
    fi
    read -r derived_type derived_key _ <"$TEMP_PUBLIC" || die "無法解析既有私鑰"
    read -r public_type public_key _ <"$public_path" || die "無法解析既有公鑰"
    [[ "$derived_type" == "$public_type" && "$derived_key" == "$public_key" ]] \
        || die "既有私鑰與公鑰不匹配：$KEY_PATH"
    chmod 600 -- "$KEY_PATH"
    chmod 644 -- "$public_path"
    rm -f -- "$TEMP_PUBLIC"
    TEMP_PUBLIC=""
    ok "沿用既有專用 SSH key：$KEY_PATH"
}

ensure_keypair() {
    local public_path="$KEY_PATH.pub"
    if [[ -e "$KEY_PATH" || -e "$public_path" || -L "$KEY_PATH" || -L "$public_path" ]]; then
        verify_existing_keypair
        return
    fi
    info "建立專用 Ed25519 SSH key：$KEY_PATH"
    ssh-keygen -q -t ed25519 -a 64 -N '' -C dispatch-center -f "$KEY_PATH" \
        || die "SSH key 建立失敗"
    [[ -f "$KEY_PATH" && ! -L "$KEY_PATH" && -f "$public_path" && ! -L "$public_path" ]] \
        || die "ssh-keygen 沒有建立完整 keypair"
    chmod 600 -- "$KEY_PATH"
    chmod 644 -- "$public_path"
    ok "專用 SSH key 已建立（私鑰內容不會顯示）"
}

install_public_key() {
    local target="$REMOTE_USER@$HOST"
    info "安裝公鑰到 $target；若尚未安裝，ssh-copy-id 會直接詢問遠端密碼"
    ssh-copy-id \
        -i "$KEY_PATH.pub" \
        -p "$PORT" \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$target" \
        || die "ssh-copy-id 失敗；未保存任何密碼"
    ok "公鑰安裝完成或原本已存在"
}

check_worker() {
    local target="$REMOTE_USER@$HOST" remote_check=""
    local -a ssh_args=(
        -p "$PORT"
        -i "$KEY_PATH"
        -o BatchMode=yes
        -o IdentitiesOnly=yes
        -o StrictHostKeyChecking=no
        -o UserKnownHostsFile=/dev/null
        -o ConnectTimeout=10
        -o ConnectionAttempts=1
    )
    if ((GPU)); then
        remote_check='set -eu
missing=""
for tool in bash tmux rsync nvidia-smi; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
    printf "missing required tools:%s\n" "$missing" >&2
    exit 20
fi
printf "worker prerequisites: ready (GPU)\n"'
    else
        remote_check='set -eu
missing=""
for tool in bash tmux rsync; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
    printf "missing required tools:%s\n" "$missing" >&2
    exit 20
fi
printf "worker prerequisites: ready\n"'
    fi

    info "以 key-only SSH 唯讀檢查工作機（30 秒上限）"
    # Goal 3 Phase B（B3）：工具缺失不再直接失敗——金鑰配置完成後，缺什麼
    # 交給平台的 server_bootstrap 核准流程去驗證與回報（真正的守門是
    # server_add 的 bootstrap-report 閘）。只有 SSH 本身連不上才中止。
    local check_status=0
    timeout --signal=TERM 30s ssh "${ssh_args[@]}" "$target" "$remote_check" \
        || check_status=$?
    if ((check_status == 0)); then
        ok "工作機 SSH 與必要工具檢查通過"
    elif ((check_status == 20)); then
        MISSING_TOOLS=1
        info "SSH 可連線，但缺少部分工具——請改走平台 bootstrap 流程（見下方指引）"
    else
        die "工作機 SSH 連線失敗；請先確認網路、帳號與金鑰"
    fi
}

print_server_config() {
    local key_display="$KEY_PATH" gpu_text="false" tags="cpu"
    if [[ "$KEY_PATH" == "$HOME/"* ]]; then
        key_display="~/${KEY_PATH#"$HOME/"}"
    fi
    if ((GPU)); then
        gpu_text="true"
        tags="gpu, training"
    fi
    cat <<EOF

下一步（Goal 3 Phase B）：金鑰已配置完成。建議先透過平台建立 bootstrap
核准請求（需要 SERVER_BOOTSTRAP_V1_ENABLED=true），由平台以審閱過的固定
腳本驗證/準備使用者層環境並留下報告——系統套件（含 GPU 驅動）仍需操作者
以 root 自行安裝，平台絕不提權：

  curl -X POST http://<dispatch-center>/servers/bootstrap-request \\
    -H 'Content-Type: application/json' -H 'X-Auth-Token: <token>' \\
    -d '{"host":"$HOST","username":"$REMOTE_USER","port":$PORT,
         "key":"$key_display","components":["tmux","rsync","git","python-venv"],
         "gpu":$gpu_text}'

到核准頁批准後，報告會出現在 GET /servers/bootstrap-reports。報告通過後：
到網站「伺服器 → 新增伺服器」，貼入下列值並保持 enabled=false；
先按「測試 SSH」，再建立新增請求並到總覽核准。腳本不會直接修改 servers.yaml。

servers:
  - name: '$SERVER_NAME'
    host: '$HOST'
    user: '$REMOTE_USER'
    key: '$key_display'
    port: $PORT
    gpu: $gpu_text
    idle_gpu_util: 15
    idle_load: 2.0
    tags: [$tags]
    project_roots: ['~/projects']
    dataset_roots: ['~/datasets']
    enabled: false
EOF
}

main() {
    parse_args "$@"
    validate_inputs
    require_local_tools
    prepare_ssh_directory
    ensure_keypair
    install_public_key
    check_worker
    print_server_config
}

main "$@"
