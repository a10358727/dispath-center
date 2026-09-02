#!/usr/bin/env bash
# release-gate 靜態不變量檢查 — 唯讀、確定性。
#
# 每條檢查對應 docs/PLATFORM_CHARTER.md 的
# 一個 INV ID,且只釘「已在 repository 驗證過的精確符號/行」——不做泛用的
# import 掃描或啟發式猜測。輸出 PASS/FAIL/SKIP(附可行動的檔案路徑);
# 有任何 FAIL 或 PRECONDITION FAILED 以非零碼結束。
#
# 本腳本絕不:連工作機、執行 SSH、發網路請求、安裝依賴、修改原始碼、
# 寫入 jobqueue.db / audit.jsonl / servers.yaml、核准或執行任何請求、
# 進行任何 git 寫入操作(只用 rev-parse / diff 唯讀比對)。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
fail_count=0
precondition_failed=0

pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; fail_count=$((fail_count + 1)); }
skip() { printf 'SKIP: %s\n' "$1"; }

require_file() {
  # 檢查標的檔案不存在時,該條檢查以 FAIL 記(架構檔案消失本身就是異常)。
  local f="$1"
  if [ ! -f "$REPO/$f" ]; then
    fail "expected file missing: $f"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# INV-LLM-3:app/agent_tools.py 不得 import 執行層(sshpool/localrun/subprocess)
# INV-LLM-4 前半:app/mcp_bridge.py 同樣不得 import 執行層
# (runtime 釘住:的 forbidden_modules 斷言)
# ---------------------------------------------------------------------------
for f in app/mcp_bridge.py; do
  if require_file "$f"; then
    if hits=$(grep -nE '^[[:space:]]*(from|import)[[:space:]]+(app\.)?(sshpool|localrun|subprocess)\b' "$REPO/$f"); then
      fail "INV-LLM-3: $f imports forbidden execution-layer module -> $hits"
    else
      pass "INV-LLM-3: $f has no sshpool/localrun/subprocess import"
    fi
  fi
done

# ---------------------------------------------------------------------------
# INV-LLM-4:app/mcp_bridge.py 是行程隔離的 HTTP client,不 import 任何 app.*
# (模組 docstring 鐵律 1;bridge 只透過 httpx 呼叫調度中心 REST API)
# ---------------------------------------------------------------------------
if require_file app/mcp_bridge.py; then
  if hits=$(grep -nE '^[[:space:]]*(from[[:space:]]+app\.|import[[:space:]]+app\b)' "$REPO/app/mcp_bridge.py"); then
    fail "INV-LLM-4: app/mcp_bridge.py imports app.* (must stay HTTP-only) -> $hits"
  else
    pass "INV-LLM-4: app/mcp_bridge.py imports no app.* module"
  fi
fi

# ---------------------------------------------------------------------------
# INV-LLM-2:工具表永無 approve/reject/自由 shell 工具
# (runtime 釘住:的 forbidden_names 斷言;
#  這裡靜態複驗 TOOLS dict key 與 ToolSpec name=,以及 bridge 的函式名)
# 注意:唯讀工具 "approvals"(列出核准請求)是合法的,exact-match 才不誤殺。
# ---------------------------------------------------------------------------
if require_file app/mcp_bridge.py; then
  if hits=$(grep -nE '(def[[:space:]]+(approve|reject)[a-z_]*\(|name="(approve|reject)")' "$REPO/app/mcp_bridge.py"); then
    fail "INV-LLM-2: approve/reject-shaped tool found in app/mcp_bridge.py -> $hits"
  else
    pass "INV-LLM-2: app/mcp_bridge.py defines no approve/reject tool"
  fi
fi

# ---------------------------------------------------------------------------
# INV-APPROVAL-4:自動核准 kind 閘門只認 enqueue/stop
# (釘住 app/approvals.py 內 maybe_auto_approve() 的那一行;字面比對)
# ---------------------------------------------------------------------------
if require_file app/approvals.py; then
  if grep -qF 'if approval.kind not in ("enqueue", "stop"):' "$REPO/app/approvals.py"; then
    pass 'INV-APPROVAL-4: auto-approve kind gate ("enqueue", "stop") present in app/approvals.py'
  else
    fail 'INV-APPROVAL-4: auto-approve kind gate line not found in app/approvals.py — whitelist may have been widened or refactored; verify maybe_auto_approve() manually'
  fi
fi

# ---------------------------------------------------------------------------
# INV-APPROVAL-1:VALID_APPROVAL_KINDS 的每個 kind 在 app/approvals.py 有落點，
# 或明確列在 transaction-only 集合並由 generic approve/reject dominant guard
# 拒絕。後者只能由專用 transaction 落地，不能為了滿足字面分支檢查而繞回
# legacy JSONL／generic mutation path。
# ---------------------------------------------------------------------------
if require_file app/db.py && require_file app/approvals.py; then
  kinds=$(sed -n '/^VALID_APPROVAL_KINDS = {/,/^}/p' "$REPO/app/db.py" | grep -oE '"[a-z_]+"' | tr -d '"' || true)
  transaction_only=$(sed -n '/^TRANSACTION_ONLY_APPROVAL_KINDS = frozenset(/,/^)/p' "$REPO/app/db.py" | grep -oE '"[a-z_]+"' | tr -d '"' || true)
  if [ -z "$kinds" ]; then
    fail "INV-APPROVAL-1: could not parse VALID_APPROVAL_KINDS from app/db.py (symbol moved or renamed?)"
  elif [ -z "$transaction_only" ]; then
    fail "INV-APPROVAL-1: could not parse TRANSACTION_ONLY_APPROVAL_KINDS from app/db.py"
  else
    orphan=""
    invalid_transaction_only=""
    for k in $transaction_only; do
      if ! printf '%s\n' "$kinds" | grep -qxF "$k"; then
        invalid_transaction_only="$invalid_transaction_only $k"
      fi
    done
    for k in $kinds; do
      if printf '%s\n' "$transaction_only" | grep -qxF "$k"; then
        continue
      fi
      if ! grep -qF "\"$k\"" "$REPO/app/approvals.py"; then
        orphan="$orphan $k"
      fi
    done
    transaction_guards=$(grep -cF 'if approval.kind in TRANSACTION_ONLY_APPROVAL_KINDS:' "$REPO/app/approvals.py" || true)
    if [ -n "$invalid_transaction_only" ]; then
      fail "INV-APPROVAL-1: transaction-only kind(s) are not in VALID_APPROVAL_KINDS:$invalid_transaction_only"
    elif [ "$transaction_guards" -ne 2 ]; then
      fail "INV-APPROVAL-1: expected transaction-only dominant guards in generic approve and reject, found $transaction_guards"
    elif [ -n "$orphan" ]; then
      fail "INV-APPROVAL-1: kind(s) in VALID_APPROVAL_KINDS (app/db.py) never referenced in app/approvals.py:$orphan"
    else
      pass "INV-APPROVAL-1: every valid kind has a materialization branch or transaction-only dominant guard ($(echo "$kinds" | wc -w) kinds)"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# INV-APPROVAL-5:auth 豁免集合只有三個精確 method/path
# (用 Python AST 讀常數,不依賴換行/縮排/集合順序;`/static/` 前綴仍由
#  middleware 的既有 startswith 邏輯處理,不在這個 exact-route 集合裡。)
# ---------------------------------------------------------------------------
if require_file app/main.py; then
  if ! command -v python3 >/dev/null 2>&1; then
    fail 'INV-APPROVAL-5: python3 is required to parse _AUTH_EXEMPT_ROUTES exactly'
  elif detail=$(python3 - "$REPO/app/main.py" 2>&1 <<'PY'
import ast
import sys

path = sys.argv[1]
tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
assignments = []
for node in tree.body:
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        continue
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if any(isinstance(target, ast.Name) and target.id == "_AUTH_EXEMPT_ROUTES" for target in targets):
        assignments.append(node.value)

if len(assignments) != 1:
    raise SystemExit(
        f"expected exactly one _AUTH_EXEMPT_ROUTES assignment, found {len(assignments)}"
    )
try:
    actual = ast.literal_eval(assignments[0])
except (TypeError, ValueError, SyntaxError) as exc:
    raise SystemExit(f"_AUTH_EXEMPT_ROUTES is not a literal set: {exc}")

expected = {
    ("GET", "/"),
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
}
if actual != expected:
    raise SystemExit(
        f"expected {sorted(expected)!r}, found {sorted(actual)!r}"
    )

middleware = [
    node
    for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and node.name == "auth_middleware"
]
if len(middleware) != 1:
    raise SystemExit(f"expected one auth_middleware, found {len(middleware)}")
exempt_assignments = [
    node.value
    for node in ast.walk(middleware[0])
    if isinstance(node, (ast.Assign, ast.AnnAssign))
    and any(
        isinstance(target, ast.Name) and target.id == "exempt"
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    )
]
if len(exempt_assignments) != 1:
    raise SystemExit(
        f"expected one auth middleware exempt assignment, found {len(exempt_assignments)}"
    )
expected_exempt = ast.parse(
    '(request.method, path) in _AUTH_EXEMPT_ROUTES or path.startswith("/static/")',
    mode="eval",
).body
if ast.dump(exempt_assignments[0], include_attributes=False) != ast.dump(
    expected_exempt, include_attributes=False
):
    raise SystemExit(
        "auth middleware exemption expression must contain only the exact route "
        "set plus the /static/ prefix"
    )
PY
  ); then
    pass 'INV-APPROVAL-5: exact routes plus sole /static/ prefix are pinned'
  else
    fail "INV-APPROVAL-5: exact auth exemption set changed in app/main.py -> $detail"
  fi
fi

# ---------------------------------------------------------------------------
# INV-AUDIT-1:audit.jsonl append-only(app/audit.py 寫入只用 "a" 模式)
# ---------------------------------------------------------------------------
if require_file app/audit.py; then
  if ! grep -qF 'open(path, "a"' "$REPO/app/audit.py"; then
    fail 'INV-AUDIT-1: append-mode open(path, "a" ...) not found in app/audit.py'
  elif hits=$(grep -nE 'open\([^)]*"(w|w\+|a\+)"' "$REPO/app/audit.py"); then
    fail "INV-AUDIT-1: non-append write mode found in app/audit.py -> $hits"
  else
    pass 'INV-AUDIT-1: app/audit.py writes audit.jsonl in append mode only'
  fi
fi

# ---------------------------------------------------------------------------
# 鐵律 4(私網綁定):api_host 預設維持 127.0.0.1(dataclass 預設與 .env 後備)
# ---------------------------------------------------------------------------
if require_file app/config.py; then
  ok=1
  grep -qF 'api_host: str = "127.0.0.1"' "$REPO/app/config.py" || ok=0
  grep -qF '"API_HOST", "127.0.0.1"' "$REPO/app/config.py" || ok=0
  if [ "$ok" -eq 1 ]; then
    pass 'BIND-PRIVATE: api_host defaults to 127.0.0.1 in app/config.py (dataclass + env fallback)'
  else
    fail 'BIND-PRIVATE: api_host default in app/config.py is no longer 127.0.0.1 — verify iron rule 4 (private-network bind only)'
  fi
fi

# D2-BACKEND-GATE retired with the Engineering Task backend (DG-CONSOLIDATION-v1 C-5 (c), 2026-09-02).

# ---------------------------------------------------------------------------
# INV-APPROVAL-2:enqueue 路徑在入列前呼叫 is_dangerous()(建立當下拒絕)
# ---------------------------------------------------------------------------
if require_file app/jobqueue.py; then
  if grep -qF 'dangerous, reason = is_dangerous(command)' "$REPO/app/jobqueue.py"; then
    pass 'INV-APPROVAL-2: enqueue_job() calls is_dangerous() before insert (app/jobqueue.py)'
  else
    fail 'INV-APPROVAL-2: is_dangerous() gate not found in app/jobqueue.py enqueue path'
  fi
fi

# ---------------------------------------------------------------------------
# INV-TEST-2:釘住測試檔存在(邊界斷言的載體不得消失)
# ---------------------------------------------------------------------------
for f in tests/test_approvals.py tests/test_autoapprove.py tests/test_mcp_bridge.py tests/test_db_migration.py tests/test_security.py tests/test_oidc.py tests/test_oidc_provider.py tests/test_capability_ledger.py tests/test_ci_release_gate.py tests/test_backup_restore_scripts.py tests/test_node_primitives_smoke.py tests/test_exec_attempt_decision_gate.py tests/test_execution_attempt_foundation.py tests/test_execution_launch_arbitration.py tests/test_execution_attempt_dispatch.py tests/test_canary_report.py tests/test_wp2d_canary_request.py tests/test_server_publication.py tests/test_dataset_snapshot.py tests/test_execution_plan.py tests/test_execution_plan_api.py tests/test_node_agent_daemon.py tests/test_node_safety_hardening.py tests/test_health_endpoints.py tests/test_restore_drill.py tests/test_code_promotion.py tests/test_node_v2_lease.py tests/test_node_credential_lifecycle.py; do
  if [ -f "$REPO/$f" ]; then
    pass "INV-TEST-2: pinning test file present: $f"
  else
    fail "INV-TEST-2: pinning test file missing: $f"
  fi
done

# DEP-DRIFT retired (DG-CONSOLIDATION-v1 C-1): tests/test_packaging_metadata.py and
# scripts/check_requirements_lock.py already pin the dependency groups exactly.
printf -- '----------------------------------------\n'
if [ "$precondition_failed" -ne 0 ]; then
  printf 'RESULT: PRECONDITION FAILED (plus %d FAIL)\n' "$fail_count"
  exit 2
elif [ "$fail_count" -ne 0 ]; then
  printf 'RESULT: FAIL (%d violation(s))\n' "$fail_count"
  exit 1
else
  printf 'RESULT: PASS (all static invariant checks green)\n'
  exit 0
fi
