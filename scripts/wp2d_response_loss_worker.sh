#!/bin/bash
set -u

usage() {
    echo "usage: sudo $0 <control-plane-ipv4> <next-job-id> [worker-home]" >&2
    exit 2
}

[[ "$EUID" == "0" ]] || {
    echo "ERROR: run this helper through sudo on the non-production worker" >&2
    exit 2
}
[[ "$#" -ge 2 && "$#" -le 3 ]] || usage

CONTROL_IP=$1
JOB_ID=$2
WORKER_HOME=${3:-/home/${SUDO_USER:-}}

[[ "$CONTROL_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || usage
IFS=. read -r octet1 octet2 octet3 octet4 <<<"$CONTROL_IP"
for octet in "$octet1" "$octet2" "$octet3" "$octet4"; do
    octet_value=$((10#$octet))
    ((octet_value >= 0 && octet_value <= 255)) || usage
done
[[ "$JOB_ID" =~ ^[1-9][0-9]*$ ]] || usage
[[ "$WORKER_HOME" == /* && -d "$WORKER_HOME" ]] || usage
command -v iptables >/dev/null 2>&1 || {
    echo "ERROR: iptables is unavailable" >&2
    exit 2
}

ATTEMPT_ROOT="$WORKER_HOME/agent_jobs/$JOB_ID/attempts"
DROP_SECONDS=35
WAIT_SECONDS=600
ACTIVE=0

delete_rule() {
    iptables -D OUTPUT -d "$CONTROL_IP" -p tcp --sport 22 -j DROP \
        2>/dev/null || true
}

cleanup() {
    if [[ "$ACTIVE" == "1" ]]; then
        delete_rule
    fi
}
trap cleanup EXIT INT TERM HUP

if iptables -C OUTPUT -d "$CONTROL_IP" -p tcp --sport 22 -j DROP \
    2>/dev/null; then
    echo "ERROR: matching firewall rule already exists" >&2
    exit 1
fi

if find "$ATTEMPT_ROOT" -mindepth 2 -maxdepth 2 -type d -name claim \
    -print -quit 2>/dev/null | grep -q .; then
    echo "ERROR: Job $JOB_ID already has a remote claim; choose the next Job" >&2
    exit 1
fi

echo "ARMED: approve the pending canary request for Job $JOB_ID now"
deadline=$((SECONDS + WAIT_SECONDS))

while ((SECONDS < deadline)); do
    claim=$(find "$ATTEMPT_ROOT" -mindepth 2 -maxdepth 2 \
        -type d -name claim -print -quit 2>/dev/null || true)
    if [[ -n "$claim" ]]; then
        # A detached best-effort cleanup is a second safety net if the waiting
        # SSH session disappears before this shell reaches its EXIT trap.
        nohup bash -c \
            'sleep "$1"; iptables -D OUTPUT -d "$2" -p tcp --sport 22 -j DROP 2>/dev/null || true' \
            _ "$((DROP_SECONDS + 15))" "$CONTROL_IP" \
            >/dev/null 2>&1 &

        iptables -I OUTPUT 1 -d "$CONTROL_IP" -p tcp --sport 22 -j DROP
        ACTIVE=1
        sleep "$DROP_SECONDS"
        cleanup
        ACTIVE=0
        echo "RESTORED: SSH response restored"
        exit 0
    fi
    sleep 0.01
done

echo "TIMEOUT: Job $JOB_ID claim not found" >&2
exit 1
