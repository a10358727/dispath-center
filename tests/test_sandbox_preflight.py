"""Goal 3 Phase A A1 sandbox preflight tests（docs/GOAL_3_FUTURE_WORK_PLAN.md）。

唯讀契約：腳本只含 test/cat/grep/id/command -v 與 `bwrap ... true`（丟棄式
namespace、零寫入）；解析 fail-closed——缺 section、截斷、空輸出一律
unknown，unknown 永遠不是通過（`ready` 只認全 pass）。端點沿用
`/codex-runner/status` 的 `{"configured": false}` 慣例，Runner 連不上時
全 unknown + `ready=false`。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.sandbox_preflight import (
    REQUIRED_DELEGATED_CONTROLLERS,
    SANDBOX_PREFLIGHT_SCRIPT,
    build_sandbox_preflight_script,
    parse_sandbox_preflight_output,
)


def _full_pass_output(delegation: str = "cpuset cpu io memory pids") -> str:
    return "\n".join(
        [
            "---BWRAP---",
            "bubblewrap 0.6.1",
            "---BWRAP-NETNS---",
            "ok",
            "---CGROUP-CONTROLLERS---",
            "cpuset cpu io memory hugetlb pids rdma misc",
            "---CGROUP-USER-DELEGATION---",
            delegation,
            "---SYSTEMD-USER-BUS---",
            "ok",
            "---PRJQUOTA---",
            "ok",
            "---END---",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# 腳本唯讀契約
# ---------------------------------------------------------------------------


def test_preflight_script_is_read_only():
    executable_lines = "\n".join(
        line
        for line in SANDBOX_PREFLIGHT_SCRIPT.splitlines()
        if not line.strip().startswith("#")
    )
    for forbidden in (
        "rm ", "mkdir", "touch", "mv ", "cp ", "chmod", "chown",
        "sudo", "apt", "tee", ">>", "> /",
    ):
        assert forbidden not in executable_lines, forbidden
    # 唯一的執行是丟棄式 namespace 裡的 `true`。
    assert "--unshare-net --die-with-parent true" in SANDBOX_PREFLIGHT_SCRIPT
    assert build_sandbox_preflight_script() == SANDBOX_PREFLIGHT_SCRIPT
    for marker in (
        "---BWRAP---", "---BWRAP-NETNS---", "---CGROUP-CONTROLLERS---",
        "---CGROUP-USER-DELEGATION---", "---SYSTEMD-USER-BUS---",
        "---PRJQUOTA---", "---END---",
    ):
        assert marker in SANDBOX_PREFLIGHT_SCRIPT


# ---------------------------------------------------------------------------
# 解析：fail-closed
# ---------------------------------------------------------------------------


def test_parse_full_pass_output_is_ready():
    report = parse_sandbox_preflight_output(_full_pass_output())
    assert report["ready"] is True
    assert all(c["status"] == "pass" for c in report["checks"].values())
    assert set(report["checks"]) == {
        "bwrap", "bwrap_no_network", "cgroup_v2",
        "cgroup_user_delegation", "systemd_user_bus", "project_quota",
    }


def test_parse_empty_or_truncated_output_is_all_unknown_not_ready():
    for stdout in ("", "garbage", _full_pass_output().replace("---END---", "")):
        report = parse_sandbox_preflight_output(stdout)
        assert report["ready"] is False
        assert all(c["status"] == "unknown" for c in report["checks"].values())


def test_parse_absent_items_fail():
    stdout = "\n".join(
        [
            "---BWRAP---", "absent",
            "---BWRAP-NETNS---", "absent",
            "---CGROUP-CONTROLLERS---", "absent",
            "---CGROUP-USER-DELEGATION---", "absent",
            "---SYSTEMD-USER-BUS---", "absent",
            "---PRJQUOTA---", "absent",
            "---END---", "",
        ]
    )
    report = parse_sandbox_preflight_output(stdout)
    assert report["ready"] is False
    assert all(c["status"] == "fail" for c in report["checks"].values())


def test_parse_incomplete_delegation_fails_with_missing_controllers():
    # 有可寫 user scope 但缺 memory/pids —— 委派不完整就是 fail，
    # 不是「有一點算一點」。
    report = parse_sandbox_preflight_output(_full_pass_output(delegation="cpu"))
    check = report["checks"]["cgroup_user_delegation"]
    assert check["status"] == "fail"
    for controller in REQUIRED_DELEGATED_CONTROLLERS:
        if controller != "cpu":
            assert controller in check["detail"]
    assert report["ready"] is False


def test_parse_netns_failure_fails_that_check_only():
    stdout = _full_pass_output().replace(
        "---BWRAP-NETNS---\nok", "---BWRAP-NETNS---\nfailed"
    )
    report = parse_sandbox_preflight_output(stdout)
    assert report["checks"]["bwrap_no_network"]["status"] == "fail"
    assert report["checks"]["bwrap"]["status"] == "pass"
    assert report["ready"] is False


# ---------------------------------------------------------------------------
# 端點
# ---------------------------------------------------------------------------


def test_endpoint_reports_unconfigured_without_runner(api_client):
    client, main_module = api_client
    main_module.app_state.config.codex_runner_server = None

    response = client.get("/codex-runner/sandbox-preflight")

    assert response.status_code == 200
    assert response.json() == {"configured": False}


def test_endpoint_runs_probe_and_returns_report(api_client):
    client, main_module = api_client
    main_module.app_state.config.codex_runner_server = "runner-a"
    captured = {}

    async def fake_ssh_run(server_name, command, timeout):
        captured["server"] = server_name
        captured["command"] = command
        return SimpleNamespace(exit_status=0, stdout=_full_pass_output(), stderr="")

    main_module.app_state.ssh_run = fake_ssh_run

    response = client.get("/codex-runner/sandbox-preflight")

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["runner"] == "runner-a"
    assert body["ready"] is True
    assert captured["server"] == "runner-a"
    assert captured["command"] == SANDBOX_PREFLIGHT_SCRIPT


def test_endpoint_unreachable_runner_is_all_unknown_not_ready(api_client):
    client, main_module = api_client
    main_module.app_state.config.codex_runner_server = "runner-a"

    async def fake_ssh_run(server_name, command, timeout):
        raise RuntimeError("SSH 到 runner-a 失敗")

    main_module.app_state.ssh_run = fake_ssh_run

    response = client.get("/codex-runner/sandbox-preflight")

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["ready"] is False
    assert "error" in body
    assert all(c["status"] == "unknown" for c in body["checks"].values())
