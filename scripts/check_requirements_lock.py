"""Check that every direct requirement is represented by a compatible lock pin."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


def _requirement_lines(path: Path) -> Iterable[tuple[int, str]]:
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("--hash="):
            continue
        yield line_number, line.removesuffix("\\").rstrip()


def _parse_requirements(path: Path) -> dict[str, Requirement]:
    parsed: dict[str, Requirement] = {}
    for line_number, line in _requirement_lines(path):
        try:
            requirement = Requirement(line)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: invalid requirement: {exc}") from exc
        name = canonicalize_name(requirement.name)
        if name in parsed:
            raise ValueError(f"{path}:{line_number}: duplicate requirement {name!r}")
        parsed[name] = requirement
    return parsed


def check_lock(manifest_path: Path, lock_path: Path) -> list[str]:
    direct = _parse_requirements(manifest_path)
    locked = _parse_requirements(lock_path)
    errors: list[str] = []

    for name, requirement in sorted(direct.items()):
        pin = locked.get(name)
        if pin is None:
            errors.append(f"{name}: missing from {lock_path}")
            continue

        specifiers = list(pin.specifier)
        if (
            len(specifiers) != 1
            or specifiers[0].operator != "=="
            or specifiers[0].version.endswith(".*")
        ):
            errors.append(f"{name}: lock entry is not one exact == pin: {pin}")
            continue

        try:
            locked_version = Version(specifiers[0].version)
        except InvalidVersion:
            errors.append(f"{name}: invalid locked version {specifiers[0].version!r}")
            continue

        if requirement.specifier and not requirement.specifier.contains(
            locked_version, prereleases=True
        ):
            errors.append(
                f"{name}: locked {locked_version} does not satisfy "
                f"{requirement.specifier}"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("requirements.txt")
    )
    parser.add_argument("--lock", type=Path, default=Path("requirements.lock"))
    args = parser.parse_args()

    try:
        errors = check_lock(args.manifest, args.lock)
    except (OSError, ValueError) as exc:
        print(f"requirements lock sync: FAIL: {exc}")
        return 1

    if errors:
        print("requirements lock sync: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "requirements lock sync: PASS "
        f"({len(_parse_requirements(args.manifest))} direct requirements)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
