"""Deterministic final-tree path policy for AI Engineering Tasks.

This module deliberately uses only the Python standard library so the exact
file can be copied to the Coding Runner and executed there.  A staged policy
binds both the policy JSON and this verifier's source digest.  The caller must
also pass the independently approved policy digest; trusting a digest stored
inside the same staged file would not protect the approval boundary.

The verifier compares the immutable base commit's tree with the final result
commit's tree.  It therefore covers changes committed by the agent as well as
changes committed by the outer runner script.  It never invokes a shell and
never includes a repository path or changed filename in an exception or CLI
diagnostic.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn


CONTRACT_VERSION = "engineering-task-v2"
POLICY_VERSION = "engineering-path-policy-v1"
VERIFIER_VERSION = "engineering-path-verifier-v1"
TREE_SEMANTICS = "final-tree-v1"
PATH_MATCHING = "case-sensitive-posix-exact-or-subtree-v1"
PROTECTED_SECRET_BASENAMES_VERSION = "dispatch-secret-basenames-v1"

EXIT_SECRET_VIOLATION = 42
EXIT_PATH_VIOLATION = 43
EXIT_CONTRACT_VIOLATION = 44

_MAX_POLICY_BYTES = 64 * 1024
_MAX_VERIFIER_SOURCE_BYTES = 1024 * 1024
_MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_SCOPE_RULES = 256
_MAX_SCOPE_UTF8_BYTES = 1024
# Leave deterministic headroom for JSON field names, separators and the
# worst-case one-byte JSON escaping expansion (for example, many ``"`` bytes).
_MAX_TOTAL_SCOPE_UTF8_BYTES = 24 * 1024
_MAX_CHANGED_PATHS = 10_000
_GIT_TIMEOUT_SECONDS = 20.0
_REGULAR_FILE_MODES = {"100644", "100755"}
_ZERO_MODE = "000000"
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RAW_HEADER_RE = re.compile(
    rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}|[0-9a-f]{64}) "
    rb"([0-9a-f]{40}|[0-9a-f]{64}) ([A-Z])\Z"
)

_POLICY_KEYS = {
    "contract_version",
    "policy_version",
    "tree_semantics",
    "path_matching",
    "allowed_paths",
    "prohibited_paths",
    "protected_secret_basenames_version",
    "verifier",
}
_VERIFIER_KEYS = {"version", "source_sha256"}


class EngineeringPathPolicyError(ValueError):
    """Base class whose public representation is deliberately path-free."""

    exit_code = EXIT_CONTRACT_VIOLATION
    safe_code = "engineering_path_contract_violation"

    def __init__(self) -> None:
        super().__init__(self.safe_code)


class EngineeringPathSecretViolation(EngineeringPathPolicyError):
    """A changed basename matches the platform's protected secret patterns."""

    exit_code = EXIT_SECRET_VIOLATION
    safe_code = "engineering_path_secret_violation"


class EngineeringPathScopeViolation(EngineeringPathPolicyError):
    """A changed path or final entry type is outside the approved policy."""

    exit_code = EXIT_PATH_VIOLATION
    safe_code = "engineering_path_scope_violation"


class EngineeringPathContractError(EngineeringPathPolicyError):
    """The policy, verifier, Git range, or Git output is not trustworthy."""

    exit_code = EXIT_CONTRACT_VIOLATION
    safe_code = "engineering_path_contract_violation"


def _contract_error() -> NoReturn:
    raise EngineeringPathContractError()


def _scope_error() -> NoReturn:
    raise EngineeringPathScopeViolation()


def _is_safe_unicode(value: str) -> bool:
    try:
        encoded = value.encode("utf-8", errors="strict")
        round_trip = encoded.decode("utf-8", errors="strict")
    except UnicodeError:
        return False
    if round_trip != value or unicodedata.normalize("NFC", value) != value:
        return False
    # Unicode category C includes controls, format controls, surrogates,
    # private-use code points and currently unassigned code points.  None are
    # needed in a repository policy and all make review or matching ambiguous.
    return not any(unicodedata.category(character).startswith("C") for character in value)


