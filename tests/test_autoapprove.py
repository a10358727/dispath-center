"""app/autoapprove.py：自動核准規則引擎（PLAN.md K.3，階段 10）。

涵蓋 PLAN.md K.6 測試清單裡跟規則引擎有關的部分：
- 命中/AND/OR/regex 從頭比對
- 檔案不存在/解析失敗降級
- mtime 重載

危險指令的黑名單檢查發生在 `app.approvals.request_enqueue_approval()`，
不在這個模組的職責範圍——見 tests/test_approvals.py 與 tests/test_api.py
的「危險指令任何規則下都被擋」測試。
"""

from __future__ import annotations

import os

from app.autoapprove import evaluate, get_rules, load_rules, match_rule


# ---------------------------------------------------------------------------
# load_rules：檔案不存在/解析失敗/格式不對 -> 降級為空列表，不擋服務
# ---------------------------------------------------------------------------


def test_load_rules_file_missing_returns_empty(tmp_path):
    assert load_rules(str(tmp_path / "nope.yaml")) == []


def test_load_rules_invalid_yaml_returns_empty(tmp_path):
    p = tmp_path / "auto_approve.yaml"
    p.write_text("rules: [this is not: valid: yaml: at all", encoding="utf-8")
    assert load_rules(str(p)) == []


def test_load_rules_top_level_not_a_dict_returns_empty(tmp_path):
    p = tmp_path / "auto_approve.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert load_rules(str(p)) == []


def test_load_rules_missing_rules_key_returns_empty(tmp_path):
    p = tmp_path / "auto_approve.yaml"
    p.write_text("something_else: true\n", encoding="utf-8")
    assert load_rules(str(p)) == []


def test_load_rules_rules_not_a_list_returns_empty(tmp_path):
    p = tmp_path / "auto_approve.yaml"
    p.write_text("rules: not-a-list\n", encoding="utf-8")
    assert load_rules(str(p)) == []


def test_load_rules_skips_non_dict_items(tmp_path):
    p = tmp_path / "auto_approve.yaml"
    p.write_text(
        "rules:\n  - source: chatgpt\n  - just-a-string\n  - kind: enqueue\n",
        encoding="utf-8",
    )
    rules = load_rules(str(p))
    assert rules == [{"source": "chatgpt"}, {"kind": "enqueue"}]


def test_load_rules_parses_valid_rules(tmp_path):
    p = tmp_path / "auto_approve.yaml"
    p.write_text(
        "rules:\n"
        "  - source: chatgpt\n"
        "    kind: enqueue\n"
        "    command_regex: '^ls '\n",
        encoding="utf-8",
    )
    rules = load_rules(str(p))
    assert rules == [{"source": "chatgpt", "kind": "enqueue", "command_regex": "^ls "}]


# ---------------------------------------------------------------------------
# get_rules：mtime 快取，改檔案要重讀，沒改不用重讀
# ---------------------------------------------------------------------------


def test_get_rules_reloads_when_mtime_changes(tmp_path):
    p = tmp_path / "auto_approve.yaml"
    p.write_text("rules:\n  - source: web\n", encoding="utf-8")
    first = get_rules(str(p))
    assert first == [{"source": "web"}]

    # 強制設一個明確不同的 mtime，避免快速檔案系統寫入間隔太短、mtime 剛好
    # 一樣造成測試 flaky（get_rules() 的重載判斷只看 mtime）。
    new_mtime = os.path.getmtime(str(p)) + 5
    p.write_text("rules:\n  - source: chatgpt\n", encoding="utf-8")
    os.utime(str(p), (new_mtime, new_mtime))

    second = get_rules(str(p))
    assert second == [{"source": "chatgpt"}]


def test_get_rules_does_not_reload_when_mtime_unchanged(tmp_path, monkeypatch):
    import app.autoapprove as autoapprove_module

    p = tmp_path / "auto_approve.yaml"
    p.write_text("rules:\n  - source: web\n", encoding="utf-8")

    calls = {"n": 0}
    real_load_rules = autoapprove_module.load_rules

    def counting_load_rules(path):
        calls["n"] += 1
        return real_load_rules(path)

    monkeypatch.setattr(autoapprove_module, "load_rules", counting_load_rules)

    autoapprove_module.get_rules(str(p))
    autoapprove_module.get_rules(str(p))
    autoapprove_module.get_rules(str(p))
    assert calls["n"] == 1


