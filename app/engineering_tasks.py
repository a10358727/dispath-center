"""AI Engineering Task 的 deterministic request 與 Hub staging primitives。

本模組刻意不執行任何命令，也不存取資料庫。建立請求時由呼叫端以注入的
``local_run`` 驗證 ProjectVersion 的精確 commit；核准後才使用這裡的純函式
組出 bundle staging 指令。使用者文字只會被渲染成 instruction 檔案內容，
永遠不會進入這些 shell command builders。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shlex
import stat
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.coding_agents import CODEX_AGENT_PROVIDER_ID, require_coding_agent
from app.datasets import build_ssh_opts
from app.engineering_path_policy import (
    EngineeringPathPolicyError,
    build_engineering_path_policy,
    canonical_engineering_path_inputs,
)
# Matching and secret-basename semantics have a single source of truth in the
# verifier module.  These two helpers are private there on purpose (that
# module's exact source bytes are digest-bound into the approval contract);
# this preview never adds behavior to that file, it only reads its existing
# private matching logic to describe likely finalize-time outcomes early.
from app.engineering_path_policy import (
    _is_protected_secret_basename,
    _scope_matches,
)
from app.hub import hub_repo_path


ENGINEERING_TASK_PROVIDER_ID = CODEX_AGENT_PROVIDER_ID
ENGINEERING_TASK_INSTRUCTION_LIMIT = 4000
ENGINEERING_TASK_DETECTION_VERSION = "path-markers-v1"
ENGINEERING_TASK_TEXT_PREVIEW_LIMIT = 65536
ENGINEERING_TASK_SOURCE_FILE_LIMIT = 1024 * 1024
ENGINEERING_TASK_BINARY_FILE_LIMIT = 512 * 1024 * 1024
# Aligned with app.engineering_path_policy._MAX_CHANGED_PATHS: bound the same
# class of unbounded-tree-size work for a single synchronous preview request.
ENGINEERING_TASK_PATH_COVERAGE_MAX_TREE_PATHS = 10_000

_ENGINEERING_RESULT_TEXT_FILES = {
    "diff.patch",
    "final_message.txt",
    "task.log",
}
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[^\r\n]{0,80}\bPRIVATE[ \t]+KEY"
    r"(?:[ \t]+BLOCK)?[ \t]*-----",
    re.IGNORECASE,
)
_CREDENTIAL_SUBSTITUTIONS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(authorization\s*:\s*basic\s+)[^\s]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|auth[_-]?token|session[_-]?token|"
        r"password|passwd|secret|cookie)\s*[:=]\s*[\"']?)[^\s\"';,]+"
    ),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:npm_|pypi-)[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)((?:aws_)?secret[_-]?access[_-]?key\s*[:=]\s*[\"']?)"
        r"[A-Za-z0-9/+=_-]{32,}"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b((?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|"
        r"amqps?|mssql|https?|ftp|ssh)://)[^/\s:@]+:[^/\s@]+(?=@)"
    ),
)
_PRIVATE_PATH_SUBSTITUTIONS = (
    re.compile(r"(?<![A-Za-z0-9_.-])/(?:home|root|tmp)/[^\s\"'<>]+"),
    re.compile(r"(?i)\b[A-Z]:\\Users\\[^\s\"'<>]+"),
)
_SECRET_SUBSTITUTIONS = _CREDENTIAL_SUBSTITUTIONS + _PRIVATE_PATH_SUBSTITUTIONS
# A generic "Bearer <word>" is useful for output redaction but is not strong
# enough to reject input (for example, "use bearer authentication").  Header,
# assignment and provider-specific shapes remain fail-closed at persistence.
_RAW_CREDENTIAL_PATTERNS = (
    _CREDENTIAL_SUBSTITUTIONS[:1] + _CREDENTIAL_SUBSTITUTIONS[2:]
)


class InvalidEngineeringTaskRequestError(ValueError):
    """Structured Engineering Task request 無效或無法固定 immutable base。"""


def _normalize_engineering_display_text(raw_text: object) -> str:
    """Remove terminal/control obfuscation before credential inspection."""

    text = _ANSI_ESCAPE_RE.sub("", str(raw_text or ""))
    return "".join(
        char
        for char in text
        if char in "\n\r\t"
        or (
            ord(char) >= 32
            and ord(char) != 127
            and not unicodedata.category(char).startswith("C")
        )
    )


def redact_engineering_text(
    raw_text: str, *, max_chars: int = ENGINEERING_TASK_TEXT_PREVIEW_LIMIT
) -> dict[str, Any]:
    """Return a bounded display preview without exposing common credentials.

    A private-key block fails closed and withholds the whole preview.  Other
    high-confidence credential forms are replaced before truncation.  This is
    intentionally conservative and is used only by the new Engineering Task
    visibility endpoints; compatibility endpoints keep their historic shape.
    """

    raw = str(raw_text or "")
    original_length = len(raw)
    text = _normalize_engineering_display_text(raw)
    # Detect after terminal/control normalization as well as on ordinary input.
    # Otherwise an ANSI escape, NUL or zero-width format character can split a
    # marker and be removed only after the fail-closed check has already run.
    if _PRIVATE_KEY_RE.search(text):
        return {
            "content": None,
            "redacted": True,
            "withheld": True,
            "redaction_count": 1,
            "truncated": original_length > max_chars,
            "reason": "private_key_detected",
        }
    redaction_count = 0
    for pattern in _SECRET_SUBSTITUTIONS:
        def replace(match: re.Match[str]) -> str:
            nonlocal redaction_count
            redaction_count += 1
            prefix = match.group(1) if match.lastindex else ""
            return f"{prefix}[REDACTED]"

        text = pattern.sub(replace, text)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n…（內容已截斷）"
    return {
        "content": text,
        "redacted": redaction_count > 0,
        "withheld": False,
        "redaction_count": redaction_count,
        "truncated": truncated,
        "reason": None,
    }


def _inspect_engineering_result_file(
    *,
    result_dir: str,
    filename: str,
    include_text: bool = False,
    max_chars: int = ENGINEERING_TASK_TEXT_PREVIEW_LIMIT,
    capture_payload: bool = False,
) -> dict[str, Any]:
    """Inspect one allowlisted regular result file using a safe logical key.

    The returned mapping never contains an absolute path.  The directory and
    allowlisted child are opened by descriptor with ``O_NOFOLLOW``; all type,
    size and change checks are made against that descriptor.  This avoids the
    lstat/open swap window and ensures even a concurrently growing source is
    never read beyond its configured cap.
    """

    if filename not in _ENGINEERING_RESULT_TEXT_FILES | {"result.json", "changes.bundle"}:
        raise ValueError("engineering artifact filename is not allowlisted")
    directory_fd: int | None = None
    file_fd: int | None = None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    try:
        directory_fd = os.open(
            result_dir,
            os.O_RDONLY | directory | nofollow | cloexec | nonblock,
        )
        file_fd = os.open(
            filename,
            os.O_RDONLY | nofollow | cloexec | nonblock,
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            return {
                "available": False,
                "reason": "unsafe_file_type",
                "storage_key": filename,
            }
        size_limit = (
            ENGINEERING_TASK_SOURCE_FILE_LIMIT
            if include_text or filename == "result.json"
            else ENGINEERING_TASK_BINARY_FILE_LIMIT
        )
        if file_stat.st_size > size_limit:
            if not include_text and filename != "result.json":
                return {
                    "available": True,
                    "reason": "digest_deferred",
                    "storage_key": filename,
                    "size_bytes": file_stat.st_size,
                    "sha256": None,
                }
            return {
                "available": False,
                "reason": "source_too_large",
                "storage_key": filename,
                "size_bytes": file_stat.st_size,
            }

        payload = bytearray() if include_text or capture_payload else None
        digest = hashlib.sha256()
        remaining = file_stat.st_size
        bytes_read = 0
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            bytes_read += len(chunk)
            remaining -= len(chunk)
            digest.update(chunk)
            if payload is not None:
                payload.extend(chunk)

        final_stat = os.fstat(file_fd)
        original_identity = (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
        )
        final_identity = (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        )
        if bytes_read != file_stat.st_size or final_identity != original_identity:
            return {
                "available": False,
                "reason": "source_changed",
                "storage_key": filename,
            }

        result: dict[str, Any] = {
            "available": True,
            "reason": None,
            "storage_key": filename,
            "size_bytes": bytes_read,
            "sha256": digest.hexdigest(),
        }
        if payload is not None:
            captured = bytes(payload)
            if include_text:
                result.update(
                    redact_engineering_text(
                        captured.decode("utf-8", errors="replace"),
                        max_chars=max_chars,
                    )
                )
            if capture_payload:
                result["_captured_payload"] = captured
        return result
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            reason = "unsafe_file_type"
        elif exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            reason = "missing"
        else:
            reason = "unreadable"
        return {"available": False, "reason": reason, "storage_key": filename}
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def inspect_engineering_result_file(
    *,
    result_dir: str,
    filename: str,
    include_text: bool = False,
    max_chars: int = ENGINEERING_TASK_TEXT_PREVIEW_LIMIT,
) -> dict[str, Any]:
    """Return safe metadata and, optionally, a redacted bounded preview."""

    return _inspect_engineering_result_file(
        result_dir=result_dir,
        filename=filename,
        include_text=include_text,
        max_chars=max_chars,
    )


def capture_sanitized_engineering_patch(*, result_dir: str) -> dict[str, Any]:
    """Capture one bounded ``diff.patch`` descriptor and return sanitized bytes.

    This is intentionally narrower than the generic visibility inspector.  The
    filename and 1 MiB ceiling are fixed, the raw bytes are captured only from
    the same descriptor whose identity and digest were checked, and callers
    never receive those raw bytes.  Invalid UTF-8, private-key material,
    truncation, or a sanitized response that exceeds the source ceiling fails
    closed instead of returning a partial download.
    """

    inspected = _inspect_engineering_result_file(
        result_dir=result_dir,
        filename="diff.patch",
        include_text=True,
        max_chars=ENGINEERING_TASK_SOURCE_FILE_LIMIT,
        capture_payload=True,
    )
    raw_payload = inspected.pop("_captured_payload", None)
    inspected.pop("content", None)
    if not inspected.get("available") or not isinstance(raw_payload, bytes):
        return inspected
    try:
        raw_text = raw_payload.decode("utf-8")
    except UnicodeDecodeError:
        return {
            **inspected,
            "available": False,
            "reason": "invalid_payload",
        }

    preview = redact_engineering_text(
        raw_text,
        max_chars=ENGINEERING_TASK_SOURCE_FILE_LIMIT,
    )
    content = preview.get("content")
    if preview.get("withheld"):
        return {
            **inspected,
            "available": False,
            "reason": "content_withheld",
            "redacted": True,
            "withheld": True,
            "truncated": bool(preview.get("truncated")),
        }
    if not isinstance(content, str) or not content:
        return {
            **inspected,
            "available": False,
            "reason": "invalid_payload",
        }
    sanitized_payload = content.encode("utf-8")
    if preview.get("truncated") or len(sanitized_payload) > ENGINEERING_TASK_SOURCE_FILE_LIMIT:
        return {
            **inspected,
            "available": False,
            "reason": "sanitized_too_large",
            "redacted": bool(preview.get("redacted")),
            "withheld": False,
            "truncated": True,
        }
    return {
        **inspected,
        "redacted": bool(preview.get("redacted")) or sanitized_payload != raw_payload,
        "withheld": False,
        "truncated": False,
        "_sanitized_payload": sanitized_payload,
    }


def load_engineering_result_json(
    *, result_dir: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read and parse allowlisted ``result.json`` from one verified descriptor.

    Consumers receive the same safe metadata as the inspection API plus a JSON
    object parsed from the exact bounded bytes that were hashed.  Invalid UTF-8,
    invalid JSON, or a non-object root fails closed without returning content.
    """

    inspected = _inspect_engineering_result_file(
        result_dir=result_dir,
        filename="result.json",
        capture_payload=True,
    )
    payload = inspected.pop("_captured_payload", None)
    if not inspected.get("available") or not isinstance(payload, bytes):
        return inspected, None
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {**inspected, "available": False, "reason": "invalid_json"}, None
    if not isinstance(parsed, dict):
        return {**inspected, "available": False, "reason": "invalid_json"}, None
    return inspected, parsed