def _validate_canonical_scope(value: object) -> str:
    if type(value) is not str or not value or not _is_safe_unicode(value):
        _contract_error()
    if (
        value != value.strip()
        or "\\" in value
        or value.startswith(("/", "~"))
        or re.match(r"[A-Za-z]:", value) is not None
        or len(value.encode("utf-8")) > _MAX_SCOPE_UTF8_BYTES
    ):
        _contract_error()
    if value == ".":
        return value
    # A terminal slash is the deliberate subtree marker.  It is not stripped:
    # ``app/`` and the exact-file rule ``app`` have different meanings.
    path_part = value[:-1] if value.endswith("/") else value
    if not path_part:
        _contract_error()
    components = path_part.split("/")
    if any(not component or component in {".", ".."} for component in components):
        _contract_error()
    if any(component.casefold() == ".git" for component in components):
        _contract_error()
    return value


def _scope_sort_key(scope: str) -> bytes:
    return scope.encode("utf-8")


def _scope_matches(scope: str, path: str) -> bool:
    if scope == ".":
        return True
    if scope.endswith("/"):
        subtree_root = scope[:-1]
        return path == subtree_root or path.startswith(scope)
    return path == scope


def _canonicalize_scope_iterable(
    values: Iterable[str],
    *,
    required: bool,
    budget: list[int] | None = None,
) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        _contract_error()
    # Bound the raw iterable before constructing a set.  Otherwise an attacker
    # could submit an arbitrarily large stream of duplicate rules that reduces
    # to one canonical value only after consuming unbounded work/memory.
    shared_budget = budget if budget is not None else [0, 0]
    validated_values: list[str] = []
    for value in values:
        shared_budget[0] += 1
        if shared_budget[0] > _MAX_SCOPE_RULES:
            _contract_error()
        validated = _validate_canonical_scope(value)
        shared_budget[1] += len(validated.encode("utf-8"))
        if shared_budget[1] > _MAX_TOTAL_SCOPE_UTF8_BYTES:
            _contract_error()
        validated_values.append(validated)
    unique = sorted(set(validated_values), key=_scope_sort_key)
    if "." in unique:
        reduced = ["."]
    else:
        # Reduce subtree rules first.  An exact rule never covers descendants;
        # a subtree covers its own directory entry and every descendant.
        subtrees: list[str] = []
        for scope in (item for item in unique if item.endswith("/")):
            path = scope[:-1]
            if any(_scope_matches(parent, path) for parent in subtrees):
                continue
            subtrees.append(scope)
        exact = [
            scope
            for scope in unique
            if not scope.endswith("/")
            and not any(_scope_matches(parent, scope) for parent in subtrees)
        ]
        reduced = sorted([*subtrees, *exact], key=_scope_sort_key)
    if required and not reduced:
        _contract_error()
    return reduced


def _validate_canonical_scope_list(value: object, *, required: bool) -> list[str]:
    if type(value) is not list:
        _contract_error()
    canonical = _canonicalize_scope_iterable(value, required=required)
    if canonical != value:
        _contract_error()
    return list(canonical)


def _validate_scope_bounds(allowed: Sequence[str], prohibited: Sequence[str]) -> None:
    scopes = [*allowed, *prohibited]
    if len(scopes) > _MAX_SCOPE_RULES:
        _contract_error()
    if sum(len(scope.encode("utf-8")) for scope in scopes) > _MAX_TOTAL_SCOPE_UTF8_BYTES:
        _contract_error()


def _read_regular_file(path: os.PathLike[str] | str, *, maximum: int) -> bytes:
    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            _contract_error()
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        final_metadata = os.fstat(descriptor)
        if len(payload) > maximum or (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ) != (
            final_metadata.st_dev,
            final_metadata.st_ino,
            final_metadata.st_size,
            final_metadata.st_mtime_ns,
            final_metadata.st_ctime_ns,
        ):
            _contract_error()
        return bytes(payload)
    except EngineeringPathPolicyError:
        raise
    except (OSError, ValueError, TypeError):
        _contract_error()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def current_engineering_path_verifier_contract() -> dict[str, str]:
    """Return the version and SHA-256 of the exact currently executing source."""

    source = engineering_path_verifier_source().encode("utf-8", errors="strict")
    return {
        "version": VERIFIER_VERSION,
        "source_sha256": hashlib.sha256(source).hexdigest(),
    }


def engineering_path_verifier_source() -> str:
    """Return the exact verifier source for safe, byte-identical staging."""

    source = _read_regular_file(Path(__file__), maximum=_MAX_VERIFIER_SOURCE_BYTES)
    try:
        decoded = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _contract_error()
    if decoded.encode("utf-8", errors="strict") != source:
        _contract_error()
    return decoded


