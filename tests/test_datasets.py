"""階段 3：專案／資料集註冊表、sync、資料引力（PLAN.md D 節）。

涵蓋 D 節「測試」清單：資料引力挑選、空間檢查邏輯、manifest 驗證、sync
依賴自動掛載、快取地圖登記/校正（FakeSSH，不依賴真實 rsync 與 SSH）。
"""

from __future__ import annotations

import asyncio
import shlex

import pytest

from app.datasets import (
    NO_CARD_NOTE,
    InvalidDatasetCardError,
    InvalidNameError,
    build_dataset_auto_facts,
    build_dataset_card,
    build_dispatch_plan,
    build_manifest,
    build_remote_manifest_check_command,
    build_setup_script,
    build_ssh_opts,
    build_sync_script,
    check_disk_space,
    dataset_remote_dir,
    finalize_sync_job,
    has_enough_space,
    make_has_dataset,
    parse_dataset_ls_output,
    parse_remote_manifest_check,
    reconcile_server_dataset_cache,
    render_dataset_card,
    validate_card_fields,
    validate_name_component,
    verify_manifest_match,
)
from app.db import Project
from app.jobqueue import enqueue_job


# ---------------------------------------------------------------------------
# 字元集驗證：資料集名/版本/專案名限 [A-Za-z0-9._-]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["defect-v3", "resnet50", "v1.2", "a_b-c.1"])
def test_validate_name_component_accepts_safe_chars(value):
    assert validate_name_component(value) == value


@pytest.mark.parametrize(
    "value",
    ["../etc/passwd", "name with space", "a;rm -rf /", "a$(whoami)", "a`id`", ""],
)
def test_validate_name_component_rejects_unsafe_chars(value):
    with pytest.raises(InvalidNameError):
        validate_name_component(value)


def test_dataset_remote_dir_validates_and_builds_path():
    assert dataset_remote_dir("defect", "v1") == "datasets/defect/v1"
    with pytest.raises(InvalidNameError):
        dataset_remote_dir("defect; rm -rf /", "v1")


# ---------------------------------------------------------------------------
# manifest：掃描本機目錄
# ---------------------------------------------------------------------------


def test_build_manifest_scans_files_and_sums_size(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 200)

    manifest = build_manifest(str(tmp_path))
    assert manifest["file_count"] == 2
    assert manifest["total_size"] == 300
    paths = {f["path"] for f in manifest["files"]}
    assert paths == {"a.bin", "sub/b.bin"}


def test_build_manifest_missing_dir_raises():
    with pytest.raises(ValueError):
        build_manifest("/no/such/path/should/not/exist")


def test_verify_manifest_match_success_and_mismatch():
    manifest = {"file_count": 2, "total_size": 300}
    ok, reason = verify_manifest_match(manifest, 2, 300)
    assert ok is True
    assert reason == ""

    ok, reason = verify_manifest_match(manifest, 1, 300)
    assert ok is False
    assert "檔案數不符" in reason

    ok, reason = verify_manifest_match(manifest, 2, 999)
    assert ok is False
    assert "總大小不符" in reason


# ---------------------------------------------------------------------------
# rsync / setup 指令組裝（純函式，不真的執行）
# ---------------------------------------------------------------------------


def test_build_sync_script_contains_mkdir_and_rsync_with_correct_dest():
    script = build_sync_script(
        "/data/defect/v1", "train", "10.0.0.5", "datasets/defect/v1", "~/.ssh/id_rsa"
    )
    assert "mkdir -p datasets/defect/v1" in script
    assert "rsync -a --partial --info=progress2" in script
    assert "train@10.0.0.5:datasets/defect/v1/" in script
    assert "-i" in script and "id_rsa" in script


def test_build_sync_script_quotes_source_path_with_spaces():
    script = build_sync_script(
        "/data/has space/v1", "train", "10.0.0.5", "datasets/x/v1", "~/.ssh/id_rsa"
    )
    assert "'/data/has space/v1/'" in script


def test_build_ssh_opts_default_port_matches_legacy_output():
    assert build_ssh_opts("~/.ssh/id_rsa") == (
        f"ssh -i {shlex.quote('~/.ssh/id_rsa')} -o StrictHostKeyChecking=no -o BatchMode=yes"
    )
    assert build_ssh_opts("~/.ssh/id_rsa", port=22) == build_ssh_opts("~/.ssh/id_rsa")


def test_build_ssh_opts_non_default_port_appends_dash_p():
    opts = build_ssh_opts("~/.ssh/id_rsa", port=32221)
    assert opts.endswith(" -p 32221")


