from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from app.engineering_path_policy import (
    CONTRACT_VERSION,
    EXIT_CONTRACT_VIOLATION,
    EXIT_PATH_VIOLATION,
    EXIT_SECRET_VIOLATION,
    EngineeringPathContractError,
    EngineeringPathScopeViolation,
    EngineeringPathSecretViolation,
    _parse_raw_diff,
    build_engineering_path_policy,
    canonical_engineering_path_inputs,
    current_engineering_path_verifier_contract,
    engineering_path_verifier_source,
    render_engineering_path_policy_file,
    validate_engineering_path_policy,
    validate_engineering_path_verifier_contract,
    verify_engineering_git_range,
)


def _git(repo: Path, *arguments: str, capture: bool = False) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip() if capture else ""


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "dispatch@example.invalid")
    _git(repo, "config", "user.name", "Dispatch Test")
    (repo / "seed.txt").write_text("base\n", encoding="utf-8")
    return repo, _commit(repo, "base")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD", capture=True)


def _commit_index(repo: Path, message: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD", capture=True)


def _policy_file(tmp_path: Path, policy: dict[str, object]) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(render_engineering_path_policy_file(policy), encoding="utf-8")
    return path


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    source = Path(__file__).parents[1] / "app" / "engineering_path_policy.py"
    return subprocess.run(
        [sys.executable, str(source), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )


def _raw_record(
    path: bytes,
    *,
    old_mode: bytes = b"000000",
    new_mode: bytes = b"100644",
    status: bytes = b"A",
) -> bytes:
    return (
        b":"
        + old_mode
        + b" "
        + new_mode
        + b" "
        + b"0" * 40
        + b" "
        + b"1" * 40
        + b" "
        + status
        + b"\0"
        + path
        + b"\0"
    )


def test_build_policy_is_canonical_deterministic_and_digest_bound():
    policy, digest = build_engineering_path_policy(
        ["tests/x.py", "app/sub/", "app", "app/", "app/x.py", "app/"],
        ["vendor/cache/", "vendor/cache/item", "generated.lock"],
    )

    assert policy["contract_version"] == CONTRACT_VERSION
    assert policy["allowed_paths"] == ["app/", "tests/x.py"]
    assert policy["prohibited_paths"] == ["generated.lock", "vendor/cache/"]
    rendered = render_engineering_path_policy_file(policy)
    assert rendered == json.dumps(
        policy,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert digest == hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    assert validate_engineering_path_policy(policy, digest) == policy


def test_root_is_the_only_canonical_catch_all_rule():
    policy, _digest = build_engineering_path_policy(
        ["app/", ".", "tests/x.py"], ["private/", ".", "secret.txt"]
    )
    assert canonical_engineering_path_inputs(policy) == {
        "allowed_paths": ["."],
        "prohibited_paths": ["."],
    }


def test_exact_rule_does_not_cover_a_descendant():
    policy, _digest = build_engineering_path_policy(["app", "app/x.py"], [])
    assert policy["allowed_paths"] == ["app", "app/x.py"]


@pytest.mark.parametrize(
    "scope",
    [
        "",
        " app/",
        "app/ ",
        "/app/",
        "~/app/",
        "~project",
        "C:/project",
        "C:\\project",
        "app//x",
        "app/./x",
        "app/../x",
        "app\\x",
        "app\nx",
        "app\x00x",
        ".git/",
        "app/.GIT/config",
        unicodedata.normalize("NFD", "café.txt"),
        "\ue000.txt",
        "a" * 1025,
    ],
)
def test_build_rejects_noncanonical_or_unreviewable_scope(scope: str):
    with pytest.raises(EngineeringPathContractError) as caught:
        build_engineering_path_policy([scope], [])
    assert caught.value.exit_code == EXIT_CONTRACT_VIOLATION


@pytest.mark.parametrize("value", [[], (), "app/", b"app/"])
def test_allowed_scope_must_be_a_nonempty_rule_iterable(value):
    with pytest.raises(EngineeringPathContractError):
        build_engineering_path_policy(value, [])


def test_policy_bounds_total_rule_count():
    with pytest.raises(EngineeringPathContractError):
        build_engineering_path_policy([f"file-{index}" for index in range(257)], [])


def test_policy_bounds_raw_duplicate_rules_before_set_construction():
    with pytest.raises(EngineeringPathContractError):
        build_engineering_path_policy(("app/" for _index in range(257)), [])


def test_policy_bounds_raw_rules_across_allowed_and_prohibited_lists():
    with pytest.raises(EngineeringPathContractError):
        build_engineering_path_policy(["app/"] * 200, ["private/"] * 57)


def test_policy_bounds_total_utf8_bytes():
    scopes = [f"{index:03d}-" + "x" * 1018 for index in range(65)]
    with pytest.raises(EngineeringPathContractError):
        build_engineering_path_policy(scopes, [])


def test_canonical_render_always_fits_staged_policy_file_bound():
    # Quotes are valid filename bytes but require the worst common JSON escape
    # expansion.  The aggregate scope bound leaves room for that expansion and
    # for the fixed policy/verifier fields.
    scopes = [f"{index:03d}-" + '"' * 1010 for index in range(24)]
    policy, _digest = build_engineering_path_policy(scopes, [])
    assert len(render_engineering_path_policy_file(policy).encode("utf-8")) <= 64 * 1024


def test_validator_rejects_digest_shape_drift_and_noncanonical_rule_order():
    policy, digest = build_engineering_path_policy(["app/", "tests/"], [])
    with pytest.raises(EngineeringPathContractError):
        validate_engineering_path_policy(policy, "A" * 64)
    with pytest.raises(EngineeringPathContractError):
        validate_engineering_path_policy(policy, "0" * 64)

    reordered = copy.deepcopy(policy)
    reordered["allowed_paths"] = ["tests/", "app/"]
    with pytest.raises(EngineeringPathContractError):
        validate_engineering_path_policy(reordered, digest)


def test_validator_rejects_unknown_field_and_source_contract_drift():
    policy, digest = build_engineering_path_policy(["."], [])
    extra = copy.deepcopy(policy)
    extra["unapproved"] = True
    with pytest.raises(EngineeringPathContractError):
        validate_engineering_path_policy(extra, digest)

    stale = copy.deepcopy(policy)
    stale["verifier"]["source_sha256"] = "0" * 64
    with pytest.raises(EngineeringPathContractError):
        validate_engineering_path_policy(stale, digest)


def test_staged_source_and_current_contract_are_byte_identical():
    source = engineering_path_verifier_source()
    assert source.encode("utf-8").decode("utf-8") == source
    contract = current_engineering_path_verifier_contract()
    assert contract["source_sha256"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert validate_engineering_path_verifier_contract(contract) == contract


def test_final_tree_allows_exact_file_and_subtree_changes(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    (repo / "app").mkdir()
    (repo / "app" / "one.py").write_text("one\n", encoding="utf-8")
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    result = _commit(repo, "allowed")
    policy, digest = build_engineering_path_policy(["app/", "README.md"], [])

    assert verify_engineering_git_range(repo, base, result, policy, digest) is None


def test_exact_scope_does_not_allow_sibling_or_descendant(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "x.py").write_text("x\n", encoding="utf-8")
    result = _commit(repo, "outside exact")
    policy, digest = build_engineering_path_policy(["tests"], [])

    with pytest.raises(EngineeringPathScopeViolation):
        verify_engineering_git_range(repo, base, result, policy, digest)


def test_subtree_rule_matches_the_root_entry_itself(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    (repo / "app").write_text("file at subtree root\n", encoding="utf-8")
    result = _commit(repo, "root entry")
    policy, digest = build_engineering_path_policy(["app/"], [])
    verify_engineering_git_range(repo, base, result, policy, digest)


def test_matching_is_case_sensitive(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    (repo / "App").mkdir()
    (repo / "App" / "one.py").write_text("one\n", encoding="utf-8")
    result = _commit(repo, "wrong case")
    policy, digest = build_engineering_path_policy(["app/"], [])

    with pytest.raises(EngineeringPathScopeViolation):
        verify_engineering_git_range(repo, base, result, policy, digest)


def test_prohibited_subtree_takes_precedence_over_allowed_root(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    target = repo / "app" / "private" / "one.py"
    target.parent.mkdir(parents=True)
    target.write_text("one\n", encoding="utf-8")
    result = _commit(repo, "prohibited")
    policy, digest = build_engineering_path_policy(["."], ["app/private/"])

    with pytest.raises(EngineeringPathScopeViolation) as caught:
        verify_engineering_git_range(repo, base, result, policy, digest)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "basename",
    [
        ".env",
        "AUTH.JSON",
        "tls.PEM",
        "signing.Key",
        "credentials-prod.json",
        "Secrets.toml",
        "id_rsa.pub",
        "ID_ED25519_backup",
    ],
)
def test_protected_secret_basename_precedes_path_allowlist(
    tmp_path: Path, basename: str
):
    repo, base = _init_repo(tmp_path)
    directory = repo / "allowed"
    directory.mkdir()
    (directory / basename).write_text("not a real secret\n", encoding="utf-8")
    result = _commit(repo, "secret shaped name")
    policy, digest = build_engineering_path_policy(["."], [])

    with pytest.raises(EngineeringPathSecretViolation) as caught:
        verify_engineering_git_range(repo, base, result, policy, digest)
    assert caught.value.exit_code == EXIT_SECRET_VIOLATION
    assert basename.casefold() not in str(caught.value).casefold()


def test_regular_executable_and_regular_deletion_are_allowed(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    seed = repo / "seed.txt"
    seed.unlink()
    script = repo / "tool.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    result = _commit(repo, "regular modes")
    policy, digest = build_engineering_path_policy(["."], [])
    verify_engineering_git_range(repo, base, result, policy, digest)


def test_added_symlink_is_rejected(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    os.symlink("seed.txt", repo / "link")
    result = _commit(repo, "symlink")
    policy, digest = build_engineering_path_policy(["."], [])

    with pytest.raises(EngineeringPathScopeViolation):
        verify_engineering_git_range(repo, base, result, policy, digest)


def test_deleted_symlink_is_accepted(tmp_path: Path):
    repo, _initial = _init_repo(tmp_path)
    os.symlink("seed.txt", repo / "link")
    base = _commit(repo, "symlink base")
    (repo / "link").unlink()
    result = _commit(repo, "delete symlink")
    policy, digest = build_engineering_path_policy(["."], [])
    verify_engineering_git_range(repo, base, result, policy, digest)


def test_replacing_symlink_with_regular_file_is_rejected(tmp_path: Path):
    repo, _initial = _init_repo(tmp_path)
    os.symlink("seed.txt", repo / "item")
    base = _commit(repo, "symlink base")
    (repo / "item").unlink()
    (repo / "item").write_text("regular\n", encoding="utf-8")
    result = _commit(repo, "replace symlink")
    policy, digest = build_engineering_path_policy(["."], [])

    with pytest.raises(EngineeringPathScopeViolation):
        verify_engineering_git_range(repo, base, result, policy, digest)


def test_added_gitlink_is_rejected(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor/module")
    result = _commit_index(repo, "gitlink")
    policy, digest = build_engineering_path_policy(["."], [])

    with pytest.raises(EngineeringPathScopeViolation):
        verify_engineering_git_range(repo, base, result, policy, digest)


def test_deleted_gitlink_is_accepted(tmp_path: Path):
    repo, initial = _init_repo(tmp_path)
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{initial},vendor/module")
    base = _commit_index(repo, "gitlink base")
    _git(repo, "update-index", "--force-remove", "vendor/module")
    result = _commit_index(repo, "delete gitlink")
    policy, digest = build_engineering_path_policy(["."], [])
    verify_engineering_git_range(repo, base, result, policy, digest)


@pytest.mark.parametrize("unsafe_name", ["line\nbreak.txt", "back\\slash.txt", " trailing"])
def test_control_backslash_and_whitespace_git_paths_fail_closed(
    tmp_path: Path, unsafe_name: str
):
    repo, base = _init_repo(tmp_path)
    (repo / unsafe_name).write_text("unsafe name\n", encoding="utf-8")
    result = _commit(repo, "unsafe path")
    policy, digest = build_engineering_path_policy(["."], [])

    with pytest.raises(EngineeringPathScopeViolation) as caught:
        verify_engineering_git_range(repo, base, result, policy, digest)
    assert unsafe_name not in str(caught.value)


def test_invalid_utf8_git_path_fails_closed(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    raw_repo = os.fsencode(repo)
    descriptor = os.open(raw_repo + b"/invalid-\xff", os.O_WRONLY | os.O_CREAT, 0o644)
    os.write(descriptor, b"invalid name\n")
    os.close(descriptor)
    result = _commit(repo, "invalid utf8 path")
    policy, digest = build_engineering_path_policy(["."], [])

    with pytest.raises(EngineeringPathScopeViolation):
        verify_engineering_git_range(repo, base, result, policy, digest)


def test_nfc_git_path_is_allowed_but_nfd_path_is_rejected(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    nfc_name = "café.txt"
    (repo / nfc_name).write_text("nfc\n", encoding="utf-8")
    nfc_result = _commit(repo, "nfc")
    policy, digest = build_engineering_path_policy([nfc_name], [])
    verify_engineering_git_range(repo, base, nfc_result, policy, digest)

    nfd_name = unicodedata.normalize("NFD", "résumé.txt")
    (repo / nfd_name).write_text("nfd\n", encoding="utf-8")
    nfd_result = _commit(repo, "nfd")
    root_policy, root_digest = build_engineering_path_policy(["."], [])
    with pytest.raises(EngineeringPathScopeViolation):
        verify_engineering_git_range(repo, nfc_result, nfd_result, root_policy, root_digest)


def test_rename_is_evaluated_as_delete_and_add_without_rename_inference(tmp_path: Path):
    repo, _initial = _init_repo(tmp_path)
    source = repo / "allowed.txt"
    source.write_text("same\n", encoding="utf-8")
    base = _commit(repo, "rename base")
    source.rename(repo / "outside.txt")
    result = _commit(repo, "rename")
    policy, digest = build_engineering_path_policy(["allowed.txt"], [])

    with pytest.raises(EngineeringPathScopeViolation):
        verify_engineering_git_range(repo, base, result, policy, digest)


def test_result_must_descend_from_exact_base(tmp_path: Path):
    repo, common = _init_repo(tmp_path)
    (repo / "left.txt").write_text("left\n", encoding="utf-8")
    left = _commit(repo, "left")
    _git(repo, "checkout", "-q", "--detach", common)
    (repo / "right.txt").write_text("right\n", encoding="utf-8")
    right = _commit(repo, "right")
    policy, digest = build_engineering_path_policy(["."], [])

    with pytest.raises(EngineeringPathContractError):
        verify_engineering_git_range(repo, left, right, policy, digest)


def test_bare_repository_verification_uses_same_policy(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    result = _commit(repo, "result")
    bare = tmp_path / "repo.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(repo), str(bare)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    policy, digest = build_engineering_path_policy(["app.py"], [])
    verify_engineering_git_range(bare, base, result, policy, digest, bare=True)


def test_raw_parser_rejects_nonregular_final_and_prior_modes():
    with pytest.raises(EngineeringPathScopeViolation):
        _parse_raw_diff(_raw_record(b"link", new_mode=b"120000"))
    with pytest.raises(EngineeringPathScopeViolation):
        _parse_raw_diff(
            _raw_record(
                b"item", old_mode=b"120000", new_mode=b"100644", status=b"T"
            )
        )


def test_raw_parser_accepts_delete_of_nonregular_entry():
    assert _parse_raw_diff(
        _raw_record(
            b"old-link", old_mode=b"120000", new_mode=b"000000", status=b"D"
        )
    ) == [("120000", "000000", "old-link")]


def test_raw_parser_bounds_changed_entry_count():
    record = _raw_record(b"same")
    with pytest.raises(EngineeringPathContractError):
        _parse_raw_diff(record * 10_001)


def test_cli_check_only_and_verify_are_standalone(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    (repo / "app.py").write_text("pass\n", encoding="utf-8")
    result = _commit(repo, "result")
    policy, digest = build_engineering_path_policy(["app.py"], [])
    policy_path = _policy_file(tmp_path, policy)

    checked = _run_cli(
        "check-only",
        "--policy",
        str(policy_path),
        "--policy-sha256",
        digest,
        "--verifier-sha256",
        policy["verifier"]["source_sha256"],
    )
    assert checked.returncode == 0
    assert checked.stdout.strip() == "engineering_path_policy_verified"
    assert checked.stderr == ""

    verified = _run_cli(
        "verify",
        "--policy",
        str(policy_path),
        "--policy-sha256",
        digest,
        "--verifier-sha256",
        policy["verifier"]["source_sha256"],
        "--repo",
        str(repo),
        "--base",
        base,
        "--result",
        result,
    )
    assert verified.returncode == 0
    assert verified.stdout.strip() == "engineering_path_policy_verified"
    assert verified.stderr == ""


def test_cli_uses_fixed_contract_exit_and_rejects_noncanonical_json(tmp_path: Path):
    policy, digest = build_engineering_path_policy(["."], [])
    path = tmp_path / "sensitive-policy-name.json"
    path.write_text(json.dumps(policy, indent=2), encoding="utf-8")

    completed = _run_cli(
        "check-only",
        "--policy",
        str(path),
        "--policy-sha256",
        digest,
        "--verifier-sha256",
        policy["verifier"]["source_sha256"],
    )
    assert completed.returncode == EXIT_CONTRACT_VIOLATION
    assert completed.stdout == ""
    assert completed.stderr.strip() == "engineering_path_contract_violation"
    assert path.name not in completed.stderr


def test_cli_rejects_verifier_digest_not_bound_by_approved_policy(tmp_path: Path):
    policy, digest = build_engineering_path_policy(["."], [])
    path = _policy_file(tmp_path, policy)

    completed = _run_cli(
        "check-only",
        "--policy",
        str(path),
        "--policy-sha256",
        digest,
        "--verifier-sha256",
        "0" * 64,
    )

    assert completed.returncode == EXIT_CONTRACT_VIOLATION
    assert completed.stdout == ""
    assert completed.stderr.strip() == "engineering_path_contract_violation"


def test_cli_uses_fixed_path_exit_without_exposing_changed_path(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    sensitive_name = "do-not-expose-this-name.txt"
    (repo / sensitive_name).write_text("outside\n", encoding="utf-8")
    result = _commit(repo, "outside")
    policy, digest = build_engineering_path_policy(["seed.txt"], [])
    policy_path = _policy_file(tmp_path, policy)

    completed = _run_cli(
        "verify",
        "--policy",
        str(policy_path),
        "--policy-sha256",
        digest,
        "--verifier-sha256",
        policy["verifier"]["source_sha256"],
        "--repo",
        str(repo),
        "--base",
        base,
        "--result",
        result,
    )
    assert completed.returncode == EXIT_PATH_VIOLATION
    assert completed.stdout == ""
    assert completed.stderr.strip() == "engineering_path_scope_violation"
    assert sensitive_name not in completed.stderr


def test_cli_uses_fixed_secret_exit_without_exposing_changed_path(tmp_path: Path):
    repo, base = _init_repo(tmp_path)
    sensitive_name = "credentials-do-not-expose.json"
    (repo / sensitive_name).write_text("shape only\n", encoding="utf-8")
    result = _commit(repo, "secret shape")
    policy, digest = build_engineering_path_policy(["."], [])
    policy_path = _policy_file(tmp_path, policy)

    completed = _run_cli(
        "verify",
        "--policy",
        str(policy_path),
        "--policy-sha256",
        digest,
        "--verifier-sha256",
        policy["verifier"]["source_sha256"],
        "--repo",
        str(repo),
        "--base",
        base,
        "--result",
        result,
    )
    assert completed.returncode == EXIT_SECRET_VIOLATION
    assert completed.stdout == ""
    assert completed.stderr.strip() == "engineering_path_secret_violation"
    assert sensitive_name not in completed.stderr
