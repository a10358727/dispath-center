"""階段 8 第一批：Project Inventory 唯讀掃描器（PLAN.md I.2 節）。

涵蓋 I.11 測試清單（第一批範圍）：marker 判斷、embedded dataset 只統計不讀
內容、git remote sanitize、禁止路徑清單（Fable 裁定版兩級規則：純系統目錄
精確符合與子路徑都禁止；`/home`／`~` 只禁止精確符合本身，不禁止子路徑）、
secret 檔案不產生對應指令、README 8KB 截斷、`exclude_names` 的 `-prune`
生效。全部用假的 `ssh_run`，不依賴真實 SSH。
"""

from __future__ import annotations

import asyncio

import pytest

from app.inventory import (
    DEFAULT_EMBEDDED_DATASET_NAMES,
    DEFAULT_EXCLUDE_NAMES,
    FORBIDDEN_SCAN_ROOTS,
    MARKER_FILES,
    MAX_READ_BYTES,
    CandidateResult,
    build_embedded_dataset_scan_command,
    build_find_command,
    build_git_info_commands,
    build_readme_read_command,
    estimate_confidence,
    guess_command,
    is_forbidden_root,
    parse_embedded_dataset_output,
    parse_find_output,
    prune_nested_candidates,
    sanitize_git_remote,
    scan_server,
    should_skip_secret,
)


# ---------------------------------------------------------------------------
# is_forbidden_root：禁止路徑清單（Fable 裁定版：兩級規則）
#
# - 純系統目錄（`/`、`/etc`、`/var`、`/root`、`/usr`、`/opt`、`/tmp`）：精確
#   符合或其子路徑都禁止。
# - `/home`／`~`：只禁止精確符合本身，**不禁止子路徑**——`/home/<user>/...`
#   與 `~/...` 是這個功能唯一合理的真實使用情境（原規格這裡寫太嚴，會擋掉
#   規格自己舉例的 `~/projects`，Fable 已裁定修正）。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root", sorted(FORBIDDEN_SCAN_ROOTS))
def test_is_forbidden_root_rejects_each_literal_root(root):
    """`FORBIDDEN_SCAN_ROOTS` 每一項本身（精確符合）都禁止，包含 `/home`
    與 `~` 這兩個「只擋精確符合本身」的項目。"""
    assert is_forbidden_root(root) is True


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/etc/ssh/sshd_config",
        "/var/log",
        "/root/.ssh",
        "/usr/local/bin",
        "/opt/foo",
        "/tmp/x",
        "/tmp/x/y/z",
    ],
)
def test_is_forbidden_root_rejects_subpaths_of_pure_system_dirs(path):
    """純系統目錄（不含 `/home`／`~`）：子路徑一律禁止。"""
    assert is_forbidden_root(path) is True


@pytest.mark.parametrize("path", ["~", "~/", "/home", "/home/"])
def test_is_forbidden_root_rejects_bare_home_forms(path):
    """裸 `~`／`~/`（等於整個家目錄）與 `/home`／`/home/`（列出所有使用者）
    本身禁止。"""
    assert is_forbidden_root(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "~/projects",
        "~/workspace",
        "/home/someuser/projects",
        "/home/otheruser/workspace/ml",
    ],
)
def test_is_forbidden_root_allows_home_subpaths(path):
    """`~/...` 與 `/home/<user>/...` 是這個功能唯一合理的真實使用情境，
    **只擋 `/home`／`~` 本身，不擋子路徑**（Fable 裁定，取代原本過嚴的
    「/home 底下全部禁止」規則）。"""
    assert is_forbidden_root(path) is False


def test_is_forbidden_root_tilde_check_does_not_use_local_expanduser(monkeypatch):
    """`~` 的比對刻意不用 `os.path.expanduser()`——那是展開 Server A 本機
    當前使用者的家目錄，跟遠端工作機的路徑語意完全無關。這裡把 Server A
    本機的 `$HOME` 指向一個系統禁止目錄（`/etc`），如果實作誤用
    `os.path.expanduser()`，`~/projects` 會被展開成 `/etc/projects`（系統
    目錄子路徑，禁止）；正確實作（純字串比對）不受影響，仍然允許。"""
    monkeypatch.setenv("HOME", "/etc")
    assert is_forbidden_root("~/projects") is False


@pytest.mark.parametrize("path", ["", "   ", None])
def test_is_forbidden_root_rejects_empty(path):
    assert is_forbidden_root(path) is True