def test_build_sync_script_non_default_port_appears_in_mkdir_and_rsync_e():
    script = build_sync_script(
        "/data/defect/v1",
        "train",
        "10.0.0.9",
        "datasets/defect/v1",
        "~/.ssh/id_rsa",
        port=32221,
    )
    remote_mkdir, rsync_cmd = script.split(" && ", 1)
    assert "-p 32221" in remote_mkdir
    assert "-p 32221" in rsync_cmd


def test_build_sync_script_default_port_matches_legacy_output():
    script_with_default = build_sync_script(
        "/data/defect/v1", "train", "10.0.0.5", "datasets/defect/v1", "~/.ssh/id_rsa", port=22
    )
    script_without_port_arg = build_sync_script(
        "/data/defect/v1", "train", "10.0.0.5", "datasets/defect/v1", "~/.ssh/id_rsa"
    )
    assert script_with_default == script_without_port_arg
    assert "-p 22" not in script_without_port_arg


def test_build_setup_script_clones_and_runs_setup_cmd():
    script = build_setup_script("resnet50", "git@github.com:x/resnet50.git", "pip install -e .")
    assert "git clone" in script
    assert "projects/resnet50" in script
    assert "pip install -e ." in script


def test_build_setup_script_rejects_unsafe_project_name():
    with pytest.raises(InvalidNameError):
        build_setup_script("bad name", "repo", None)


# ---------------------------------------------------------------------------
# df 剩餘空間檢查（純函式 has_enough_space + async check_disk_space）
# ---------------------------------------------------------------------------


def test_has_enough_space():
    dataset_size = 100 * 1024 * 1024 * 1024  # 100 GB
    assert has_enough_space(int(dataset_size * 1.3), dataset_size) is True
    assert has_enough_space(int(dataset_size * 1.1), dataset_size) is False
    assert has_enough_space(None, dataset_size) is False


class FakeCommandResult:
    def __init__(self, stdout: str):
        self.stdout = stdout


class FakeSSH:
    def __init__(self, responses: dict[str, str], unreachable: bool = False):
        self.responses = responses
        self.unreachable = unreachable
        self.calls: list[str] = []

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if self.unreachable:
            raise ConnectionError("simulated unreachable")
        for key, value in self.responses.items():
            if key in command:
                return FakeCommandResult(value)
        return FakeCommandResult("")


def test_check_disk_space_sufficient():
    # df -Pk 輸出：Filesystem 1024-blocks Used Available Capacity Mounted
    ssh = FakeSSH({"df -Pk": "/dev/sda1 100000000 1000000 99000000 2% /\n"})
    ok, reason, avail = asyncio.run(check_disk_space(ssh, "server-a", 1024 * 1024))
    assert ok is True
    assert avail == 99000000 * 1024


def test_check_disk_space_insufficient():
    ssh = FakeSSH({"df -Pk": "/dev/sda1 100000000 99000000 1000000 99% /\n"})
    ok, reason, avail = asyncio.run(
        check_disk_space(ssh, "server-a", 10 * 1024 * 1024 * 1024)
    )
    assert ok is False
    assert "剩餘空間不足" in reason


def test_check_disk_space_unreachable():
    ssh = FakeSSH({}, unreachable=True)
    ok, reason, avail = asyncio.run(check_disk_space(ssh, "server-a", 1024))
    assert ok is False
    assert avail is None
    assert "SSH 連不上" in reason


def test_check_disk_space_unparsable_output():
    ssh = FakeSSH({"df -Pk": "garbage output\n"})
    ok, reason, avail = asyncio.run(check_disk_space(ssh, "server-a", 1024))
    assert ok is False
    assert avail is None


# ---------------------------------------------------------------------------
# 同步後 manifest 檢查指令組裝與解析
# ---------------------------------------------------------------------------


def test_build_and_parse_remote_manifest_check_roundtrip():
    cmd = build_remote_manifest_check_command("datasets/defect/v1")
    assert "find" in cmd and "datasets/defect/v1" in cmd

    output = "COUNT:42\nSIZE:123456\n"
    count, size = parse_remote_manifest_check(output)
    assert count == 42
    assert size == 123456


def test_parse_remote_manifest_check_garbled_defaults_to_zero():
    count, size = parse_remote_manifest_check("nonsense output\n")
    assert count == 0
    assert size == 0


# ---------------------------------------------------------------------------
# finalize_sync_job：manifest 比對通過才登記快取；不通過改判 failed
# ---------------------------------------------------------------------------


