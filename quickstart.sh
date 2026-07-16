#!/usr/bin/env bash
# Dispatch Center safe one-click launcher (local/development use).
#
# This launcher manages only the FastAPI control-plane process.  It never
# starts cloudflared, the MCP bridge, or a worker process.  Once app.main is
# running, its normal monitor may probe servers already present in config.
# Production deployments should continue to use deploy/dispatch-center.service.

set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT_DIR"

BOOTSTRAP_PYTHON=""
VENV_DIR="${DISPATCH_VENV_DIR:-$ROOT_DIR/.venv}"
VENV_PYTHON="$VENV_DIR/bin/python"
ENV_FILE="$ROOT_DIR/.env"
SERVERS_FILE="$ROOT_DIR/servers.yaml"
REQUIREMENTS_FILE="$ROOT_DIR/requirements.txt"
REQUIREMENTS_STAMP="$VENV_DIR/.dispatch-requirements.sha256"
RUNTIME_DIR="${DISPATCH_RUNTIME_DIR:-$ROOT_DIR/.runtime/dispatch-center}"
PID_FILE="$RUNTIME_DIR/app.pid"
ENDPOINT_FILE="$RUNTIME_DIR/app.endpoint"
LOG_FILE="$RUNTIME_DIR/app.log"
LOCK_FILE="$RUNTIME_DIR/manager.lock"
START_TIMEOUT_SEC="${DISPATCH_START_TIMEOUT_SEC:-20}"

ACTION="start"
FORCE_DEPS=0
SKIP_DEPS=0
FOLLOW_LOGS=0
LOCK_HELD=0
LOCK_FD=""
BACKGROUND_STARTING=0
BACKGROUND_CHILD_PID=""
BACKGROUND_CHILD_STARTTIME=""

API_HOST_VALUE=""
API_PORT_VALUE=""
AUTH_MODE_VALUE=""
BIND_SCOPE_VALUE=""
MANAGED_PID=""
MANAGED_STARTTIME=""
MANAGED_BOOT_ID=""

info() { printf '• %s\n' "$*"; }
ok() { printf '✓ %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：
  ./quickstart.sh                         一鍵安裝/更新依賴並在背景啟動
  ./quickstart.sh setup                   只準備 .env、servers.yaml、venv 與依賴
  ./quickstart.sh foreground              前景啟動，Ctrl+C 停止
  ./quickstart.sh stop                    停止本腳本管理的服務
  ./quickstart.sh restart                 重新啟動
  ./quickstart.sh status                  顯示 PID、URL 與健康狀態
  ./quickstart.sh logs [--follow]         顯示（或持續追蹤）應用 log
  ./quickstart.sh url                     只顯示本機 listener URL

選項：
  --force-deps   即使 requirements.txt 未變也重新安裝依賴
  --skip-deps    不安裝依賴；要求指定 venv 已經可用
  -h, --help     顯示本說明

安全行為：
  * 不 source/eval .env，也不顯示任何 token、cookie 或 secret。
  * 只允許 loopback、RFC1918、link-local 或 Tailscale 私網 bind。
  * 未啟用 AUTH_TOKEN/OIDC 的 open-development 模式只准 loopback。
  * 只啟動 app.main；不會開 MCP、cloudflared 或 worker process。
  * 新建的空 servers.yaml 不探測主機；既有節點仍由 app monitor 正常探測。
EOF
}

