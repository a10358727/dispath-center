from pathlib import Path

from scripts.check_requirements_lock import check_lock


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_lock_check_accepts_exact_compatible_direct_pins(tmp_path):
    manifest = _write(
        tmp_path / "requirements.txt",
        "Example_Pkg[extra]>=2.0,<3.0\npytest>=8.0\n",
    )
    lock = _write(
        tmp_path / "requirements.lock",
        "example-pkg==2.4.1 \\\n"
        "    --hash=sha256:ignored-by-parser\n"
        "pytest==9.1.1\n"
        "transitive==1.0\n",
    )

    assert check_lock(manifest, lock) == []


def test_lock_check_reports_missing_direct_requirement(tmp_path):
    manifest = _write(tmp_path / "requirements.txt", "httpx2>=2.9,<3.0\n")
    lock = _write(tmp_path / "requirements.lock", "httpx==0.28.1\n")

    assert check_lock(manifest, lock) == [
        f"httpx2: missing from {lock}",
    ]


def test_lock_check_reports_incompatible_direct_pin(tmp_path):
    manifest = _write(tmp_path / "requirements.txt", "fastapi>=0.110,<1.0\n")
    lock = _write(tmp_path / "requirements.lock", "fastapi==1.0.0\n")

    assert check_lock(manifest, lock) == [
        "fastapi: locked 1.0.0 does not satisfy <1.0,>=0.110",
    ]


def test_lock_check_rejects_non_exact_lock_entry(tmp_path):
    manifest = _write(tmp_path / "requirements.txt", "pytest>=8.0\n")
    lock = _write(tmp_path / "requirements.lock", "pytest>=8.0\n")

    assert check_lock(manifest, lock) == [
        "pytest: lock entry is not one exact == pin: pytest>=8.0",
    ]


def test_lock_check_accepts_constraint_and_requirement_directives(tmp_path):
    manifest = _write(
        tmp_path / "requirements-dev.txt",
        "-c requirements.lock\n-r shared-tools.txt\nruff>=0.12,<1.0\n",
    )
    lock = _write(tmp_path / "requirements-dev.lock", "ruff==0.16.1\n")

    assert check_lock(manifest, lock) == []