def codex_provider_capability_snapshot() -> dict[str, Any]:
    """回傳可安全持久化的 Codex provider capability snapshot。

    這是目前 legacy ``codex exec`` adapter 的真實能力，不把尚未完成的
    app-server event stream／resume／command callback 說成已實作。
    """

    return require_coding_agent(ENGINEERING_TASK_PROVIDER_ID).capability_snapshot()


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _clean_items(value: object) -> list[str]:
    if value is None:
        return []
    raw_items: Iterable[object]
    if isinstance(value, str):
        raw_items = value.splitlines()
    elif isinstance(value, Iterable):
        raw_items = value
    else:
        raw_items = (value,)
    return [item for raw in raw_items if (item := _clean_text(raw))]


def _iter_text_values(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_text_values(item)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _iter_text_values(item)


def _reject_raw_credentials(value: object) -> None:
    """Fail before approval persistence when free text contains a credential.

    Repository paths are deliberately excluded from this input check.  They
    are useful scope metadata and are only redacted on compatibility output
    surfaces.  This gate is limited to private-key blocks and high-confidence
    token/assignment shapes so ordinary mentions such as "secret broker" stay
    valid.
    """

    for text in _iter_text_values(value):
        normalized = _normalize_engineering_display_text(text)
        if _PRIVATE_KEY_RE.search(normalized) or any(
            pattern.search(normalized) for pattern in _RAW_CREDENTIAL_PATTERNS
        ):
            raise InvalidEngineeringTaskRequestError(
                "AI Engineering Task 文字不可包含 raw credential；"
                "請移除 token、password、cookie 或 private key"
            )


def reject_engineering_raw_credentials(value: object) -> None:
    """Public persistence gate for other Engineering Task free-text inputs."""

    _reject_raw_credentials(value)


def normalize_engineering_task_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """正規化 structured request，保留固定且可序列化的欄位集合。"""

    objective = _clean_text(spec.get("objective"))
    if not objective:
        raise InvalidEngineeringTaskRequestError("objective 不可為空")

    validation = spec.get("validation") or {}
    permissions = spec.get("permissions") or {}
    if not isinstance(validation, Mapping) or not isinstance(permissions, Mapping):
        raise InvalidEngineeringTaskRequestError("validation/permissions 必須是 object")

    if permissions.get("modify_project_files", True) is not True:
        raise InvalidEngineeringTaskRequestError("AI Engineering Task 必須允許修改隔離 worktree")
    if bool(permissions.get("install_dependencies", False)):
        raise InvalidEngineeringTaskRequestError(
            "dependency installation 尚未有 engineering_command 核准與技術強制，不能授權"
        )
    if bool(permissions.get("external_network", False)):
        raise InvalidEngineeringTaskRequestError(
            "external network 尚未有 engineering_command 核准與技術強制，不能授權"
        )

    environment_references = _clean_items(permissions.get("environment_references"))
    secret_references = _clean_items(permissions.get("secret_references"))
    if environment_references or secret_references:
        raise InvalidEngineeringTaskRequestError(
            "尚未設定 environment/secret reference broker，不能提交 reference"
        )

    try:
        path_policy, _path_policy_sha256 = build_engineering_path_policy(
            spec.get("allowed_paths"),
            spec.get("prohibited_paths", []),
        )
        canonical_paths = canonical_engineering_path_inputs(path_policy)
    except EngineeringPathPolicyError as exc:
        raise InvalidEngineeringTaskRequestError(
            "allowed_paths 必須明確指定至少一個 canonical relative POSIX scope；"
            "使用 '.' 代表整個 repository，目錄 subtree 必須以 '/' 結尾"
        ) from exc

    worker_target = _clean_text(validation.get("worker_validation_target")) or None
    normalized = {
        "objective": objective,
        "background": _clean_text(spec.get("background")) or None,
        "expected_changes": _clean_items(spec.get("expected_changes")),
        "non_goals": _clean_items(spec.get("non_goals")),
        "allowed_paths": canonical_paths["allowed_paths"],
        "prohibited_paths": canonical_paths["prohibited_paths"],
        "prohibited_changes": _clean_items(spec.get("prohibited_changes")),
        "acceptance_criteria": _clean_items(spec.get("acceptance_criteria")),
        "validation": {
            "tests_lint": bool(validation.get("tests_lint", False)),
            "build_smoke": bool(validation.get("build_smoke", False)),
            "continue_fixing_failures": bool(
                validation.get("continue_fixing_failures", False)
            ),
            "worker_validation_target": worker_target,
        },
        "permissions": {
            "modify_project_files": True,
            "install_dependencies": False,
            "external_network": False,
            "environment_references": [],
            "secret_references": [],
        },
    }
    _reject_raw_credentials(normalized)
    return normalized


def _add_section(parts: list[str], heading: str, items: Iterable[str]) -> None:
    cleaned = [item for raw in items if (item := _clean_text(raw))]
    if cleaned:
        parts.append(f"{heading}:\n" + "\n".join(f"- {item}" for item in cleaned))


def render_engineering_task_instruction(spec: Mapping[str, Any]) -> str:
    """以固定 section 順序渲染 instruction，超過既有限制即拒絕。"""

    normalized = normalize_engineering_task_spec(spec)
    validation = normalized["validation"]
    parts = ["AI Engineering Task"]
    _add_section(parts, "Task objective", _clean_items(normalized["objective"]))
    _add_section(
        parts,
        "Background and relevant context",
        _clean_items(normalized["background"]),
    )
    _add_section(parts, "Expected changes", normalized["expected_changes"])
    _add_section(parts, "Non-goals", normalized["non_goals"])
    _add_section(parts, "Allowed modification scope", normalized["allowed_paths"])
    _add_section(parts, "Prohibited paths", normalized["prohibited_paths"])
    _add_section(parts, "Prohibited changes", normalized["prohibited_changes"])
    _add_section(parts, "Acceptance criteria", normalized["acceptance_criteria"])

    validation_items: list[str] = []
    if validation["tests_lint"]:
        validation_items.append(
            "Run the repository's relevant tests and lint checks only inside this "
            "current sandboxed agent turn; the outer Runner will not execute "
            "repository code after the turn."
        )
    if validation["build_smoke"]:
        validation_items.append(
            "Run relevant build and smoke checks only inside this current "
            "sandboxed agent turn."
        )
    if validation["continue_fixing_failures"]:
        validation_items.append(
            "Where feasible in the current agent turn, analyze and fix validation failures."
        )
    worker_target = validation["worker_validation_target"]
    if worker_target:
        validation_items.append(
            f"Record {worker_target} as a worker validation preference; do not assume a worker Job exists."
        )
    _add_section(parts, "Validation strategy", validation_items)

    _add_section(
        parts,
        "Requested execution behavior",
        [
            "Modify project files only inside the existing isolated Git worktree.",
            "Dependency installation is not authorized by this form; do not install dependencies.",
            "External network access is not authorized by this form; do not access external networks.",
            "The final Git diff is technically checked against the approved path policy on the Runner before bundling and independently on Server A before acceptance.",
            "This final-result policy does not claim turn-time filesystem confinement; temporary writes during the agent turn remain outside this guarantee.",
        ],
    )
    instruction = "\n\n".join(parts)
    if len(instruction) > ENGINEERING_TASK_INSTRUCTION_LIMIT:
        raise InvalidEngineeringTaskRequestError(
            f"generated instruction 過長（{len(instruction)} 字元，上限 "
            f"{ENGINEERING_TASK_INSTRUCTION_LIMIT} 字元）"
        )
    return instruction


def detect_project_metadata(paths: Iterable[str]) -> dict[str, Any]:
    """只依 exact commit tree 的路徑 marker 做確定性 metadata detection。"""

    normalized_paths = sorted(
        {path.strip().lstrip("./") for path in paths if isinstance(path, str) and path.strip()}
    )
    names = {path.lower() for path in normalized_paths}
    basenames = {path.rsplit("/", 1)[-1] for path in names}
    suffixes = {Path(path).suffix.lower() for path in names}

    languages: list[str] = []
    language_markers = (
        ("python", ".py" in suffixes or bool({"pyproject.toml", "requirements.txt"} & basenames)),
        ("typescript", bool({".ts", ".tsx"} & suffixes) or "tsconfig.json" in basenames),
        ("javascript", bool({".js", ".jsx", ".mjs", ".cjs"} & suffixes) or "package.json" in basenames),
        ("go", ".go" in suffixes or "go.mod" in basenames),
        ("rust", ".rs" in suffixes or "cargo.toml" in basenames),
        ("java", ".java" in suffixes or bool({"pom.xml", "build.gradle", "build.gradle.kts"} & basenames)),
        ("shell", ".sh" in suffixes),
    )
    for language, present in language_markers:
        if present:
            languages.append(language)

    frameworks: list[str] = []
    framework_markers = (
        ("django", "manage.py" in basenames),
        ("nextjs", any(name.startswith("next.config.") for name in basenames)),
        ("vue", any(name.startswith("vue.config.") for name in basenames)),
        ("svelte", any(name.startswith("svelte.config.") for name in basenames)),
    )
    for framework, present in framework_markers:
        if present:
            frameworks.append(framework)

    validation_tools: list[str] = []
    tool_markers = (
        ("pytest", bool({"pytest.ini", "conftest.py"} & basenames)),
        ("tox", "tox.ini" in basenames),
        ("ruff", bool({"ruff.toml", ".ruff.toml"} & basenames)),
        ("eslint", any(name.startswith("eslint.config.") for name in basenames)),
    )
    for tool, present in tool_markers:
        if present:
            validation_tools.append(tool)

    return {
        "detection": ENGINEERING_TASK_DETECTION_VERSION,
        "languages": languages,
        "frameworks": frameworks,
        "validation_tools": validation_tools,
    }


async def inspect_hub_project_version(
    *,
    project_name: str,
    git_commit: str,
    local_home_dir: str,
    local_run,
) -> tuple[str, dict[str, Any]]:
    """確認 Hub 仍含 ProjectVersion 的 exact commit，並讀取 tree markers。"""

    if local_run is None:
        raise ValueError("inspect_hub_project_version 需要 local_run")
    repo_path = str(Path(hub_repo_path(project_name, local_home_dir)).resolve())
    if not Path(repo_path).is_dir():
        raise InvalidEngineeringTaskRequestError(
            f"專案 {project_name} 尚未有可用的 Server A Hub，請先執行 hub sync"
        )
    commit = _clean_text(git_commit)
    if not commit:
        raise InvalidEngineeringTaskRequestError("ProjectVersion 沒有 git_commit")

    rev = f"{commit}^{{commit}}"
    verify = await local_run(
        f"git --git-dir={shlex.quote(repo_path)} rev-parse --verify {shlex.quote(rev)}",
        15,
    )
    if getattr(verify, "exit_status", 1) != 0:
        raise InvalidEngineeringTaskRequestError(
            "ProjectVersion 的 commit 已無法在 Hub 解析；請重新同步並建立新請求"
        )
    resolved = _clean_text(getattr(verify, "stdout", ""))
    if not resolved or resolved.lower() != commit.lower():
        raise InvalidEngineeringTaskRequestError(
            "ProjectVersion 不是可直接驗證的 exact commit；不會以 branch HEAD 代替"
        )

    tree = await local_run(
        f"git --git-dir={shlex.quote(repo_path)} ls-tree -r --name-only {shlex.quote(resolved)}",
        30,
    )
    if getattr(tree, "exit_status", 1) != 0:
        raise InvalidEngineeringTaskRequestError("無法讀取 ProjectVersion tree metadata")
    paths = (getattr(tree, "stdout", "") or "").splitlines()
    return resolved, detect_project_metadata(paths)


def _rule_coverage_summary(scope: str, paths: Sequence[str], directories: set[str]) -> dict[str, Any]:
    basename = (scope[:-1] if scope.endswith("/") else scope).rsplit("/", 1)[-1]
    return {
        "scope": scope,
        "file_hits": sum(1 for path in paths if _scope_matches(scope, path)),
        "matches_existing_directory": (
            not scope.endswith("/") and scope != "." and scope in directories
        ),
        "secret_protected": _is_protected_secret_basename(basename),
    }


async def preview_hub_path_policy_coverage(
    *,
    project_name: str,
    git_commit: str,
    allowed_paths: Iterable[str],
    prohibited_paths: Iterable[str],
    local_home_dir: str,
    local_run,
) -> dict[str, Any]:
    """回傳 allowed/prohibited 規則在 pinned base tree 上的唯讀涵蓋摘要。

    只回傳計數與布林，永遠不回傳任何 repo 路徑字串；比對語意重用
    ``app.engineering_path_policy`` 的既有私有函式，此函式本身完全不改變
    verifier 合約。純 advisory：0 命中不代表規則錯誤（規則可能指向即將新建
    的檔案），exact 規則命中既有目錄名幾乎必定是想要的是 subtree（結尾 ``/``）。
    """

    if local_run is None:
        raise ValueError("preview_hub_path_policy_coverage 需要 local_run")
    repo_path = str(Path(hub_repo_path(project_name, local_home_dir)).resolve())
    if not Path(repo_path).is_dir():
        raise InvalidEngineeringTaskRequestError(
            f"專案 {project_name} 尚未有可用的 Server A Hub，請先執行 hub sync"
        )
    commit = _clean_text(git_commit)
    if not commit:
        raise InvalidEngineeringTaskRequestError("ProjectVersion 沒有 git_commit")

    rev = f"{commit}^{{commit}}"
    verify = await local_run(
        f"git --git-dir={shlex.quote(repo_path)} rev-parse --verify {shlex.quote(rev)}",
        15,
    )
    if getattr(verify, "exit_status", 1) != 0:
        raise InvalidEngineeringTaskRequestError(
            "ProjectVersion 的 commit 已無法在 Hub 解析；請重新同步並建立新請求"
        )
    resolved = _clean_text(getattr(verify, "stdout", ""))
    if not resolved or resolved.lower() != commit.lower():
        raise InvalidEngineeringTaskRequestError(
            "ProjectVersion 不是可直接驗證的 exact commit；不會以 branch HEAD 代替"
        )

    try:
        policy, _digest = build_engineering_path_policy(allowed_paths, prohibited_paths)
    except EngineeringPathPolicyError as exc:
        raise InvalidEngineeringTaskRequestError(
            "allowed_paths 必須明確指定至少一個 canonical relative POSIX scope；"
            "使用 '.' 代表整個 repository，目錄 subtree 必須以 '/' 結尾"
        ) from exc
    canonical = canonical_engineering_path_inputs(policy)

    tree = await local_run(
        f"git --git-dir={shlex.quote(repo_path)} ls-tree -r --name-only {shlex.quote(resolved)}",
        30,
    )
    if getattr(tree, "exit_status", 1) != 0:
        raise InvalidEngineeringTaskRequestError("無法讀取 ProjectVersion tree metadata")
    raw_paths = (getattr(tree, "stdout", "") or "").splitlines()
    truncated = len(raw_paths) > ENGINEERING_TASK_PATH_COVERAGE_MAX_TREE_PATHS
    paths = raw_paths[:ENGINEERING_TASK_PATH_COVERAGE_MAX_TREE_PATHS]

    directories: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))

    return {
        "base_commit": resolved,
        "tree_file_count": len(paths),
        "truncated": truncated,
        "allowed": [
            _rule_coverage_summary(scope, paths, directories)
            for scope in canonical["allowed_paths"]
        ],
        "prohibited": [
            _rule_coverage_summary(scope, paths, directories)
            for scope in canonical["prohibited_paths"]
        ],
    }


