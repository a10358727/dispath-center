#!/usr/bin/env bash
# release-gate 靜態不變量檢查 — 唯讀、確定性。
#
# 每條檢查對應 .claude/skills/dispatcher-domain/references/invariants.md 的
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
# (runtime 釘住:tests/test_agent_tools.py 的 forbidden_modules 斷言)
# ---------------------------------------------------------------------------
for f in app/agent_tools.py app/mcp_bridge.py; do
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
# (runtime 釘住:tests/test_agent_tools.py 的 forbidden_names 斷言;
#  這裡靜態複驗 TOOLS dict key 與 ToolSpec name=,以及 bridge 的函式名)
# 注意:唯讀工具 "approvals"(列出核准請求)是合法的,exact-match 才不誤殺。
# ---------------------------------------------------------------------------
if require_file app/agent_tools.py; then
  found=""
  for name in approve approve_approval reject reject_approval shell exec run_command; do
    if hits=$(grep -nE "(^[[:space:]]*\"${name}\":[[:space:]]*ToolSpec\(|name=\"${name}\")" "$REPO/app/agent_tools.py"); then
      found="${found}${hits}\n"
    fi
  done
  if [ -n "$found" ]; then
    fail "INV-LLM-2: forbidden tool name registered in app/agent_tools.py -> $(printf '%b' "$found" | tr '\n' ' ')"
  else
    pass "INV-LLM-2: app/agent_tools.py registers no approve/reject/shell/exec tool"
  fi
fi
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
# INV-APPROVAL-1:VALID_APPROVAL_KINDS 的每個 kind 在 app/approvals.py 有落點
# (kind 加了白名單卻沒有 approve() 分支 = 核准後無法落地的孤兒 kind)
# ---------------------------------------------------------------------------
if require_file app/db.py && require_file app/approvals.py; then
  kinds=$(sed -n '/^VALID_APPROVAL_KINDS = {/,/^}/p' "$REPO/app/db.py" | grep -oE '"[a-z_]+"' | tr -d '"' || true)
  if [ -z "$kinds" ]; then
    fail "INV-APPROVAL-1: could not parse VALID_APPROVAL_KINDS from app/db.py (symbol moved or renamed?)"
  else
    orphan=""
    for k in $kinds; do
      if ! grep -qF "\"$k\"" "$REPO/app/approvals.py"; then
        orphan="$orphan $k"
      fi
    done
    if [ -n "$orphan" ]; then
      fail "INV-APPROVAL-1: kind(s) in VALID_APPROVAL_KINDS (app/db.py) never referenced in app/approvals.py:$orphan"
    else
      pass "INV-APPROVAL-1: every VALID_APPROVAL_KINDS entry is referenced in app/approvals.py ($(echo "$kinds" | wc -w) kinds)"
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

# ---------------------------------------------------------------------------
# D2-BACKEND-GATE:Engineering Task backend 預設關閉，且啟用需雙鑰匙
# （docs/AI_ENGINEERING_DECISION_GATE.md §D2：finalization sandbox 完成前
#  不得單開 ENGINEERING_TASK_BACKEND_V1）
# ---------------------------------------------------------------------------
if require_file app/config.py; then
  ok=1
  grep -qF 'engineering_task_backend_v1: bool = False' "$REPO/app/config.py" || ok=0
  grep -qF '"ENGINEERING_TASK_BACKEND_V1", "false"' "$REPO/app/config.py" || ok=0
  grep -qF 'engineering_task_backend_v1_accept_unsandboxed_finalization: bool = False' "$REPO/app/config.py" || ok=0
  grep -qF '"ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION", "false"' "$REPO/app/config.py" || ok=0
  grep -qF '"ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION=true "' "$REPO/app/config.py" || ok=0
  if [ "$ok" -eq 1 ]; then
    pass 'D2-BACKEND-GATE: engineering task backend defaults off and enabling requires the explicit unsandboxed-finalization acknowledgment'
  else
    fail 'D2-BACKEND-GATE: backend flag defaults or the D2 double-key interlock in app/config.py changed — see docs/AI_ENGINEERING_DECISION_GATE.md §D2'
  fi
fi

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
for f in tests/test_agent_tools.py tests/test_approvals.py tests/test_autoapprove.py tests/test_mcp_bridge.py tests/test_db_migration.py tests/test_security.py tests/test_oidc.py tests/test_oidc_provider.py; do
  if [ -f "$REPO/$f" ]; then
    pass "INV-TEST-2: pinning test file present: $f"
  else
    fail "INV-TEST-2: pinning test file missing: $f"
  fi
done

# ---------------------------------------------------------------------------
# 依賴漂移:requirements.txt vs local Git HEAD
# Goal 1 / Slice 7 已由使用者明文核准唯一新增 Authlib>=1.7,<2.0。比較
# non-comment effective specs,所以說明註解/排版不影響結果;除此之外任何新增、
# 刪除或版本修改仍 FAIL。HEAD 未來已含該行時也不會要求重複加入。
# ---------------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  printf 'PRECONDITION FAILED: no Git baseline (git not installed; dependency-drift and test-deletion checks cannot run)\n'
  precondition_failed=1
elif ! git -C "$REPO" rev-parse --verify HEAD >/dev/null 2>&1; then
  printf 'PRECONDITION FAILED: no Git baseline (repository has no initial commit; run `git init && git add -A && git commit` to establish one)\n'
  precondition_failed=1
else
  approved_authlib='Authlib>=1.7,<2.0'
  baseline_specs=$(
    git -C "$REPO" show HEAD:requirements.txt \
      | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
      | sed -e '/^$/d' -e '/^#/d' \
      | LC_ALL=C sort -u
  )
  current_specs=$(
    sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
      -e '/^$/d' -e '/^#/d' "$REPO/requirements.txt" \
      | LC_ALL=C sort -u
  )
  if printf '%s\n' "$baseline_specs" | grep -qxF "$approved_authlib"; then
    expected_specs="$baseline_specs"
  else
    expected_specs=$(printf '%s\n%s\n' "$baseline_specs" "$approved_authlib" | LC_ALL=C sort -u)
  fi
  if [ "$current_specs" = "$expected_specs" ]; then
    pass "DEP-DRIFT: effective requirements match HEAD plus the approved $approved_authlib change"
  else
    fail "DEP-DRIFT: effective requirements differ from HEAD beyond approved $approved_authlib — review git diff HEAD -- requirements.txt"
  fi
fi

# ---------------------------------------------------------------------------
# 總結
# ---------------------------------------------------------------------------
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