def validate_engineering_path_verifier_contract(
    contract: Mapping[str, Any],
) -> dict[str, str]:
    """Fail closed unless ``contract`` binds this exact verifier source."""

    if type(contract) is not dict or set(contract) != _VERIFIER_KEYS:
        _contract_error()
    version = contract.get("version")
    source_sha256 = contract.get("source_sha256")
    if (
        type(version) is not str
        or version != VERIFIER_VERSION
        or type(source_sha256) is not str
        or _SHA256_RE.fullmatch(source_sha256) is None
    ):
        _contract_error()
    current = current_engineering_path_verifier_contract()
    if not hmac.compare_digest(source_sha256, current["source_sha256"]):
        _contract_error()
    return dict(current)


def canonical_engineering_path_inputs(policy: Mapping[str, Any]) -> dict[str, list[str]]:
    """Extract strict, canonical path inputs from a policy mapping.

    Lists must already be UTF-8/NFC POSIX scopes in canonical byte order, with
    duplicates and redundant descendants removed.  ``.`` is the sole spelling
    for repository root and the allowlist must never be empty.
    """

    if not isinstance(policy, Mapping):
        _contract_error()
    allowed = _validate_canonical_scope_list(policy.get("allowed_paths"), required=True)
    prohibited = _validate_canonical_scope_list(
        policy.get("prohibited_paths"), required=False
    )
    _validate_scope_bounds(allowed, prohibited)
    return {"allowed_paths": allowed, "prohibited_paths": prohibited}


def _validated_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    if type(policy) is not dict or set(policy) != _POLICY_KEYS:
        _contract_error()
    if (
        policy.get("contract_version") != CONTRACT_VERSION
        or policy.get("policy_version") != POLICY_VERSION
        or policy.get("tree_semantics") != TREE_SEMANTICS
        or policy.get("path_matching") != PATH_MATCHING
        or policy.get("protected_secret_basenames_version")
        != PROTECTED_SECRET_BASENAMES_VERSION
    ):
        _contract_error()
    inputs = canonical_engineering_path_inputs(policy)
    verifier = validate_engineering_path_verifier_contract(policy.get("verifier"))
    return {
        "contract_version": CONTRACT_VERSION,
        "policy_version": POLICY_VERSION,
        "tree_semantics": TREE_SEMANTICS,
        "path_matching": PATH_MATCHING,
        "allowed_paths": inputs["allowed_paths"],
        "prohibited_paths": inputs["prohibited_paths"],
        "protected_secret_basenames_version": PROTECTED_SECRET_BASENAMES_VERSION,
        "verifier": verifier,
    }


def _canonical_policy_bytes(policy: Mapping[str, Any]) -> bytes:
    validated = _validated_policy(policy)
    try:
        rendered = json.dumps(
            validated,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
        if len(rendered) > _MAX_POLICY_BYTES:
            _contract_error()
        return rendered
    except (TypeError, ValueError, UnicodeError):
        _contract_error()


def render_engineering_path_policy_file(policy: Mapping[str, Any]) -> str:
    """Render the one canonical JSON representation used for staging/digesting."""

    return _canonical_policy_bytes(policy).decode("utf-8")


def build_engineering_path_policy(
    allowed_paths: Iterable[str], prohibited_paths: Iterable[str]
) -> tuple[dict[str, Any], str]:
    """Build a canonical v2 policy and its independently persisted SHA-256."""

    raw_budget = [0, 0]
    allowed = _canonicalize_scope_iterable(
        allowed_paths, required=True, budget=raw_budget
    )
    prohibited = _canonicalize_scope_iterable(
        prohibited_paths, required=False, budget=raw_budget
    )
    _validate_scope_bounds(allowed, prohibited)
    policy: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "policy_version": POLICY_VERSION,
        "tree_semantics": TREE_SEMANTICS,
        "path_matching": PATH_MATCHING,
        "allowed_paths": allowed,
        "prohibited_paths": prohibited,
        "protected_secret_basenames_version": PROTECTED_SECRET_BASENAMES_VERSION,
        "verifier": current_engineering_path_verifier_contract(),
    }
    rendered = render_engineering_path_policy_file(policy)
    return policy, hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def validate_engineering_path_policy(
    policy: Mapping[str, Any], policy_sha256: str
) -> dict[str, Any]:
    """Validate shape, canonical representation, source binding and digest."""

    if type(policy_sha256) is not str or _SHA256_RE.fullmatch(policy_sha256) is None:
        _contract_error()
    validated = _validated_policy(policy)
    actual = hashlib.sha256(_canonical_policy_bytes(validated)).hexdigest()
    if not hmac.compare_digest(policy_sha256, actual):
        _contract_error()
    return validated