def test_is_forbidden_root_allows_scoped_absolute_paths():
    assert is_forbidden_root("/data/projects") is False
    assert is_forbidden_root("/srv/training/projects") is False


def test_is_forbidden_root_allows_relative_paths():
    """相對路徑（不是 `/` 或 `~` 開頭）落在遠端使用者自己的 home 目錄底下，
    不在硬編碼禁止清單的任何一個絕對路徑前綴之下。"""
    assert is_forbidden_root("projects") is False
    assert is_forbidden_root("workspace/ml-projects") is False


def test_is_forbidden_root_does_not_reject_everything_via_root_prefix():
    """`/` 本身被禁止，但不能因為 `/` 在清單裡就把所有絕對路徑都判為禁止
    ——各個別禁止目錄各自判斷，允許的絕對路徑（不落在任何禁止目錄底下）
    要正確放行。"""
    assert is_forbidden_root("/") is True
    assert is_forbidden_root("/data") is False


# ---------------------------------------------------------------------------
# should_skip_secret：秘密檔案黑名單，涵蓋所有 pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        ".env",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "id_ed25519_backup",
        "server.pem",
        "private.key",
        "secrets.yaml",
        "secrets_prod.json",
        "credentials.json",
        "credentials_backup",
    ],
)
def test_should_skip_secret_matches_all_patterns(filename):
    assert should_skip_secret(filename) is True


@pytest.mark.parametrize(
    "filename",
    ["README.md", "train.py", "requirements.txt", "pyproject.toml", "package.json"],
)
def test_should_skip_secret_does_not_match_normal_files(filename):
    assert should_skip_secret(filename) is False


def test_should_skip_secret_uses_basename_not_full_path():
    assert should_skip_secret("/data/projects/proj1/.env") is True
    assert should_skip_secret("/data/projects/proj1/README.md") is False


# ---------------------------------------------------------------------------
# sanitize_git_remote：多種 URL 格式
# ---------------------------------------------------------------------------


def test_sanitize_git_remote_masks_https_token():
    assert (
        sanitize_git_remote("https://ghp_abc123@github.com/user/repo.git")
        == "https://***@github.com/user/repo.git"
    )


def test_sanitize_git_remote_masks_userinfo_with_password():
    assert (
        sanitize_git_remote("https://user:password@gitlab.example.com/group/repo.git")
        == "https://***@gitlab.example.com/group/repo.git"
    )


def test_sanitize_git_remote_leaves_ssh_shorthand_unchanged():
    assert sanitize_git_remote("git@github.com:user/repo.git") == "git@github.com:user/repo.git"


def test_sanitize_git_remote_leaves_plain_https_unchanged():
    assert (
        sanitize_git_remote("https://github.com/user/repo.git")
        == "https://github.com/user/repo.git"
    )


def test_sanitize_git_remote_leaves_ssh_scheme_without_userinfo_unchanged():
    assert sanitize_git_remote("ssh://gitserver/repo.git") == "ssh://gitserver/repo.git"


def test_sanitize_git_remote_handles_none_and_empty():
    assert sanitize_git_remote(None) is None
    assert sanitize_git_remote("") == ""


# ---------------------------------------------------------------------------
# build_find_command / parse_find_output：組裝與往返；exclude_names 的
# -prune 出現在指令裡
# ---------------------------------------------------------------------------


def test_build_find_command_includes_prune_for_exclude_names():
    cmd = build_find_command("/data/projects", DEFAULT_EXCLUDE_NAMES)
    assert "-prune" in cmd
    for name in DEFAULT_EXCLUDE_NAMES:
        assert f"-name {name}" in cmd or f"-name '{name}'" in cmd


def test_default_exclude_names_includes_p1_2_additions():
    """PLAN.md P.1.2：巢狀噪音清理追加的四個目錄名，且照樣會被組進
    `find` 的 `-prune` 子句（沿用上面 test_build_find_command_includes_
    prune_for_exclude_names 的斷言邏輯，這裡只額外鎖定這四個新名字）。"""
    for name in [".pytest_cache", ".ipynb_checkpoints", ".idea", ".vscode"]:
        assert name in DEFAULT_EXCLUDE_NAMES
    cmd = build_find_command("/data/projects", DEFAULT_EXCLUDE_NAMES)
    for name in [".pytest_cache", ".ipynb_checkpoints", ".idea", ".vscode"]:
        assert f"-name {name}" in cmd or f"-name '{name}'" in cmd