def _validated_task_id(task_id: str) -> str:
    try:
        parsed = uuid.UUID(str(task_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid engineering task id") from exc
    return str(parsed)


def local_engineering_bundle_path(task_id: str, local_home_dir: str) -> str:
    task_id = _validated_task_id(task_id)
    base = Path(local_home_dir or ".").resolve()
    return str(base / "engineering_bundles" / f"{task_id}.bundle")


def local_engineering_bundle_repo_path(task_id: str, local_home_dir: str) -> str:
    """Task-local bare repo used to give the approved SHA a bundle ref name.

    ``git bundle create <raw-sha>`` refuses to create a bundle because a bundle
    must advertise at least one ref.  We must not use ``--all``: refs can move
    after approval and would make the staged artifact contain unapproved tips.
    This isolated repo lets staging fetch only the approved commit and expose it
    under a deterministic local ref without mutating the canonical Hub.
    """

    task_id = _validated_task_id(task_id)
    base = Path(local_home_dir or ".").resolve()
    return str(base / "engineering_bundles" / f"{task_id}.git")


def local_engineering_instruction_path(task_id: str, local_home_dir: str) -> str:
    relpath = local_engineering_instruction_relpath(task_id)
    base = Path(local_home_dir or ".").resolve()
    return str(base / relpath)


def local_engineering_instruction_relpath(task_id: str) -> str:
    return f"engineering_bundles/{_validated_task_id(task_id)}.instruction.txt"


def local_engineering_path_policy_path(task_id: str, local_home_dir: str) -> str:
    relpath = local_engineering_path_policy_relpath(task_id)
    base = Path(local_home_dir or ".").resolve()
    return str(base / relpath)


def local_engineering_path_policy_relpath(task_id: str) -> str:
    return f"engineering_bundles/{_validated_task_id(task_id)}.path-policy.json"


def local_engineering_path_verifier_path(task_id: str, local_home_dir: str) -> str:
    relpath = local_engineering_path_verifier_relpath(task_id)
    base = Path(local_home_dir or ".").resolve()
    return str(base / relpath)


def local_engineering_path_verifier_relpath(task_id: str) -> str:
    return f"engineering_bundles/{_validated_task_id(task_id)}.path-policy-verifier.py"


def remote_engineering_bundle_path(task_id: str) -> str:
    return f"engineering_bundles/{_validated_task_id(task_id)}.bundle"


def _validated_exact_commit(exact_commit: str) -> str:
    commit = _clean_text(exact_commit)
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise ValueError("invalid exact base commit")
    return commit.lower()


def build_engineering_bundle_create_command(
    task_id: str,
    project_name: str,
    exact_commit: str,
    local_home_dir: str,
) -> str:
    """Build a bundle containing the approved commit, without reading branch HEAD.

    The task-local bare repo and fixed ``refs/heads/approved`` are idempotent retry
    state.  The canonical Hub is read-only throughout this command.
    """

    task_id = _validated_task_id(task_id)
    commit = _validated_exact_commit(exact_commit)
    object_format = "sha256" if len(commit) == 64 else "sha1"
    repo_path = str(Path(hub_repo_path(project_name, local_home_dir)).resolve())
    bundle_path = local_engineering_bundle_path(task_id, local_home_dir)
    staging_repo_path = local_engineering_bundle_repo_path(task_id, local_home_dir)
    bundle_dir = str(Path(bundle_path).parent)
    return (
        f"mkdir -p {shlex.quote(bundle_dir)} && "
        f"if [ -d {shlex.quote(staging_repo_path)} ]; then "
        f"git --git-dir={shlex.quote(staging_repo_path)} "
        "rev-parse --is-bare-repository >/dev/null; "
        f"else git init --bare --object-format={object_format} "
        f"{shlex.quote(staging_repo_path)}; fi && "
        f"git --git-dir={shlex.quote(staging_repo_path)} fetch --force --no-tags "
        f"{shlex.quote(repo_path)} "
        f"{shlex.quote(f'{commit}:refs/heads/approved')} && "
        f"test \"$(git --git-dir={shlex.quote(staging_repo_path)} "
        "rev-parse --verify refs/heads/approved^{commit})\" = "
        f"{shlex.quote(commit)} && "
        f"git --git-dir={shlex.quote(staging_repo_path)} bundle create "
        f"{shlex.quote(bundle_path)} refs/heads/approved"
    )


def build_engineering_bundle_verify_command(
    task_id: str,
    project_name: str,
    exact_commit: str,
    local_home_dir: str,
) -> str:
    task_id = _validated_task_id(task_id)
    commit = _validated_exact_commit(exact_commit)
    repo_path = str(Path(hub_repo_path(project_name, local_home_dir)).resolve())
    bundle_path = local_engineering_bundle_path(task_id, local_home_dir)
    return (
        f"git --git-dir={shlex.quote(repo_path)} bundle verify "
        f"{shlex.quote(bundle_path)} && "
        f"test \"$(git bundle list-heads {shlex.quote(bundle_path)} "
        "refs/heads/approved | awk '{print $1}')\" = "
        f"{shlex.quote(commit)}"
    )


def build_engineering_bundle_push_command(
    task_id: str,
    target_cfg: object,
    local_home_dir: str,
) -> str:
    """組出 Server A → Coding Runner 的 deterministic bundle push command。"""

    src = local_engineering_bundle_path(task_id, local_home_dir)
    remote_path = remote_engineering_bundle_path(task_id)
    ssh_opts = build_ssh_opts(target_cfg.key_path, target_cfg.port)
    remote = f"{target_cfg.user}@{target_cfg.host}"
    mkdir_command = shlex.quote("mkdir -p engineering_bundles")
    remote_mkdir = f"{ssh_opts} {shlex.quote(remote)} {mkdir_command}"
    rsync_command = (
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(src)} "
        f"{shlex.quote(f'{remote}:{remote_path}')}"
    )
    return f"{remote_mkdir} && {rsync_command}"


def build_engineering_staging_push_command(
    task_id: str,
    approval_id: int,
    workspace_rel: str,
    target_cfg: object,
    local_home_dir: str,
    *,
    include_path_policy: bool = False,
) -> str:
    """Push the immutable bundle and pre-staged instruction in one local Job.

    The instruction content is never part of this command.  Approval writes it
    to a Server-A file before atomically making the staging Job dispatchable;
    the coding Job depends on this staging Job, so it cannot observe a missing
    or partially transferred instruction.
    """

    task_id = _validated_task_id(task_id)
    if not isinstance(approval_id, int) or isinstance(approval_id, bool) or approval_id < 1:
        raise ValueError("invalid approval id")
    workspace_rel = _clean_text(workspace_rel).strip("/")
    if (
        not workspace_rel
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", workspace_rel)
        or ".." in workspace_rel.split("/")
    ):
        raise ValueError("invalid engineering workspace path")

    bundle_src = local_engineering_bundle_path(task_id, local_home_dir)
    instruction_src = local_engineering_instruction_path(task_id, local_home_dir)
    bundle_remote = remote_engineering_bundle_path(task_id)
    instruction_remote = f"{workspace_rel}/tasks/{approval_id}/instruction.txt"
    ssh_opts = build_ssh_opts(target_cfg.key_path, target_cfg.port)
    remote = f"{target_cfg.user}@{target_cfg.host}"
    mkdir_payload = (
        f"mkdir -p {shlex.quote(str(Path(bundle_remote).parent))} "
        f"{shlex.quote(str(Path(instruction_remote).parent))}"
    )
    mkdir_command = f"{ssh_opts} {shlex.quote(remote)} {shlex.quote(mkdir_payload)}"
    push_bundle = (
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(bundle_src)} "
        f"{shlex.quote(f'{remote}:{bundle_remote}')}"
    )
    push_instruction = (
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(instruction_src)} "
        f"{shlex.quote(f'{remote}:{instruction_remote}')}"
    )
    command = f"{mkdir_command} && {push_bundle} && {push_instruction}"
    if not include_path_policy:
        # Keep the v1 command byte-for-byte stable: already-pending approvals
        # and command journals must not drift when v2 support is deployed.
        return command

    policy_src = local_engineering_path_policy_path(task_id, local_home_dir)
    verifier_src = local_engineering_path_verifier_path(task_id, local_home_dir)
    policy_remote = f"{workspace_rel}/tasks/{approval_id}/path-policy.json"
    verifier_remote = (
        f"{workspace_rel}/tasks/{approval_id}/path-policy-verifier.py"
    )
    push_policy = (
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(policy_src)} "
        f"{shlex.quote(f'{remote}:{policy_remote}')}"
    )
    push_verifier = (
        f"rsync -a -e {shlex.quote(ssh_opts)} {shlex.quote(verifier_src)} "
        f"{shlex.quote(f'{remote}:{verifier_remote}')}"
    )
    return f"{command} && {push_policy} && {push_verifier}"
