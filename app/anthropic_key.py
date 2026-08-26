"""DG-ASSISTANT-CLAUDE-TURN v1 C2 (docs/DECISIONS.md 2026-08-26 — Anthropic
API key UI-direct-set exception, user ruling): atomic `.env` rewrite for
`ANTHROPIC_API_KEY`.

This is the one documented direct-execute exception in this packet: a
platform administrator types a masked key into the "AI 供應商" panel and it
takes effect immediately (no approval card) — the user explicitly ruled this
in, mirroring how `DG-INFRA-DIRECT-ACTIONS v1` made server add/update/disable
direct-execute. Everything here is deliberately narrow:

- shape validation only (`sk-ant-` prefix, 20..300 chars, no whitespace) —
  never a real call to Anthropic to "verify" the key (that would spend the
  caller's quota and contact a real external service from a request handler
  a test suite exercises).
- the value is **never** returned, logged, or handed to `app.audit` — the
  route layer (`dispatch_center/api/routers/ai_providers_v2.py`) only calls
  `append_audit("anthropic_api_key_configured"/"..._cleared", {})`, and this
  module itself never touches `app.audit` at all.
- exactly one KEY=VALUE line in the target `.env` is added/replaced/removed;
  every other line (including comments/ordering) is preserved verbatim.
- write is atomic (temp file + `os.replace()`, same discipline as
  `app.server_config.write_servers_yaml_atomically()`) and chmod'd `0o600`
  before the rename so the key is never briefly world-readable; exactly one
  `{path}.bak` backup is kept (overwritten each time, not a growing history
  — the packet's wording is "保留一份 .env.bak 備份", singular).
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

ANTHROPIC_API_KEY_ENV_NAME = "ANTHROPIC_API_KEY"

_MIN_KEY_LENGTH = 20
_MAX_KEY_LENGTH = 300
_KEY_PREFIX = "sk-ant-"


class InvalidAnthropicApiKeyError(ValueError):
    """The caller-supplied string does not have the expected key shape."""


def validate_anthropic_api_key(value: str) -> str:
    """Shape-only validation (never a real Anthropic API call): `sk-ant-`
    prefix, bounded length, no whitespace anywhere. Raises
    `InvalidAnthropicApiKeyError` (mapped to HTTP 400 by the route layer,
    zero writes) otherwise."""

    if not isinstance(value, str) or not value:
        raise InvalidAnthropicApiKeyError("API key 必須是非空字串")
    if any(ch.isspace() for ch in value):
        raise InvalidAnthropicApiKeyError("API key 不可包含空白字元")
    if not (_MIN_KEY_LENGTH <= len(value) <= _MAX_KEY_LENGTH):
        raise InvalidAnthropicApiKeyError(
            f"API key 長度必須介於 {_MIN_KEY_LENGTH}~{_MAX_KEY_LENGTH} 字元之間"
        )
    if not value.startswith(_KEY_PREFIX):
        raise InvalidAnthropicApiKeyError(f"API key 必須以 {_KEY_PREFIX!r} 開頭")
    return value


def _read_env_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def _backup_env_file(path: str) -> None:
    """Single overwritten `{path}.bak` (not a timestamped history — see
    module docstring). Skipped if the file does not exist yet (first-ever
    write), same convention as
    `app.server_config.backup_servers_yaml()`. `shutil.copy2()` preserves the
    *source* file's mode -- a pre-existing `.env` that happens to be
    world-readable would otherwise propagate that mode to the backup, so the
    backup is explicitly chmod'd `0o600` regardless of the source file's
    permissions (the backup can contain the very same key this module
    exists to protect)."""

    if os.path.exists(path):
        backup_path = f"{path}.bak"
        shutil.copy2(path, backup_path)
        os.chmod(backup_path, 0o600)


def _rewrite_env_key_line(
    lines: list[str], key: str, new_value: Optional[str]
) -> list[str]:
    """Replace/add/remove exactly the one `KEY=...` line; every other line
    (comments, ordering, unrelated variables) is untouched. `new_value=None`
    drops the line entirely (`DELETE`); otherwise the line is set/overwritten
    in place (its original position if it already existed, appended at the
    end otherwise)."""

    prefix = f"{key}="
    out: list[str] = []
    found = False
    for line in lines:
        if line.strip().startswith(prefix):
            found = True
            if new_value is not None:
                out.append(f"{key}={new_value}")
            continue
        out.append(line)
    if new_value is not None and not found:
        out.append(f"{key}={new_value}")
    return out


def _atomic_write_env(path: str, lines: list[str]) -> None:
    """Temp file + `os.replace()` (same atomicity discipline as
    `app.server_config.write_servers_yaml_atomically()`), but the temp file
    is created at mode `0o600` **atomically at creation** via the `os.open()`
    mode argument (owner-only bits, so umask cannot widen it) instead of a
    separate `open()` followed by `os.chmod()` -- the latter leaves a window
    where the file briefly exists at the process's default (often
    world-readable) mode before the chmod call lands."""

    tmp_path = f"{path}.tmp.{os.getpid()}"
    content = "\n".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp_path, path)


def set_anthropic_api_key(path: str, api_key: str) -> None:
    """Validate (never a real Anthropic call), backup, then atomically
    rewrite `path` with `ANTHROPIC_API_KEY=<api_key>`. Raises
    `InvalidAnthropicApiKeyError` before touching the filesystem at all if
    the shape is wrong."""

    validated = validate_anthropic_api_key(api_key)
    lines = _read_env_lines(path)
    new_lines = _rewrite_env_key_line(lines, ANTHROPIC_API_KEY_ENV_NAME, validated)
    _backup_env_file(path)
    _atomic_write_env(path, new_lines)


def clear_anthropic_api_key(path: str) -> None:
    """Atomically remove the `ANTHROPIC_API_KEY=` line (if any) from `path`.
    A no-op content-wise if the key was never set, but still backs up and
    rewrites the file (idempotent, matches the DELETE route's semantics)."""

    lines = _read_env_lines(path)
    new_lines = _rewrite_env_key_line(lines, ANTHROPIC_API_KEY_ENV_NAME, None)
    _backup_env_file(path)
    _atomic_write_env(path, new_lines)


__all__ = [
    "ANTHROPIC_API_KEY_ENV_NAME",
    "InvalidAnthropicApiKeyError",
    "clear_anthropic_api_key",
    "set_anthropic_api_key",
    "validate_anthropic_api_key",
]