def test_build_find_command_includes_all_marker_files():
    cmd = build_find_command("/data/projects", [])
    for marker in MARKER_FILES:
        assert marker in cmd


def test_build_find_command_no_prune_clause_when_exclude_names_empty():
    cmd = build_find_command("/data/projects", [])
    assert "-prune" not in cmd


def test_build_find_command_respects_max_depth():
    cmd = build_find_command("/data/projects", [], max_depth=2)
    assert "-maxdepth 3" in cmd  # marker 比候選目錄深一層


def test_build_find_command_never_contains_cat_or_write_redirection():
    """find 指令本身絕不讀取檔案內容或寫入任何東西。"""
    cmd = build_find_command("/data/projects", DEFAULT_EXCLUDE_NAMES)
    assert " cat " not in cmd
    assert ">" not in cmd or "2>/dev/null" in cmd


def test_parse_find_output_groups_markers_by_directory():
    text = (
        "/data/projects/proj1/.git\n"
        "/data/projects/proj1/README.md\n"
        "/data/projects/proj1/requirements.txt\n"
        "/data/projects/proj2/train.py\n"
        "\n"
    )
    result = parse_find_output(text)
    by_path = {r["path"]: r["markers"] for r in result}
    assert by_path["/data/projects/proj1"] == [".git", "README.md", "requirements.txt"]
    assert by_path["/data/projects/proj2"] == ["train.py"]


def test_parse_find_output_handles_empty_input():
    assert parse_find_output("") == []
    assert parse_find_output("   \n  \n") == []


def test_find_command_and_parse_round_trip_is_stable():
    """組指令與解析輸出的欄位命名要一致（往返測試：假設的 find 輸出格式跟
    parse_find_output() 的假設吻合）。"""
    cmd = build_find_command("/data/projects", DEFAULT_EXCLUDE_NAMES)
    assert cmd.startswith("find ")
    fake_output = "/data/projects/proj1/.git\n"
    parsed = parse_find_output(fake_output)
    assert parsed == [{"path": "/data/projects/proj1", "markers": [".git"]}]


# ---------------------------------------------------------------------------
# README 8KB 截斷
# ---------------------------------------------------------------------------


def test_build_readme_read_command_uses_head_c_8192():
    cmd = build_readme_read_command("/data/projects/proj1/README.md")
    assert f"head -c {MAX_READ_BYTES}" in cmd
    assert "8192" in cmd


def test_build_readme_read_command_returns_none_for_secret_filenames():
    assert build_readme_read_command("/data/projects/proj1/.env") is None


def test_build_readme_read_command_never_reads_more_than_max_bytes_conceptually():
    """組指令只用 head -c，不會有 cat 整份檔案的指令混進來。"""
    cmd = build_readme_read_command("/data/projects/proj1/README.md")
    assert cmd is not None
    assert " cat " not in cmd


# ---------------------------------------------------------------------------
# build_git_info_commands
# ---------------------------------------------------------------------------


def test_build_git_info_commands_has_three_readonly_commands_with_fallback():
    cmds = build_git_info_commands("/data/projects/proj1")
    assert set(cmds.keys()) == {"branch", "commit", "remote"}
    assert "git -C" in cmds["branch"] and "branch --show-current" in cmds["branch"]
    assert "rev-parse HEAD" in cmds["commit"]
    assert "remote get-url origin" in cmds["remote"]
    for cmd in cmds.values():
        assert "|| true" in cmd


# ---------------------------------------------------------------------------
# embedded dataset：只統計，不讀內容
# ---------------------------------------------------------------------------


def test_build_embedded_dataset_scan_command_never_cats_file_contents():
    cmd = build_embedded_dataset_scan_command(
        "/data/projects/proj1", DEFAULT_EMBEDDED_DATASET_NAMES, DEFAULT_EXCLUDE_NAMES
    )
    assert " cat " not in cmd
    assert "head -c" not in cmd
    # 只有統計類指令
    assert "du -sb" in cmd
    assert "wc -l" in cmd


def test_build_embedded_dataset_scan_command_includes_all_default_names():
    cmd = build_embedded_dataset_scan_command(
        "/data/projects/proj1", DEFAULT_EMBEDDED_DATASET_NAMES, DEFAULT_EXCLUDE_NAMES
    )
    for name in DEFAULT_EMBEDDED_DATASET_NAMES:
        assert name in cmd