def test_finalize_sync_job_success_registers_cache(db, audit_path):
    db.insert_dataset(
        "defect", "v1", 300, "/data/defect/v1", {"file_count": 2, "total_size": 300}
    )
    job = enqueue_job(db, command="true", type="sync", pin_server="_local", audit_path=audit_path)
    db.update_job(
        job.id,
        status="running",
        server="_local",
        target_server="server-a",
        dataset_name="defect",
        dataset_version="v1",
    )
    job = db.get_job(job.id)

    ssh = FakeSSH({"find": "COUNT:2\nSIZE:300\n"})
    asyncio.run(finalize_sync_job(db, job, 0, "rsync done\n", ssh, audit_path=audit_path))

    updated = db.get_job(job.id)
    assert updated.status == "done"
    assert db.is_dataset_cached("server-a", "defect", "v1") is True


def test_finalize_sync_job_mismatch_marks_failed_and_no_cache(db, audit_path):
    db.insert_dataset(
        "defect", "v1", 300, "/data/defect/v1", {"file_count": 2, "total_size": 300}
    )
    job = enqueue_job(db, command="true", type="sync", pin_server="_local", audit_path=audit_path)
    db.update_job(
        job.id,
        status="running",
        server="_local",
        target_server="server-a",
        dataset_name="defect",
        dataset_version="v1",
    )
    job = db.get_job(job.id)

    # 只同步過去 1 個檔案，跟 manifest 記錄的 2 個對不上
    ssh = FakeSSH({"find": "COUNT:1\nSIZE:100\n"})
    asyncio.run(finalize_sync_job(db, job, 0, "rsync done\n", ssh, audit_path=audit_path))

    updated = db.get_job(job.id)
    assert updated.status == "failed"
    assert db.is_dataset_cached("server-a", "defect", "v1") is False
    assert "驗證失敗" in (updated.log_tail or "")


def test_finalize_sync_job_ssh_unreachable_marks_failed(db, audit_path):
    db.insert_dataset(
        "defect", "v1", 300, "/data/defect/v1", {"file_count": 2, "total_size": 300}
    )
    job = enqueue_job(db, command="true", type="sync", pin_server="_local", audit_path=audit_path)
    db.update_job(
        job.id,
        status="running",
        server="_local",
        target_server="server-a",
        dataset_name="defect",
        dataset_version="v1",
    )
    job = db.get_job(job.id)

    ssh = FakeSSH({}, unreachable=True)
    asyncio.run(finalize_sync_job(db, job, 0, "rsync done\n", ssh, audit_path=audit_path))

    updated = db.get_job(job.id)
    assert updated.status == "failed"
    assert db.is_dataset_cached("server-a", "defect", "v1") is False


# ---------------------------------------------------------------------------
# 資料引力：make_has_dataset
# ---------------------------------------------------------------------------


def test_make_has_dataset_true_when_cached(db, audit_path):
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    db.upsert_dataset_cache("server-a", "defect", "v1")
    job = enqueue_job(
        db, command="python train.py", type="train", project="proj", audit_path=audit_path
    )
    has_dataset = make_has_dataset(db, "server-a")
    assert has_dataset(db.get_job(job.id)) is True


def test_make_has_dataset_false_when_not_cached(db, audit_path):
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    job = enqueue_job(
        db, command="python train.py", type="train", project="proj", audit_path=audit_path
    )
    has_dataset = make_has_dataset(db, "server-b")
    assert has_dataset(db.get_job(job.id)) is False


def test_make_has_dataset_true_when_no_dataset_requirement(db, audit_path):
    db.insert_project("proj", "git@x")  # 沒有 dataset_name
    job = enqueue_job(
        db, command="python train.py", type="train", project="proj", audit_path=audit_path
    )
    has_dataset = make_has_dataset(db, "server-a")
    assert has_dataset(db.get_job(job.id)) is True


def test_make_has_dataset_true_for_non_train_jobs(db, audit_path):
    job = enqueue_job(db, command="sleep 60", type="adhoc", audit_path=audit_path)
    has_dataset = make_has_dataset(db, "server-a")
    assert has_dataset(db.get_job(job.id)) is True


# ---------------------------------------------------------------------------
# build_dispatch_plan：sync 依賴自動掛載（核准卡片顯示用的計畫）
# ---------------------------------------------------------------------------


def test_build_dispatch_plan_needs_sync_when_not_cached(db):
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    db.insert_dataset("defect", "v1", 500, "/data/defect/v1", {"file_count": 1, "total_size": 500})
    project = db.get_project("proj")

    plan = build_dispatch_plan(db, project, "server-a")
    assert plan.sync_plan is not None
    assert plan.sync_plan["dataset_name"] == "defect"
    assert plan.sync_plan["size_bytes"] == 500


