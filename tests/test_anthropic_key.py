"""DG-ASSISTANT-CLAUDE-TURN v1 C2: `app.anthropic_key` — shape validation +
atomic `.env` rewrite mechanics. No network, no real Anthropic call, no
route/HTTP layer here (see `tests/test_ai_providers_v2_api.py` for the
endpoint-level tests: key absent from response/audit/logs, 400 on invalid
shape with zero writes)."""

from __future__ import annotations

import os
import stat

import pytest

from app.anthropic_key import (
    ENV_VALUE_VALIDATORS,
    InvalidAnthropicApiKeyError,
    InvalidEnvValueError,
    clear_anthropic_api_key,
    clear_env_var,
    set_anthropic_api_key,
    set_env_var,
    validate_anthropic_api_key,
    validate_model_name,
)

VALID_KEY = "sk-ant-" + "a" * 20


# ---------------------------------------------------------------------------
# Shape validation (pure)
# ---------------------------------------------------------------------------


def test_validate_accepts_well_shaped_key():
    assert validate_anthropic_api_key(VALID_KEY) == VALID_KEY


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        "no-prefix-" + "a" * 20,
        "sk-ant-" + "a" * 400,
        "sk-ant-has space",
        "sk-ant-has\ttab",
        "sk-ant-" + "a" * 5,  # below the 20-char total floor
    ],
)
def test_validate_rejects_malformed_keys(value):
    with pytest.raises(InvalidAnthropicApiKeyError):
        validate_anthropic_api_key(value)


# ---------------------------------------------------------------------------
# Atomic file rewrite
# ---------------------------------------------------------------------------


def test_set_creates_file_when_missing(tmp_path):
    env_path = tmp_path / ".env"
    set_anthropic_api_key(str(env_path), VALID_KEY)
    content = env_path.read_text(encoding="utf-8")
    assert f"ANTHROPIC_API_KEY={VALID_KEY}" in content.splitlines()


def test_set_preserves_other_lines_and_order(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DB_PATH=jobqueue.db\n# a comment\nAUTH_TOKEN=xyz\n", encoding="utf-8"
    )
    set_anthropic_api_key(str(env_path), VALID_KEY)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "DB_PATH=jobqueue.db",
        "# a comment",
        "AUTH_TOKEN=xyz",
        f"ANTHROPIC_API_KEY={VALID_KEY}",
    ]


def test_set_overwrites_existing_key_in_place(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DB_PATH=jobqueue.db\nANTHROPIC_API_KEY=old-value\nAUTH_TOKEN=xyz\n",
        encoding="utf-8",
    )
    new_key = "sk-ant-" + "b" * 30
    set_anthropic_api_key(str(env_path), new_key)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "DB_PATH=jobqueue.db",
        f"ANTHROPIC_API_KEY={new_key}",
        "AUTH_TOKEN=xyz",
    ]
    assert "old-value" not in env_path.read_text(encoding="utf-8")


def test_set_invalid_key_raises_and_writes_nothing(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DB_PATH=jobqueue.db\n", encoding="utf-8")
    original = env_path.read_text(encoding="utf-8")
    with pytest.raises(InvalidAnthropicApiKeyError):
        set_anthropic_api_key(str(env_path), "not-a-real-key")
    assert env_path.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env.bak").exists()


def test_set_creates_single_bak_backup_overwritten_each_time(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DB_PATH=jobqueue.db\n", encoding="utf-8")
    set_anthropic_api_key(str(env_path), VALID_KEY)
    backup_path = tmp_path / ".env.bak"
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == "DB_PATH=jobqueue.db\n"

    second_key = "sk-ant-" + "c" * 30
    set_anthropic_api_key(str(env_path), second_key)
    # still exactly one .bak file, now holding the *previous* .env content
    # (the one with the first key), not a growing timestamped history.
    assert backup_path.exists()
    assert f"ANTHROPIC_API_KEY={VALID_KEY}" in backup_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".env.bak.*"))


def test_set_chmods_the_written_file_600(tmp_path):
    env_path = tmp_path / ".env"
    set_anthropic_api_key(str(env_path), VALID_KEY)
    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    assert mode == 0o600