def test_get_rules_missing_file_returns_empty_and_later_reloads_when_created(tmp_path):
    p = tmp_path / "auto_approve.yaml"
    assert get_rules(str(p)) == []

    p.write_text("rules:\n  - source: web\n", encoding="utf-8")
    assert get_rules(str(p)) == [{"source": "web"}]


# ---------------------------------------------------------------------------
# match_rule：AND（規則裡有填的欄位全部符合）、"any" 萬用、regex 從頭比對
# ---------------------------------------------------------------------------


def test_match_rule_empty_rule_matches_anything():
    assert match_rule({}, source="web", kind="enqueue", command="ls", project=None, pin_server=None)


def test_match_rule_source_must_match_exactly():
    rule = {"source": "chatgpt"}
    assert match_rule(rule, source="chatgpt", kind="enqueue", command=None, project=None, pin_server=None)
    assert not match_rule(rule, source="vllm", kind="enqueue", command=None, project=None, pin_server=None)


def test_match_rule_source_any_is_wildcard():
    rule = {"source": "any"}
    assert match_rule(rule, source="web", kind="enqueue", command=None, project=None, pin_server=None)
    assert match_rule(rule, source="chatgpt", kind="stop", command=None, project=None, pin_server=None)


def test_match_rule_kind_must_match_exactly():
    rule = {"kind": "stop"}
    assert match_rule(rule, source="api", kind="stop", command=None, project=None, pin_server=None)
    assert not match_rule(rule, source="api", kind="enqueue", command=None, project=None, pin_server=None)


def test_match_rule_kind_any_is_wildcard():
    rule = {"kind": "any"}
    assert match_rule(rule, source="api", kind="enqueue", command=None, project=None, pin_server=None)
    assert match_rule(rule, source="api", kind="stop", command=None, project=None, pin_server=None)


def test_match_rule_project_and_pin_server_exact_match():
    rule = {"project": "proj-a", "pin_server": "gpu1"}
    assert match_rule(
        rule, source="api", kind="enqueue", command=None, project="proj-a", pin_server="gpu1"
    )
    assert not match_rule(
        rule, source="api", kind="enqueue", command=None, project="proj-b", pin_server="gpu1"
    )
    assert not match_rule(
        rule, source="api", kind="enqueue", command=None, project="proj-a", pin_server="gpu2"
    )


def test_match_rule_all_filled_fields_must_and_together():
    rule = {"source": "chatgpt", "kind": "enqueue", "project": "proj-a"}
    # source 對、kind 對，但 project 不對 -> 不命中（AND，不是只要有一項符合）
    assert not match_rule(
        rule, source="chatgpt", kind="enqueue", command=None, project="other", pin_server=None
    )
    assert match_rule(
        rule, source="chatgpt", kind="enqueue", command=None, project="proj-a", pin_server=None
    )


def test_match_rule_command_regex_matches_from_start():
    rule = {"command_regex": "^ls "}
    assert match_rule(rule, source="api", kind="enqueue", command="ls -la /tmp", project=None, pin_server=None)
    # re.match 是從頭比對，指令中間才出現 "ls " 不算命中
    assert not match_rule(
        rule, source="api", kind="enqueue", command="echo hi && ls -la", project=None, pin_server=None
    )


def test_match_rule_command_regex_none_command_does_not_match():
    """kind=stop 的 approval 沒有 command（一律是 None）：填了 command_regex
    的規則對 stop 請求只會比對失敗，不會誤命中（不是拋例外）。"""
    rule = {"command_regex": "^ls "}
    assert not match_rule(rule, source="api", kind="stop", command=None, project=None, pin_server=None)


def test_match_rule_invalid_regex_never_matches_and_logs(caplog):
    rule = {"command_regex": "("}  # 不合法的 regex
    with caplog.at_level("WARNING"):
        result = match_rule(
            rule, source="api", kind="enqueue", command="anything", project=None, pin_server=None
        )
    assert result is False
    assert any("command_regex" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# evaluate：規則之間 OR，取第一條命中的索引；沒有命中回 None
# ---------------------------------------------------------------------------


def test_evaluate_returns_first_matching_index():
    rules = [
        {"source": "web"},
        {"source": "chatgpt"},
        {"source": "any"},
    ]
    assert evaluate(rules, source="chatgpt", kind="enqueue", command="ls") == 1


def test_evaluate_returns_none_when_nothing_matches():
    rules = [{"source": "web"}, {"source": "chatgpt"}]
    assert evaluate(rules, source="vllm", kind="enqueue", command="ls") is None


def test_evaluate_empty_rules_returns_none():
    assert evaluate([], source="web", kind="enqueue") is None