def test_build_dispatch_plan_no_sync_when_already_cached(db):
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    db.insert_dataset("defect", "v1", 500, "/data/defect/v1", {"file_count": 1, "total_size": 500})
    db.upsert_dataset_cache("server-a", "defect", "v1")
    project = db.get_project("proj")

    plan = build_dispatch_plan(db, project, "server-a")
    assert plan.sync_plan is None


def test_build_dispatch_plan_raises_when_dataset_not_registered(db):
    db.insert_project("proj", "git@x", dataset_name="defect", dataset_version="v1")
    project = db.get_project("proj")

    with pytest.raises(ValueError):
        build_dispatch_plan(db, project, "server-a")


def test_build_dispatch_plan_needs_setup_first_time(db, audit_path):
    db.insert_project("proj", "git@x", setup_cmd="pip install -e .")
    project = db.get_project("proj")

    plan = build_dispatch_plan(db, project, "server-a")
    assert plan.setup_plan is not None
    assert plan.setup_plan["setup_cmd"] == "pip install -e ."


def test_build_dispatch_plan_no_setup_when_already_completed(db, audit_path):
    db.insert_project("proj", "git@x", setup_cmd="pip install -e .")
    project = db.get_project("proj")

    setup_job = enqueue_job(
        db, command="git clone ...", type="setup", project="proj", pin_server="server-a",
        audit_path=audit_path,
    )
    db.update_job(setup_job.id, status="done", server="server-a", exit_code=0)

    plan = build_dispatch_plan(db, project, "server-a")
    assert plan.setup_plan is None


def test_build_dispatch_plan_no_setup_cmd_means_no_plan(db):
    db.insert_project("proj", "git@x")  # 沒有 setup_cmd
    project = db.get_project("proj")
    plan = build_dispatch_plan(db, project, "server-a")
    assert plan.setup_plan is None


# ---------------------------------------------------------------------------
# 每小時快取地圖校正
# ---------------------------------------------------------------------------


def test_parse_dataset_ls_output():
    text = (
        "datasets/defect/v1/\n"
        "datasets/resnet/v2/\n"
        "ls: cannot access 'datasets/*/*/': No such file or directory\n"
        "\n"
    )
    pairs = parse_dataset_ls_output(text)
    assert set(pairs) == {("defect", "v1"), ("resnet", "v2")}


def test_reconcile_server_dataset_cache_adds_and_removes(db):
    db.upsert_dataset_cache("server-a", "stale", "v0")
    found = [("defect", "v1")]

    added, removed = reconcile_server_dataset_cache(db, "server-a", found)
    assert added == {("defect", "v1")}
    assert removed == {("stale", "v0")}

    cache = {(c.dataset, c.version) for c in db.list_dataset_cache(server="server-a")}
    assert cache == {("defect", "v1")}


# ---------------------------------------------------------------------------
# 階段 16（PLAN.md Q.3 節）：資料卡——build_dataset_card / render_dataset_
# card / validate_card_fields / build_dataset_auto_facts
# ---------------------------------------------------------------------------


def test_validate_card_fields_accepts_non_empty_strings():
    validate_card_fields("這是什麼", "怎麼做的")  # 不丟例外


@pytest.mark.parametrize(
    "description,method",
    [
        (None, "method"),
        ("", "method"),
        ("   ", "method"),
        ("desc", None),
        ("desc", ""),
        ("desc", "\n\t "),
    ],
)
def test_validate_card_fields_rejects_empty_or_blank(description, method):
    with pytest.raises(InvalidDatasetCardError):
        validate_card_fields(description, method)


def test_build_dataset_card_shape_minimal():
    card = build_dataset_card("這是什麼", "人工標註")
    assert card["description"] == "這是什麼"
    assert card["method"] == "人工標註"
    assert card["derived_from"] is None
    assert card["counts_custom"] is None
    assert card["created_at"] == card["updated_at"]
    assert card["created_at"]  # 非空


def test_build_dataset_card_strips_whitespace():
    card = build_dataset_card("  desc  ", "  method  ")
    assert card["description"] == "desc"
    assert card["method"] == "method"


def test_build_dataset_card_with_derived_from_and_counts():
    card = build_dataset_card(
        "v2 版本",
        "從 v1 篩選",
        derived_from={"name": "defect", "version": "v1"},
        counts={"train": 800, "val": 200, "note": "80/20 split"},
    )
    assert card["derived_from"] == {"name": "defect", "version": "v1"}
    assert card["counts_custom"] == {"train": 800, "val": 200, "note": "80/20 split"}