def test_set_backup_is_chmod_600_even_when_source_file_was_world_readable(tmp_path):
    """A pre-existing `.env` that happens to be world-readable must not
    propagate that mode to the `.bak` copy (the backup can contain the very
    same key this module exists to protect)."""

    env_path = tmp_path / ".env"
    env_path.write_text("DB_PATH=jobqueue.db\n", encoding="utf-8")
    os.chmod(env_path, 0o644)

    set_anthropic_api_key(str(env_path), VALID_KEY)

    env_mode = stat.S_IMODE(os.stat(env_path).st_mode)
    backup_mode = stat.S_IMODE(os.stat(tmp_path / ".env.bak").st_mode)
    assert env_mode == 0o600
    assert backup_mode == 0o600


def test_clear_removes_the_key_line_only(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DB_PATH=jobqueue.db\nANTHROPIC_API_KEY=" + VALID_KEY + "\nAUTH_TOKEN=xyz\n",
        encoding="utf-8",
    )
    clear_anthropic_api_key(str(env_path))
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["DB_PATH=jobqueue.db", "AUTH_TOKEN=xyz"]


def test_clear_is_a_no_op_content_wise_when_key_never_set(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DB_PATH=jobqueue.db\n", encoding="utf-8")
    clear_anthropic_api_key(str(env_path))
    assert env_path.read_text(encoding="utf-8").splitlines() == ["DB_PATH=jobqueue.db"]


def test_clear_when_file_missing_creates_empty_file_without_raising(tmp_path):
    env_path = tmp_path / ".env"
    clear_anthropic_api_key(str(env_path))
    assert env_path.exists()
    assert env_path.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# Packet D2: generalized set_env_var()/clear_env_var() + validator registry
# ---------------------------------------------------------------------------


def test_validate_model_name_accepts_empty_and_bounded_tokens():
    assert validate_model_name("") == ""
    assert validate_model_name("claude-opus-4-20250514") == "claude-opus-4-20250514"
    assert validate_model_name("a" * 64) == "a" * 64


@pytest.mark.parametrize(
    "value",
    [
        "a" * 65,
        "sonnet; rm -rf /",
        "sonnet\n",
        "sonnet with space",
        "$(whoami)",
        "sonnet`id`",
    ],
)
def test_validate_model_name_rejects_shell_metacharacters_and_whitespace(value):
    with pytest.raises(InvalidEnvValueError):
        validate_model_name(value)


def test_env_value_validators_registry_has_both_model_keys():
    assert set(ENV_VALUE_VALIDATORS) == {"ASSISTANT_CLAUDE_MODEL", "LLM_MODEL"}
    assert ENV_VALUE_VALIDATORS["ASSISTANT_CLAUDE_MODEL"] is validate_model_name
    assert ENV_VALUE_VALIDATORS["LLM_MODEL"] is validate_model_name


def test_set_env_var_writes_registered_key(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DB_PATH=jobqueue.db\n", encoding="utf-8")
    result = set_env_var(str(env_path), "ASSISTANT_CLAUDE_MODEL", "sonnet")
    assert result == "sonnet"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["DB_PATH=jobqueue.db", "ASSISTANT_CLAUDE_MODEL=sonnet"]


def test_set_env_var_empty_value_clears_the_line(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DB_PATH=jobqueue.db\nASSISTANT_CLAUDE_MODEL=sonnet\n", encoding="utf-8"
    )
    result = set_env_var(str(env_path), "ASSISTANT_CLAUDE_MODEL", "")
    assert result == ""
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["DB_PATH=jobqueue.db"]


def test_set_env_var_rejects_shell_metacharacters_and_writes_nothing(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DB_PATH=jobqueue.db\n", encoding="utf-8")
    original = env_path.read_text(encoding="utf-8")
    with pytest.raises(InvalidEnvValueError):
        set_env_var(str(env_path), "ASSISTANT_CLAUDE_MODEL", "sonnet; rm -rf /")
    assert env_path.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env.bak").exists()


def test_set_env_var_rejects_unregistered_key(tmp_path):
    env_path = tmp_path / ".env"
    with pytest.raises(InvalidEnvValueError):
        set_env_var(str(env_path), "SOME_UNREGISTERED_KEY", "value")
    assert not env_path.exists()


def test_clear_env_var_removes_only_the_named_key(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DB_PATH=jobqueue.db\nLLM_MODEL=opus\nASSISTANT_CLAUDE_MODEL=sonnet\n",
        encoding="utf-8",
    )
    clear_env_var(str(env_path), "LLM_MODEL")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["DB_PATH=jobqueue.db", "ASSISTANT_CLAUDE_MODEL=sonnet"]