def _is_protected_secret_basename(path: str) -> bool:
    basename = path.rsplit("/", 1)[-1].casefold()
    return (
        basename in {".env", "auth.json"}
        or basename.endswith((".pem", ".key"))
        or basename.startswith(("credentials", "secrets", "id_rsa", "id_ed25519"))
    )


def _decode_changed_path(raw_path: bytes) -> str:
    try:
        path = raw_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _scope_error()
    if (
        not path
        or not _is_safe_unicode(path)
        or path != path.strip()
        or "\\" in path
        or path.startswith(("/", "~"))
        or re.match(r"[A-Za-z]:", path) is not None
        or len(raw_path) > _MAX_SCOPE_UTF8_BYTES
    ):
        _scope_error()
    if path.startswith("/") or path.endswith("/") or path == ".":
        _scope_error()
    components = path.split("/")
    if any(not component or component in {".", ".."} for component in components):
        _scope_error()
    if any(component.casefold() == ".git" for component in components):
        _scope_error()
    return path


def _parse_raw_diff(output: bytes) -> list[tuple[str, str, str]]:
    """Parse ``git diff-tree --raw -z --no-renames`` without path quoting."""

    if not output:
        return []
    fields = output.split(b"\0")
    if fields[-1] != b"":
        _contract_error()
    fields.pop()
    if len(fields) % 2:
        _contract_error()
    if len(fields) // 2 > _MAX_CHANGED_PATHS:
        _contract_error()
    parsed: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for index in range(0, len(fields), 2):
        match = _RAW_HEADER_RE.fullmatch(fields[index])
        if match is None:
            _contract_error()
        old_mode = match.group(1).decode("ascii")
        new_mode = match.group(2).decode("ascii")
        status_code = match.group(5).decode("ascii")
        if status_code not in {"A", "D", "M", "T"}:
            _contract_error()
        if status_code == "D":
            if new_mode != _ZERO_MODE:
                _contract_error()
        else:
            if new_mode not in _REGULAR_FILE_MODES:
                _scope_error()
            if old_mode != _ZERO_MODE and old_mode not in _REGULAR_FILE_MODES:
                _scope_error()
        path = _decode_changed_path(fields[index + 1])
        if path in seen:
            _contract_error()
        seen.add(path)
        parsed.append((old_mode, new_mode, path))
    return parsed


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_argv_bounded(
    argv: Sequence[str], *, maximum_output: int, timeout: float
) -> bytes:
    """Run a fixed argv command with a hard wall-clock and stdout read bound."""

    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - argv only; no shell
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if process.stdout is None:
            _terminate_process(process)
            _contract_error()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        output = bytearray()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                _contract_error()
            events = selector.select(min(remaining, 0.25))
            if not events:
                continue
            for key, _mask in events:
                chunk = os.read(key.fd, min(64 * 1024, maximum_output + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > maximum_output:
                    _terminate_process(process)
                    _contract_error()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process(process)
            _contract_error()
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            _contract_error()
        if return_code != 0:
            _contract_error()
        return bytes(output)
    except EngineeringPathPolicyError:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        if process is not None:
            _terminate_process(process)
        _contract_error()
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.stdout is not None:
            process.stdout.close()


def _git_argv(repo: str, *, bare: bool, arguments: Sequence[str]) -> list[str]:
    if type(repo) is not str or not repo or "\x00" in repo:
        _contract_error()
    if bare:
        return ["git", f"--git-dir={repo}", *arguments]
    return ["git", "-C", repo, *arguments]


def verify_engineering_git_range(
    git_dir_or_repo: os.PathLike[str] | str,
    base_commit: str,
    result_commit: str,
    policy: Mapping[str, Any],
    policy_sha256: str,
    *,
    bare: bool = False,
) -> None:
    """Verify every final-tree change in ``base_commit`` → ``result_commit``.

    Prohibited scopes take precedence over allowed scopes.  Deletions are
    accepted when in scope; every added or modified final entry must be a
    regular 0644/0755 blob.  Symlinks and gitlinks are consequently rejected.
    """

    validated = validate_engineering_path_policy(policy, policy_sha256)
    if type(base_commit) is not str or _COMMIT_RE.fullmatch(base_commit) is None:
        _contract_error()
    if type(result_commit) is not str or _COMMIT_RE.fullmatch(result_commit) is None:
        _contract_error()
    try:
        repo = os.fspath(git_dir_or_repo)
    except TypeError:
        _contract_error()
    if type(repo) is not str:
        _contract_error()

    for commit in (base_commit, result_commit):
        _run_argv_bounded(
            _git_argv(repo, bare=bare, arguments=["cat-file", "-e", f"{commit}^{{commit}}"]),
            maximum_output=0,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    _run_argv_bounded(
        _git_argv(
            repo,
            bare=bare,
            arguments=["merge-base", "--is-ancestor", base_commit, result_commit],
        ),
        maximum_output=0,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    raw_diff = _run_argv_bounded(
        _git_argv(
            repo,
            bare=bare,
            arguments=[
                "diff-tree",
                "--raw",
                "-z",
                "--no-renames",
                "--no-commit-id",
                "-r",
                base_commit,
                result_commit,
                "--",
            ],
        ),
        maximum_output=_MAX_GIT_OUTPUT_BYTES,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    allowed = validated["allowed_paths"]
    prohibited = validated["prohibited_paths"]
    for _old_mode, _new_mode, path in _parse_raw_diff(raw_diff):
        if _is_protected_secret_basename(path):
            raise EngineeringPathSecretViolation()
        if any(_scope_matches(scope, path) for scope in prohibited):
            _scope_error()
        if not any(_scope_matches(scope, path) for scope in allowed):
            _scope_error()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _contract_error()
        result[key] = value
    return result


def _load_policy_file(path: str) -> dict[str, Any]:
    payload = _read_regular_file(path, maximum=_MAX_POLICY_BYTES)
    try:
        decoded = payload.decode("utf-8", errors="strict")
        if decoded.encode("utf-8") != payload:
            _contract_error()
        parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except EngineeringPathPolicyError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        _contract_error()
    if type(parsed) is not dict:
        _contract_error()
    # Reject alternate whitespace/key-order encodings even when their parsed
    # value is equivalent.  There is exactly one approved byte representation.
    if render_engineering_path_policy_file(parsed).encode("utf-8") != payload:
        _contract_error()
    return parsed


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # noqa: ARG002 - never expose argv
        _contract_error()


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="engineering-path-policy")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check-only", "check"):
        check = commands.add_parser(name)
        check.add_argument("--policy", required=True)
        check.add_argument("--policy-sha256", required=True)
        check.add_argument("--verifier-sha256", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--policy", required=True)
    verify.add_argument("--policy-sha256", required=True)
    verify.add_argument("--verifier-sha256", required=True)
    verify.add_argument("--repo", required=True)
    verify.add_argument("--base", required=True)
    verify.add_argument("--result", required=True)
    verify.add_argument("--bare", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone verifier entry point with fixed, non-sensitive diagnostics."""

    try:
        arguments = _build_cli_parser().parse_args(argv)
        policy = _load_policy_file(arguments.policy)
        validated = validate_engineering_path_policy(
            policy, arguments.policy_sha256
        )
        if (
            type(arguments.verifier_sha256) is not str
            or _SHA256_RE.fullmatch(arguments.verifier_sha256) is None
            or not hmac.compare_digest(
                arguments.verifier_sha256,
                validated["verifier"]["source_sha256"],
            )
        ):
            _contract_error()
        if arguments.command in {"check-only", "check"}:
            pass
        elif arguments.command == "verify":
            verify_engineering_git_range(
                arguments.repo,
                arguments.base,
                arguments.result,
                policy,
                arguments.policy_sha256,
                bare=arguments.bare,
            )
        else:  # pragma: no cover - argparse's required subparser prevents this
            _contract_error()
    except EngineeringPathPolicyError as exc:
        print(exc.safe_code, file=sys.stderr)
        return exc.exit_code
    print("engineering_path_policy_verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