def test_parse_embedded_dataset_output_parses_multiple_entries():
    text = (
        "PATH:/data/projects/proj1/data\n"
        "FILES:120\n"
        "BYTES:104857600\n"
        "TOP:train,val,test,\n"
        "EXT:jpg:100,txt:20,\n"
        "---\n"
        "PATH:/data/projects/proj1/dataset\n"
        "FILES:5\n"
        "BYTES:1024\n"
        "TOP:readme.txt,\n"
        "EXT:txt:5,\n"
        "---\n"
    )
    parsed = parse_embedded_dataset_output(text)
    assert len(parsed) == 2
    assert parsed[0]["path"] == "/data/projects/proj1/data"
    assert parsed[0]["file_count"] == 120
    assert parsed[0]["size_bytes"] == 104857600
    assert parsed[0]["top_level_names"] == ["train", "val", "test"]
    assert parsed[0]["extension_summary"] == {"jpg": 100, "txt": 20}
    assert parsed[1]["file_count"] == 5


def test_parse_embedded_dataset_output_handles_empty_output():
    assert parse_embedded_dataset_output("") == []


def test_parse_embedded_dataset_output_tolerates_missing_bytes_value():
    """`du -sb` 對不存在/沒有權限的目錄可能輸出空字串，容錯不炸掉。"""
    text = "PATH:/data/x\nFILES:0\nBYTES:\nTOP:\nEXT:\n---\n"
    parsed = parse_embedded_dataset_output(text)
    assert parsed[0]["size_bytes"] == 0
    assert parsed[0]["file_count"] == 0


# ---------------------------------------------------------------------------
# confidence / command 猜測
# ---------------------------------------------------------------------------


def test_estimate_confidence_higher_with_more_markers():
    low = estimate_confidence(["scripts"])
    high = estimate_confidence([".git", "README.md", "requirements.txt"])
    assert 0.0 < low < high <= 0.95


def test_estimate_confidence_empty_markers_is_low():
    assert estimate_confidence([]) == 0.1


def test_guess_command_train_py():
    assert guess_command(["train.py"], "/data/x") == "python train.py"


def test_guess_command_no_entrypoint_returns_none():
    assert guess_command(["package.json"], "/data/x") is None
    assert guess_command([], "/data/x") is None


# ---------------------------------------------------------------------------
# scan_server：主流程，注入假 ssh_run
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout


class RecordingFakeSSH:
    def __init__(self, responses):
        """`responses`：list of (predicate(cmd)->bool, stdout) 依序匹配。"""
        self.responses = responses
        self.calls: list[str] = []

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        for predicate, stdout in self.responses:
            if predicate(command):
                return FakeResult(stdout)
        return FakeResult("")


def test_scan_server_skips_forbidden_roots_without_calling_ssh():
    ssh = RecordingFakeSSH([])
    result = asyncio.run(scan_server(ssh, "server-a", ["/etc", "/tmp"]))
    assert result == []
    assert ssh.calls == []


def test_scan_server_builds_candidates_from_find_output():
    ssh = RecordingFakeSSH(
        [
            (lambda c: c.startswith("find"), "/data/projects/proj1/.git\n/data/projects/proj1/README.md\n"),
            (lambda c: "head -c" in c, "# Proj1\nA test project.\n"),
            (lambda c: "branch --show-current" in c, "main\n"),
            (lambda c: "rev-parse HEAD" in c, "deadbeef\n"),
            (lambda c: "remote get-url origin" in c, "https://tok@github.com/x/y.git\n"),
            (lambda c: c.startswith("for d in"), ""),
        ]
    )
    result = asyncio.run(scan_server(ssh, "server-a", ["/data/projects"]))
    assert len(result) == 1
    cand = result[0]
    assert isinstance(cand, CandidateResult)
    assert cand.server == "server-a"
    assert cand.path == "/data/projects/proj1"
    assert cand.name_guess == "proj1"
    assert cand.git_branch == "main"
    assert cand.git_commit == "deadbeef"
    assert cand.git_remote == "https://***@github.com/x/y.git"
    assert cand.readme_excerpt.startswith("# Proj1")
    assert cand.confidence > 0.5