parse_args() {
    if (($# > 0)) && [[ "$1" != -* ]]; then
        ACTION="$1"
        shift
    fi
    while (($# > 0)); do
        case "$1" in
            --force-deps) FORCE_DEPS=1 ;;
            --skip-deps) SKIP_DEPS=1 ;;
            --follow) FOLLOW_LOGS=1 ;;
            -h|--help) usage; exit 0 ;;
            *) die "未知參數：$1（請用 --help）" ;;
        esac
        shift
    done
    if ((FORCE_DEPS && SKIP_DEPS)); then
        die "--force-deps 與 --skip-deps 不能同時使用"
    fi
    [[ "$START_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] \
        && ((START_TIMEOUT_SEC <= 300)) \
        || die "DISPATCH_START_TIMEOUT_SEC 必須是 1..300 的整數"
}

prepare_runtime_dir() {
    reject_symlink_components "$RUNTIME_DIR" \
        || die "拒絕含 symlink 的 runtime directory：$RUNTIME_DIR"
    mkdir -p "$RUNTIME_DIR"
    [[ -d "$RUNTIME_DIR" && ! -L "$RUNTIME_DIR" ]] \
        || die "runtime path 不是一般目錄：$RUNTIME_DIR"
    [[ "$(stat -c '%u' "$RUNTIME_DIR")" == "$(id -u)" ]] \
        || die "runtime directory 不屬於目前使用者：$RUNTIME_DIR"
    chmod 700 "$RUNTIME_DIR"
    local path
    for path in "$PID_FILE" "$ENDPOINT_FILE" "$LOG_FILE" "$LOG_FILE.1" "$LOCK_FILE"; do
        [[ ! -L "$path" ]] || die "拒絕 symlink runtime metadata：$path"
        [[ ! -e "$path" || -f "$path" ]] \
            || die "runtime metadata 必須是一般檔案：$path"
    done
}

reject_symlink_components() {
    "$BOOTSTRAP_PYTHON" - "$1" <<'PY'
from pathlib import Path
import os
import sys

path = Path(os.path.abspath(sys.argv[1]))
current = Path(path.anchor)
for part in path.parts[1:]:
    current /= part
    if current.is_symlink():
        raise SystemExit(1)
    if not current.exists():
        break
PY
}

release_lock() {
    if ((LOCK_HELD)); then
        exec {LOCK_FD}>&-
        LOCK_HELD=0
        LOCK_FD=""
    fi
}

acquire_lock() {
    local fd_identity="" path_identity=""
    prepare_runtime_dir
    exec {LOCK_FD}>> "$LOCK_FILE" || die "無法開啟 manager lock：$LOCK_FILE"
    chmod 600 "$LOCK_FILE"
    fd_identity="$(stat -Lc '%d:%i:%u:%F' "/proc/$$/fd/$LOCK_FD")" \
        || die "無法驗證 manager lock file descriptor"
    path_identity="$(stat -Lc '%d:%i:%u:%F' "$LOCK_FILE")" \
        || die "無法驗證 manager lock path"
    if [[ "$fd_identity" != "$path_identity" ]]; then
        exec {LOCK_FD}>&-
        LOCK_FD=""
        die "manager lock path 在開啟期間改變；拒絕繼續"
    fi
    if ! "$BOOTSTRAP_PYTHON" - "$LOCK_FD" <<'PY'
import fcntl
import sys

try:
    fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except (BlockingIOError, OSError, ValueError):
    raise SystemExit(1)
PY
    then
        exec {LOCK_FD}>&-
        LOCK_FD=""
        die "另一個 quickstart 管理命令正在執行"
    fi
    LOCK_HELD=1
    trap release_lock EXIT
}

python_supports_launcher() {
    local candidate="$1"
    [[ -x "$candidate" && ! -d "$candidate" ]] || return 1
    "$candidate" - <<'PY' >/dev/null 2>&1
from pathlib import Path
import os
import signal
import sys

if not sys.platform.startswith("linux"):
    raise SystemExit(1)
required_proc = (
    Path("/proc/self/stat"),
    Path("/proc/self/cmdline"),
    Path("/proc/self/cwd"),
    Path("/proc/sys/kernel/random/boot_id"),
)
if not all(path.exists() for path in required_proc):
    raise SystemExit(1)
if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
    raise SystemExit(1)
try:
    pidfd = os.pidfd_open(os.getpid())
except OSError:
    raise SystemExit(1)
else:
    os.close(pidfd)
PY
}

select_bootstrap_python() {
    local configured="${PYTHON_BIN:-}" candidate="" resolved=""
    local -a candidates=(
        python3
        /usr/bin/python3
        /usr/local/bin/python3
        python3.13
        python3.12
        python3.11
        python3.10
    )

    if [[ -n "$configured" ]]; then
        resolved="$(command -v -- "$configured" 2>/dev/null || true)"
        [[ -n "$resolved" ]] \
            || die "PYTHON_BIN 指定的 Python 不存在：$configured"
        python_supports_launcher "$resolved" \
            || die "PYTHON_BIN 指定的 Python 不符合需求（Python 3.10+、Linux /proc 與 pidfd）：$configured"
        BOOTSTRAP_PYTHON="$resolved"
        return
    fi

    for candidate in "${candidates[@]}"; do
        resolved="$(command -v -- "$candidate" 2>/dev/null || true)"
        [[ -n "$resolved" ]] || continue
        if python_supports_launcher "$resolved"; then
            BOOTSTRAP_PYTHON="$resolved"
            info "使用 Python 建立／管理專案環境：$BOOTSTRAP_PYTHON"
            return
        fi
    done

    die "找不到支援 Python 3.10+、Linux /proc 與 pidfd 的 Python；可用 PYTHON_BIN=/path/to/python 明確指定"
}

require_bootstrap_python() {
    [[ -n "$BOOTSTRAP_PYTHON" ]] \
        || die "尚未選定 bootstrap Python"
    python_supports_launcher "$BOOTSTRAP_PYTHON" \
        || die "選定的 Python 已不可用：$BOOTSTRAP_PYTHON"
}

require_launcher_platform() {
    local mv_help=""
    select_bootstrap_python
    require_bootstrap_python
    command -v mktemp >/dev/null 2>&1 || die "找不到 mktemp"
    command -v stat >/dev/null 2>&1 || die "找不到 stat"
    command -v mv >/dev/null 2>&1 || die "找不到 mv"
    stat -c '%u' "$ROOT_DIR" >/dev/null 2>&1 \
        || die "需要支援 stat -c 與 mv -T 的 GNU coreutils"
    mv_help="$(mv --help 2>&1)" || die "無法檢查 mv 功能"
    [[ "$mv_help" == *"--no-target-directory"* ]] \
        || die "需要支援 stat -c 與 mv -T 的 GNU coreutils"
}

ensure_config_files() {
    [[ ! -L "$ENV_FILE" ]] || die "拒絕 symlink .env"
    [[ ! -L "$SERVERS_FILE" ]] || die "拒絕 symlink servers.yaml"
    if [[ ! -e "$ENV_FILE" ]]; then
        [[ -f "$ROOT_DIR/.env.example" ]] || die "缺少 .env.example"
        cp "$ROOT_DIR/.env.example" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        ok "已建立 .env（mode 600）；預設是 loopback open-development"
        warn "正式或私網使用前，請先設定 AUTH_TOKEN 或 OIDC。"
    elif [[ ! -f "$ENV_FILE" ]]; then
        die ".env 存在但不是一般檔案"
    else
        chmod 600 "$ENV_FILE"
    fi

    if [[ ! -e "$SERVERS_FILE" ]]; then
        local tmp="$SERVERS_FILE.tmp.$$"
        printf 'servers: []\n' > "$tmp"
        chmod 600 "$tmp"
        mv "$tmp" "$SERVERS_FILE"
        ok "已建立空的 servers.yaml；不會探測任何示例主機"
    elif [[ ! -f "$SERVERS_FILE" ]]; then
        die "servers.yaml 存在但不是一般檔案"
    fi
}

ensure_venv() {
    require_bootstrap_python
    reject_symlink_components "$VENV_DIR" \
        || die "拒絕含 symlink 的 venv directory：$VENV_DIR"
    if [[ -e "$VENV_DIR" && ! -d "$VENV_DIR" ]]; then
        die "$VENV_DIR 已存在但不是目錄"
    fi
    if [[ ! -d "$VENV_DIR" ]]; then
        info "建立 Python venv：$VENV_DIR"
        "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"
    fi
    [[ -x "$VENV_PYTHON" ]] \
        || die "venv 不完整：找不到 $VENV_PYTHON；請移走後重新執行 setup"
}

requirements_hash() {
    "$VENV_PYTHON" - "$REQUIREMENTS_FILE" <<'PY'
import hashlib
import pathlib
import sys

digest = hashlib.sha256()
digest.update(pathlib.Path(sys.argv[1]).read_bytes())
digest.update(f"\npython={sys.version_info.major}.{sys.version_info.minor}\n".encode())
print(digest.hexdigest())
PY
}

dependency_runtime_ok() {
    "$VENV_PYTHON" -c \
        'import anthropic, asyncssh, authlib, fastapi, httpx, mcp, uvicorn, yaml' \
        >/dev/null 2>&1 \
        && "$VENV_PYTHON" -m pip check >/dev/null 2>&1
}

check_dependency_runtime() {
    dependency_runtime_ok \
        || die "venv dependency check 失敗；請移除 --skip-deps 或使用 --force-deps 修復"
}

ensure_dependencies() {
    [[ -f "$REQUIREMENTS_FILE" ]] || die "缺少 requirements.txt"
    if ((SKIP_DEPS)); then
        info "依要求跳過 pip install"
        check_dependency_runtime
        return
    fi

    local current_hash installed_hash=""
    current_hash="$(requirements_hash)"
    if [[ -r "$REQUIREMENTS_STAMP" ]]; then
        installed_hash="$(<"$REQUIREMENTS_STAMP")"
    fi

    if ((FORCE_DEPS)) || [[ "$current_hash" != "$installed_hash" ]]; then
        info "安裝 requirements.txt（首次執行或依賴已變更）"
        "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS_FILE"
        local tmp="$REQUIREMENTS_STAMP.tmp.$$"
        printf '%s\n' "$current_hash" > "$tmp"
        chmod 600 "$tmp"
        mv "$tmp" "$REQUIREMENTS_STAMP"
    else
        info "requirements.txt 未變，跳過 pip install"
    fi
    if ! dependency_runtime_ok; then
        warn "dependency stamp 存在但 runtime 不完整；自動修復一次"
        "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS_FILE"
        local repair_tmp="$REQUIREMENTS_STAMP.repair.$$"
        printf '%s\n' "$current_hash" > "$repair_tmp"
        chmod 600 "$repair_tmp"
        mv "$repair_tmp" "$REQUIREMENTS_STAMP"
    fi
    check_dependency_runtime
}

load_safe_config() {
    local output
    if ! output="$("$VENV_PYTHON" - <<'PY'
import ipaddress
import re
import socket

from app.config import load_app_config

config = load_app_config()
host = config.api_host.strip()
if not host or any(ord(character) < 32 or character.isspace() for character in host):
    raise SystemExit("API_HOST contains invalid characters")
if re.fullmatch(r"[A-Za-z0-9._:-]+", host) is None:
    raise SystemExit("API_HOST must be an IP address or simple hostname")
if not 1 <= config.api_port <= 65535:
    raise SystemExit("API_PORT must be between 1 and 65535")

allowed_networks = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)

try:
    addresses = {ipaddress.ip_address(host)}
except ValueError:
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise SystemExit("API_HOST does not resolve") from exc
if not addresses or any(
    not any(address in network for network in allowed_networks)
    for address in addresses
):
    raise SystemExit("API_HOST must resolve only to loopback/private addresses")
loopback_networks = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
bind_scope = (
    "loopback"
    if all(any(address in network for network in loopback_networks) for address in addresses)
    else "private"
)
if config.oidc_enabled and config.auth_token:
    mode = "OIDC + legacy fallback"
elif config.oidc_enabled:
    mode = "OIDC"
elif config.auth_token:
    mode = "legacy token"
else:
    mode = "open-development"
print(host)
print(config.api_port)
print(mode)
print(bind_scope)
PY
    )"; then
        die "設定驗證失敗；請檢查 .env 與 servers.yaml（不會顯示 secret）"
    fi

    mapfile -t config_lines <<< "$output"
    ((${#config_lines[@]} == 4)) || die "無法解析安全設定摘要"
    API_HOST_VALUE="${config_lines[0]}"
    API_PORT_VALUE="${config_lines[1]}"
    AUTH_MODE_VALUE="${config_lines[2]}"
    BIND_SCOPE_VALUE="${config_lines[3]}"

    if [[ "$AUTH_MODE_VALUE" == "open-development" && "$BIND_SCOPE_VALUE" != "loopback" ]]; then
        die "open-development 只准綁 loopback；請設定 AUTH_TOKEN/OIDC 或改 API_HOST=127.0.0.1"
    fi
}

bootstrap() {
    command -v nohup >/dev/null 2>&1 || die "找不到 nohup"
    command -v tail >/dev/null 2>&1 || die "找不到 tail"
    ensure_config_files
    ensure_venv
    ensure_dependencies
    prepare_runtime_dir
    load_safe_config
    ok "設定有效：$API_HOST_VALUE:$API_PORT_VALUE（$AUTH_MODE_VALUE）"
}

endpoint_url() {
    local host="$1" port="$2"
    if [[ "$host" == *:* ]]; then
        printf 'http://[%s]:%s/' "$host" "$port"
    else
        printf 'http://%s:%s/' "$host" "$port"
    fi
}

load_process_record() {
    [[ -r "$PID_FILE" && ! -L "$PID_FILE" ]] || return 1
    local -a values=()
    mapfile -t values < "$PID_FILE"
    ((${#values[@]} == 3)) || return 2
    [[ "${values[0]}" =~ ^[0-9]+$ \
        && "${values[1]}" =~ ^[0-9]+$ \
        && "${values[2]}" =~ ^[A-Fa-f0-9-]+$ ]] || return 2
    MANAGED_PID="${values[0]}"
    MANAGED_STARTTIME="${values[1]}"
    MANAGED_BOOT_ID="${values[2]}"
}

current_boot_id() {
    local boot_id=""
    [[ -r /proc/sys/kernel/random/boot_id ]] || return 1
    IFS= read -r boot_id < /proc/sys/kernel/random/boot_id
    [[ "$boot_id" =~ ^[A-Fa-f0-9-]+$ ]] || return 1
    printf '%s\n' "$boot_id"
}

process_starttime() {
    "$BOOTSTRAP_PYTHON" - "$1" <<'PY'
from pathlib import Path
import sys

try:
    raw = Path(f"/proc/{int(sys.argv[1])}/stat").read_text()
    fields_after_comm = raw[raw.rfind(")") + 2 :].split()
    if fields_after_comm[0] == "Z":
        raise SystemExit(1)
    print(fields_after_comm[19])
except (FileNotFoundError, IndexError, PermissionError, ValueError):
    raise SystemExit(1)
PY
}

process_record_is_current() {
    local actual="" boot_id=""
    boot_id="$(current_boot_id 2>/dev/null)" \
        && [[ "$boot_id" == "$MANAGED_BOOT_ID" ]] \
        || return 1
    actual="$(process_starttime "$MANAGED_PID" 2>/dev/null)" \
        && [[ "$actual" == "$MANAGED_STARTTIME" ]]
}

process_record_is_owned() {
    "$BOOTSTRAP_PYTHON" - \
        "$MANAGED_PID" "$MANAGED_STARTTIME" "$MANAGED_BOOT_ID" \
        "$ROOT_DIR" "$VENV_PYTHON" <<'PY'
from pathlib import Path
import os
import sys

pid, expected_starttime, expected_boot_id, expected_cwd, expected_python = sys.argv[1:]
try:
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    raw = Path(f"/proc/{int(pid)}/stat").read_text()
    fields_after_comm = raw[raw.rfind(")") + 2 :].split()
    actual_starttime = fields_after_comm[19]
    cwd = os.path.realpath(f"/proc/{pid}/cwd")
    argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
except (FileNotFoundError, IndexError, PermissionError, ValueError):
    raise SystemExit(1)

expected_argv = [expected_python.encode(), b"-m", b"app.main"]
raise SystemExit(
    0
    if boot_id == expected_boot_id
    and fields_after_comm[0] != "Z"
    and actual_starttime == expected_starttime
    and cwd == expected_cwd
    and argv[:3] == expected_argv
    else 1
)
PY
}

signal_owned_process() {
    local signal_number="$1"
    "$BOOTSTRAP_PYTHON" - \
        "$MANAGED_PID" "$MANAGED_STARTTIME" "$MANAGED_BOOT_ID" \
        "$ROOT_DIR" "$VENV_PYTHON" "$signal_number" <<'PY'
from pathlib import Path
import os
import signal
import sys

(
    pid_text,
    expected_starttime,
    expected_boot_id,
    expected_cwd,
    expected_python,
    signal_text,
) = sys.argv[1:]
pid = int(pid_text)
try:
    pidfd = os.pidfd_open(pid)
except (AttributeError, OSError):
    raise SystemExit(1)

try:
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields_after_comm = raw[raw.rfind(")") + 2 :].split()
    actual_starttime = fields_after_comm[19]
    cwd = os.path.realpath(f"/proc/{pid}/cwd")
    argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    expected_argv = [expected_python.encode(), b"-m", b"app.main"]
    if not (
        boot_id == expected_boot_id
        and fields_after_comm[0] != "Z"
        and actual_starttime == expected_starttime
        and cwd == expected_cwd
        and argv[:3] == expected_argv
    ):
        raise SystemExit(1)
    signal.pidfd_send_signal(pidfd, int(signal_text))
except (AttributeError, FileNotFoundError, IndexError, OSError, PermissionError, ValueError):
    raise SystemExit(1)
finally:
    os.close(pidfd)
PY
}

signal_spawned_child() {
    local child_pid="$1" child_starttime="$2" signal_number="$3"
    "$BOOTSTRAP_PYTHON" - \
        "$child_pid" "$child_starttime" "$$" "$signal_number" <<'PY'
from pathlib import Path
import os
import signal
import sys

pid_text, expected_starttime, expected_parent, signal_text = sys.argv[1:]
pid = int(pid_text)
try:
    pidfd = os.pidfd_open(pid)
except (AttributeError, OSError, ValueError):
    raise SystemExit(1)

try:
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields_after_comm = raw[raw.rfind(")") + 2 :].split()
    if not (
        fields_after_comm[0] != "Z"
        and fields_after_comm[1] == expected_parent
        and fields_after_comm[19] == expected_starttime
    ):
        raise SystemExit(1)
    signal.pidfd_send_signal(pidfd, int(signal_text))
except (AttributeError, FileNotFoundError, IndexError, OSError, PermissionError, ValueError):
    raise SystemExit(1)
finally:
    os.close(pidfd)
PY
}

write_process_record() {
    local pid="$1" starttime="$2" boot_id="$3"
    local tmp_pid
    tmp_pid="$(mktemp "$RUNTIME_DIR/.app.pid.XXXXXX")" \
        || die "無法建立 PID metadata"
    printf '%s\n%s\n%s\n' "$pid" "$starttime" "$boot_id" > "$tmp_pid"
    chmod 600 "$tmp_pid"
    mv "$tmp_pid" "$PID_FILE"
    MANAGED_PID="$pid"
    MANAGED_STARTTIME="$starttime"
    MANAGED_BOOT_ID="$boot_id"
}

write_endpoint_record() {
    local host="$1" port="$2" tmp_endpoint
    tmp_endpoint="$(mktemp "$RUNTIME_DIR/.app.endpoint.XXXXXX")" \
        || die "無法建立 endpoint metadata"
    printf '%s\n%s\n' "$host" "$port" > "$tmp_endpoint"
    chmod 600 "$tmp_endpoint"
    mv "$tmp_endpoint" "$ENDPOINT_FILE"
}

read_endpoint() {
    [[ -r "$ENDPOINT_FILE" && ! -L "$ENDPOINT_FILE" ]] || return 1
    local -a values=()
    mapfile -t values < "$ENDPOINT_FILE"
    ((${#values[@]} == 2)) || return 1
    API_HOST_VALUE="${values[0]}"
    API_PORT_VALUE="${values[1]}"
    [[ "$API_HOST_VALUE" =~ ^[A-Za-z0-9._:-]+$ \
        && "$API_PORT_VALUE" =~ ^[0-9]+$ \
        && ${#API_PORT_VALUE} -le 5 ]] \
        && ((10#$API_PORT_VALUE >= 1 && 10#$API_PORT_VALUE <= 65535))
}

http_ready() {
    "$BOOTSTRAP_PYTHON" - "$1" "$2" <<'PY' >/dev/null 2>&1
import http.client
import sys

connection = None
try:
    connection = http.client.HTTPConnection(sys.argv[1], int(sys.argv[2]), timeout=1)
    connection.request("GET", "/")
    response = connection.getresponse()
    response.read(1)
    raise SystemExit(0 if response.status == 200 else 1)
except (OSError, ValueError, http.client.HTTPException):
    raise SystemExit(1)
finally:
    if connection is not None:
        connection.close()
PY
}

port_is_open() {
    "$BOOTSTRAP_PYTHON" - "$1" "$2" <<'PY' >/dev/null 2>&1
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1):
        pass
except OSError:
    raise SystemExit(1)
PY
}

rotate_log_if_needed() {
    if [[ -f "$LOG_FILE" ]]; then
        local size
        size="$(wc -c < "$LOG_FILE")"
        if ((size > 5242880)); then
            mv -T -- "$LOG_FILE" "$LOG_FILE.1"
        fi
    fi
    touch "$LOG_FILE"
    chmod 600 "$LOG_FILE"
}

clear_process_metadata() {
    rm -f -- "$PID_FILE" "$ENDPOINT_FILE"
    MANAGED_PID=""
    MANAGED_STARTTIME=""
    MANAGED_BOOT_ID=""
}

reap_managed_child_if_possible() {
    wait "$MANAGED_PID" 2>/dev/null || true
}

load_current_owned_record() {
    local record_status=0
    load_process_record || record_status=$?
    if ((record_status == 1)); then
        [[ ! -e "$ENDPOINT_FILE" ]] || rm -f -- "$ENDPOINT_FILE"
        return 1
    fi
    if ((record_status == 2)); then
        warn "PID metadata 已損壞；不會傳送 signal，僅清除 metadata"
        clear_process_metadata
        return 1
    fi
    if ! process_record_is_current; then
        reap_managed_child_if_possible
        clear_process_metadata
        return 1
    fi
    process_record_is_owned \
        || die "PID metadata 指向存活但不相符的行程；拒絕覆寫或傳送 signal（PID $MANAGED_PID）"
    return 0
}

managed_service_is_ready() {
    load_current_owned_record || return 1
    read_endpoint \
        || die "本腳本管理的行程仍存活，但 endpoint metadata 遺失；請執行 stop"
    if http_ready "$API_HOST_VALUE" "$API_PORT_VALUE"; then
        return 0
    fi
    die "本腳本管理的行程仍存活但 health check 失敗；請先看 logs，再明確 restart"
}

wait_for_managed_exit() {
    local attempts="$1" attempt
    for ((attempt = 0; attempt < attempts; attempt++)); do
        if ! process_record_is_current; then
            reap_managed_child_if_possible
            return 0
        fi
        sleep 0.25
    done
    return 1
}

terminate_managed_process() {
    local reason="$1"
    if ! process_record_is_current; then
        reap_managed_child_if_possible
        clear_process_metadata
        return 0
    fi
    process_record_is_owned \
        || die "PID $MANAGED_PID 不再符合本腳本的 app.main identity；保留 metadata 並拒絕 signal"

    info "$reason：送出 SIGTERM，等待 FastAPI lifespan 關閉（PID $MANAGED_PID）"
    if ! signal_owned_process 15; then
        if ! process_record_is_current; then
            reap_managed_child_if_possible
            clear_process_metadata
            return 0
        fi
        die "無法以 pidfd 對已驗證行程送出 SIGTERM；保留 metadata"
    fi
    if wait_for_managed_exit 40; then
        clear_process_metadata
        ok "服務已停止"
        return 0
    fi

    process_record_is_owned \
        || die "等待期間 process identity 改變；保留 metadata 並拒絕 SIGKILL"
    warn "10 秒內未停止；對同一個 pidfd 驗證行程送出 SIGKILL"
    signal_owned_process 9 \
        || die "無法以 pidfd 送出 SIGKILL；保留 metadata"
    if wait_for_managed_exit 20; then
        clear_process_metadata
        ok "服務已停止"
        return 0
    fi
    die "SIGKILL 後仍無法確認原行程已停止；保留 metadata，請人工檢查"
}

abort_background_start() {
    local signal_number="$1"
    trap - INT TERM
    if ((BACKGROUND_STARTING)); then
        warn "啟動管理器收到 signal；先以 pidfd 安全關閉剛建立的 child"
        terminate_spawned_child "中止啟動"
        BACKGROUND_STARTING=0
    fi
    exit $((128 + signal_number))
}

terminate_spawned_child() {
    local reason="$1" child_pid="$BACKGROUND_CHILD_PID"
    local child_starttime="$BACKGROUND_CHILD_STARTTIME" attempt actual=""
    if [[ -z "$child_pid" ]]; then
        set +u
        child_pid="$!"
        set -u
    fi
    if [[ ! "$child_pid" =~ ^[0-9]+$ ]]; then
        return 0
    fi
    if [[ -z "$child_starttime" ]]; then
        child_starttime="$(process_starttime "$child_pid" 2>/dev/null)" || return 0
        BACKGROUND_CHILD_STARTTIME="$child_starttime"
    fi

    actual="$(process_starttime "$child_pid" 2>/dev/null)" || {
        wait "$child_pid" 2>/dev/null || true
        clear_process_metadata
        return 0
    }
    [[ "$actual" == "$child_starttime" ]] || {
        warn "啟動 child identity 已改變；保留 metadata 且不傳送 signal"
        return 1
    }

    info "$reason：對已驗證的直接 child 送出 SIGTERM（PID $child_pid）"
    if ! signal_spawned_child "$child_pid" "$child_starttime" 15; then
        actual="$(process_starttime "$child_pid" 2>/dev/null)" || {
            wait "$child_pid" 2>/dev/null || true
            clear_process_metadata
            return 0
        }
        warn "無法以 pidfd 關閉啟動 child；保留 metadata"
        return 1
    fi
    for ((attempt = 0; attempt < 40; attempt++)); do
        actual="$(process_starttime "$child_pid" 2>/dev/null)" || {
            wait "$child_pid" 2>/dev/null || true
            clear_process_metadata
            ok "啟動 child 已停止"
            return 0
        }
        [[ "$actual" == "$child_starttime" ]] \
            || {
                warn "啟動 child identity 在等待期間改變；保留 metadata"
                return 1
            }
        sleep 0.25
    done

    warn "啟動 child 10 秒內未停止；以 pidfd 送出 SIGKILL"
    signal_spawned_child "$child_pid" "$child_starttime" 9 \
        || return 1
    for ((attempt = 0; attempt < 20; attempt++)); do
        if ! actual="$(process_starttime "$child_pid" 2>/dev/null)"; then
            wait "$child_pid" 2>/dev/null || true
            clear_process_metadata
            ok "啟動 child 已停止"
            return 0
        fi
        [[ "$actual" == "$child_starttime" ]] || return 1
        sleep 0.25
    done
    warn "SIGKILL 後仍無法確認啟動 child 已停止；保留 metadata"
    return 1
}

background_exit_cleanup() {
    local original_status=$?
    trap - EXIT INT TERM
    if ((BACKGROUND_STARTING)); then
        terminate_spawned_child "啟動命令提前結束" || true
        BACKGROUND_STARTING=0
    fi
    release_lock
    return "$original_status"
}

start_background() {
    if managed_service_is_ready; then
        ok "服務已在運行（PID $MANAGED_PID）：$(endpoint_url "$API_HOST_VALUE" "$API_PORT_VALUE")"
        return
    fi

    bootstrap
    if port_is_open "$API_HOST_VALUE" "$API_PORT_VALUE"; then
        die "$API_HOST_VALUE:$API_PORT_VALUE 已被其他行程使用；不會接管或終止它"
    fi

    rotate_log_if_needed
    info "背景啟動 Dispatch Center"
    local pid="" starttime="" boot_id="" identity_ready=0
    boot_id="$(current_boot_id)" || die "無法讀取 Linux boot identity"
    BACKGROUND_STARTING=1
    BACKGROUND_CHILD_PID=""
    BACKGROUND_CHILD_STARTTIME=""
    trap 'abort_background_start 2' INT
    trap 'abort_background_start 15' TERM
    trap background_exit_cleanup EXIT
    PYTHONUNBUFFERED=1 nohup "$VENV_PYTHON" -m app.main \
        {LOCK_FD}>&- \
        >> "$LOG_FILE" 2>&1 < /dev/null &
    BACKGROUND_CHILD_PID=$!
    pid="$BACKGROUND_CHILD_PID"
    starttime="$(process_starttime "$pid" 2>/dev/null)" || {
        die "服務在建立 process metadata 前結束；請看 $LOG_FILE"
    }
    BACKGROUND_CHILD_STARTTIME="$starttime"
    write_process_record "$pid" "$starttime" "$boot_id"
    write_endpoint_record "$API_HOST_VALUE" "$API_PORT_VALUE"
    local identity_attempt
    for ((identity_attempt = 0; identity_attempt < 20; identity_attempt++)); do
        if ! process_record_is_current; then
            reap_managed_child_if_possible
            clear_process_metadata
            die "服務在 process identity 驗證前結束；請看 $LOG_FILE"
        fi
        if process_record_is_owned; then
            identity_ready=1
            break
        fi
        sleep 0.05
    done
    if ((identity_ready == 0)); then
        die "新行程 identity 驗證失敗；保留 metadata 且不傳送 signal，請人工檢查 PID $pid"
    fi
    local attempts=$((START_TIMEOUT_SEC * 4)) attempt
    for ((attempt = 0; attempt < attempts; attempt++)); do
        if ! process_record_is_current; then
            reap_managed_child_if_possible
            clear_process_metadata
            die "服務在 readiness 前結束；請看 $LOG_FILE"
        fi
        process_record_is_owned \
            || die "readiness 期間 process identity 改變；保留 metadata 並停止管理"
        if http_ready "$API_HOST_VALUE" "$API_PORT_VALUE"; then
            process_record_is_owned \
                || die "ready 後 process identity 改變；拒絕宣告啟動成功"
            BACKGROUND_STARTING=0
            trap - INT TERM
            ok "啟動完成（PID $pid）：$(endpoint_url "$API_HOST_VALUE" "$API_PORT_VALUE")"
            info "狀態：./quickstart.sh status　log：./quickstart.sh logs --follow"
            return
        fi
        sleep 0.25
    done

    terminate_managed_process "啟動逾時"
    BACKGROUND_STARTING=0
    trap - INT TERM
    die "服務在 ${START_TIMEOUT_SEC}s 內未 ready；請看 $LOG_FILE"
}

setup_only() {
    if load_current_owned_record; then
        die "服務仍在運行（PID $MANAGED_PID）；setup 不會修改 live venv，請先 stop"
    fi
    bootstrap
    ok "setup 完成；執行 ./quickstart.sh 啟動"
}

stop_service() {
    if ! load_current_owned_record; then
        info "服務未由 quickstart 管理，或已停止；stale metadata 已清除"
        return
    fi
    terminate_managed_process "停止服務"
}

status_service() {
    local record_status=0
    load_process_record || record_status=$?
    if ((record_status != 0)); then
        info "狀態：stopped（沒有有效 PID metadata）"
        return 1
    fi
    if ! process_record_is_current; then
        warn "狀態：stopped（stale process metadata；執行 stop 可清除）"
        return 1
    fi
    if ! process_record_is_owned; then
        warn "狀態：foreign process identity（拒絕管理 PID $MANAGED_PID）"
        return 2
    fi
    if ! read_endpoint; then
        warn "狀態：running，但 endpoint metadata 遺失"
        return 2
    fi
    local url
    url="$(endpoint_url "$API_HOST_VALUE" "$API_PORT_VALUE")"
    if http_ready "$API_HOST_VALUE" "$API_PORT_VALUE"; then
        ok "狀態：running + ready（PID $MANAGED_PID）"
        printf '%s\n' "$url"
        return 0
    fi
    warn "狀態：running，但 health check 失敗（PID $MANAGED_PID，$url）"
    return 2
}

show_url() {
    if read_endpoint; then
        endpoint_url "$API_HOST_VALUE" "$API_PORT_VALUE"
        printf '\n'
        return
    fi
    [[ -x "$VENV_PYTHON" && -f "$ENV_FILE" ]] \
        || die "尚未 setup，沒有可顯示的 endpoint"
    load_safe_config
    endpoint_url "$API_HOST_VALUE" "$API_PORT_VALUE"
    printf '\n'
}

show_logs() {
    [[ -f "$LOG_FILE" ]] || die "尚無 log：$LOG_FILE"
    if ((FOLLOW_LOGS)); then
        release_lock
        trap - EXIT
        exec tail -n 100 -F "$LOG_FILE"
    fi
    tail -n 100 "$LOG_FILE"
}

start_foreground() {
    if managed_service_is_ready; then
        die "服務已在運行（PID $MANAGED_PID）；請先 stop"
    fi
    bootstrap
    if port_is_open "$API_HOST_VALUE" "$API_PORT_VALUE"; then
        die "$API_HOST_VALUE:$API_PORT_VALUE 已被其他行程使用"
    fi

    local pid="$$" starttime="" boot_id=""
    boot_id="$(current_boot_id)" || die "無法讀取 Linux boot identity"
    starttime="$(process_starttime "$pid")" \
        || die "無法取得 foreground process identity"
    write_process_record "$pid" "$starttime" "$boot_id"
    write_endpoint_record "$API_HOST_VALUE" "$API_PORT_VALUE"
    trap 'clear_process_metadata' EXIT
    ok "前景啟動：$(endpoint_url "$API_HOST_VALUE" "$API_PORT_VALUE")（Ctrl+C 停止）"
    release_lock
    exec "$VENV_PYTHON" -m app.main
}

main() {
    parse_args "$@"
    case "$ACTION" in
        setup)
            require_launcher_platform
            acquire_lock
            setup_only
            ;;
        start)
            require_launcher_platform
            acquire_lock
            start_background
            ;;
        foreground)
            require_launcher_platform
            acquire_lock
            start_foreground
            ;;
        stop)
            require_launcher_platform
            acquire_lock
            stop_service
            ;;
        restart|restart-app)
            require_launcher_platform
            acquire_lock
            stop_service
            start_background
            ;;
        status)
            require_launcher_platform
            acquire_lock
            status_service
            ;;
        logs)
            require_launcher_platform
            acquire_lock
            show_logs
            ;;
        url)
            require_launcher_platform
            acquire_lock
            show_url
            ;;
        help) usage ;;
        *) die "未知命令：$ACTION（請用 --help）" ;;
    esac
}

main "$@"