def test_build_dataset_card_rejects_missing_description_or_method():
    with pytest.raises(InvalidDatasetCardError):
        build_dataset_card("", "method")
    with pytest.raises(InvalidDatasetCardError):
        build_dataset_card("desc", "")


def test_build_dataset_card_rejects_invalid_counts_type():
    with pytest.raises(InvalidDatasetCardError):
        build_dataset_card("desc", "method", counts={"bad": [1, 2, 3]})
    with pytest.raises(InvalidDatasetCardError):
        build_dataset_card("desc", "method", counts={"bad": {"nested": 1}})
    with pytest.raises(InvalidDatasetCardError):
        build_dataset_card("desc", "method", counts="not a dict")


def test_build_dataset_card_accepts_scalar_count_types():
    card = build_dataset_card(
        "desc", "method", counts={"a": "text", "b": 1, "c": 1.5}
    )
    assert card["counts_custom"] == {"a": "text", "b": 1, "c": 1.5}


def test_build_dataset_auto_facts_shape(db):
    db.insert_dataset(
        name="defect",
        version="v1",
        size_bytes=3072,
        source_path="/data/defect",
        manifest={"file_count": 2, "total_size": 3072, "files": []},
    )
    db.upsert_dataset_cache("server-a", "defect", "v1")
    db.upsert_dataset_cache("server-b", "defect", "v1")
    dataset = db.get_dataset("defect", "v1")

    facts = build_dataset_auto_facts(db, dataset)
    assert facts["file_count"] == 2
    assert facts["total_size_bytes"] == 3072
    assert facts["size_bytes"] == 3072
    assert facts["created_at"] == dataset.created_at
    assert facts["cached_on"] == ["server-a", "server-b"]


def test_build_dataset_auto_facts_no_cache_entries(db):
    db.insert_dataset(
        name="defect",
        version="v1",
        size_bytes=100,
        source_path="/data/defect",
        manifest={"file_count": 1, "total_size": 100, "files": []},
    )
    dataset = db.get_dataset("defect", "v1")
    facts = build_dataset_auto_facts(db, dataset)
    assert facts["cached_on"] == []


def test_render_dataset_card_with_card_is_markdown_prose():
    card = build_dataset_card(
        "缺陷偵測資料集第一版",
        "人工標註，來源為產線攝影機",
        derived_from={"name": "raw-frames", "version": "v3"},
        counts={"train": 800, "val": 200},
    )
    auto_facts = {
        "file_count": 1000,
        "total_size_bytes": 1024_000,
        "size_bytes": 1024_000,
        "created_at": "2026-07-11T00:00:00",
        "cached_on": ["server-a", "server-b"],
    }
    rendered = render_dataset_card("defect", "v1", card, auto_facts)

    assert rendered.startswith("# defect@v1")
    assert "缺陷偵測資料集第一版" in rendered
    assert "人工標註，來源為產線攝影機" in rendered
    assert "1000" in rendered
    assert "1024000" in rendered
    assert "server-a" in rendered and "server-b" in rendered
    assert "train: 800" in rendered
    assert "val: 200" in rendered
    assert "raw-frames@v3" in rendered
    assert NO_CARD_NOTE not in rendered
    # 不是 JSON 堆疊：不應該直接把整個 dict 字面印出來
    assert "{'description'" not in rendered
    assert '"description"' not in rendered


def test_render_dataset_card_without_card_uses_fixed_note_and_still_shows_counts():
    auto_facts = {
        "file_count": 42,
        "total_size_bytes": 999,
        "size_bytes": 999,
        "created_at": "2020-01-01T00:00:00",
        "cached_on": [],
    }
    rendered = render_dataset_card("legacy-ds", "v1", None, auto_facts)

    assert rendered.startswith("# legacy-ds@v1")
    assert rendered.count(NO_CARD_NOTE) == 2  # 描述段與製作方式段都要有
    assert "42" in rendered  # 數量段照出，不受無卡影響
    assert "999" in rendered
    assert "（無，這是原始版本" in rendered  # 無卡也沒有衍生自資訊


def test_render_dataset_card_no_cache_shows_placeholder():
    card = build_dataset_card("desc", "method")
    auto_facts = {
        "file_count": 1,
        "total_size_bytes": 1,
        "size_bytes": 1,
        "created_at": "2026-01-01T00:00:00",
        "cached_on": [],
    }
    rendered = render_dataset_card("d", "v1", card, auto_facts)
    assert "尚無機器快取此版本" in rendered