def test_scan_server_continues_when_one_root_find_fails():
    class FailingFirstRootSSH:
        def __init__(self):
            self.calls: list[str] = []

        async def __call__(self, server_name, command, timeout):
            self.calls.append(command)
            if "roota" in command:
                raise ConnectionError("simulated ssh failure")
            if command.startswith("find"):
                return FakeResult("")
            return FakeResult("")

    ssh = FailingFirstRootSSH()
    result = asyncio.run(scan_server(ssh, "server-a", ["/data/roota", "/data/rootb"]))
    assert result == []
    # 兩個 root 都嘗試過（第一個失敗不影響第二個繼續）
    assert any("roota" in c for c in ssh.calls)
    assert any("rootb" in c for c in ssh.calls)


def test_scan_server_never_reads_embedded_dataset_file_contents():
    """完整跑一次 scan_server，確認過程中送出的所有指令都沒有讀資料檔案
    內容（沒有針對 embedded dataset 目錄的 cat/head）。"""
    ssh = RecordingFakeSSH(
        [
            (lambda c: c.startswith("find"), "/data/projects/proj1/train.py\n"),
            (lambda c: c.startswith("for d in"), "PATH:/data/projects/proj1/data\nFILES:3\nBYTES:900\nTOP:a.jpg,\nEXT:jpg:3,\n---\n"),
        ]
    )
    result = asyncio.run(scan_server(ssh, "server-a", ["/data/projects"]))
    assert len(result) == 1
    assert result[0].embedded_data_paths == ["/data/projects/proj1/data"]
    assert result[0].estimated_data_bytes == 900
    assert result[0].command_guess == "python train.py"
    for cmd in ssh.calls:
        assert " cat " not in cmd


# ---------------------------------------------------------------------------
# prune_nested_candidates：巢狀候選剔除（PLAN.md P.1.2）
# ---------------------------------------------------------------------------


def _cand(path: str) -> CandidateResult:
    """輔助函式：只填 `prune_nested_candidates()` 關心的 `path`，其餘欄位
    用最小合法值——這條純函式不看 path 以外的任何欄位。"""
    return CandidateResult(
        server="server-a",
        path=path,
        name_guess=path.rsplit("/", 1)[-1],
        kind="project",
        git_remote=None,
        git_branch=None,
        git_commit=None,
    )


def test_prune_nested_candidates_removes_direct_child():
    candidates = [_cand("/data/proj/data"), _cand("/data/proj"), _cand("/data/proj/scripts")]
    kept = prune_nested_candidates(candidates)
    assert [c.path for c in kept] == ["/data/proj"]


def test_prune_nested_candidates_does_not_misjudge_sibling_with_shared_prefix():
    """`/a/bc` 不是 `/a/b` 的子目錄——純字串前綴比對會誤判，必須確認前綴
    後面接的是路徑分隔符號。"""
    candidates = [_cand("/a/b"), _cand("/a/bc")]
    kept = prune_nested_candidates(candidates)
    assert sorted(c.path for c in kept) == ["/a/b", "/a/bc"]


def test_prune_nested_candidates_multi_level_nesting_keeps_only_top():
    candidates = [
        _cand("/data/proj"),
        _cand("/data/proj/sub"),
        _cand("/data/proj/sub/deeper"),
        _cand("/data/proj/sub/deeper/.pytest_cache"),
    ]
    kept = prune_nested_candidates(candidates)
    assert [c.path for c in kept] == ["/data/proj"]


def test_prune_nested_candidates_keeps_unrelated_candidates():
    candidates = [_cand("/data/proj-a"), _cand("/data/proj-b"), _cand("/other/proj-c")]
    kept = prune_nested_candidates(candidates)
    assert sorted(c.path for c in kept) == ["/data/proj-a", "/data/proj-b", "/other/proj-c"]


def test_prune_nested_candidates_handles_empty_input():
    assert prune_nested_candidates([]) == []


def test_prune_nested_candidates_accepts_dict_shaped_items():
    """輸入形狀相容帶 `path` 鍵的 dict（例如 `parse_find_output()` 的
    產出），不是只認 `CandidateResult`。"""
    candidates = [{"path": "/data/proj/data"}, {"path": "/data/proj"}]
    kept = prune_nested_candidates(candidates)
    assert kept == [{"path": "/data/proj"}]


def test_prune_nested_candidates_trailing_slash_normalized():
    candidates = [_cand("/data/proj/"), _cand("/data/proj/data")]
    kept = prune_nested_candidates(candidates)
    assert [c.path for c in kept] == ["/data/proj/"]
