"""Verify that the built Control Plane, Node Agent and runner-agent wheels stay independent."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZipFile


def _single_wheel(directory: Path, prefix: str) -> Path:
    matches = sorted(directory.glob(f"{prefix}-*.whl"))
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one {prefix!r} wheel in {directory}, found {len(matches)}"
        )
    return matches[0]


def _member_with_suffix(members: set[str], suffix: str) -> str:
    matches = sorted(name for name in members if name.endswith(suffix))
    if len(matches) != 1:
        raise ValueError(f"expected one wheel member ending in {suffix!r}")
    return matches[0]


def check_wheels(directory: Path) -> list[str]:
    errors: list[str] = []
    try:
        control_wheel = _single_wheel(directory, "dispatch_center")
        node_wheel = _single_wheel(directory, "dispatch_node_agent")
        runner_wheel = _single_wheel(directory, "dispatch_agent")
    except ValueError as exc:
        return [str(exc)]

    with ZipFile(control_wheel) as archive:
        control_members = set(archive.namelist())
        required_control = {
            "app/main.py",
            "app/settings/model.py",
            "dispatch_center/api/errors.py",
            "dispatch_center/api/routers/projects.py",
            "dispatch_center/cli.py",
            "dispatch_center_web/workspace.html",
            "dispatch_center_web/workspace.css",
            "dispatch_center_web/workspace.js",
        }
        for member in sorted(required_control - control_members):
            errors.append(f"control-plane wheel is missing {member}")
        if any(name.startswith("agent/") for name in control_members):
            errors.append("control-plane wheel must not contain the Node Agent package")
        if any(name.startswith("dispatch_agent/") for name in control_members):
            errors.append("control-plane wheel must not contain the runner-agent package")
        if "dispatch_center_web/studio/index.html" not in control_members:
            errors.append("control-plane wheel is missing the built Studio (dispatch_center_web/studio/index.html)")

        try:
            entry_name = _member_with_suffix(control_members, ".dist-info/entry_points.txt")
            entries = archive.read(entry_name).decode("utf-8")
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"control-plane entry points are unreadable: {exc}")
        else:
            for command in ("dispatch =", "dispatch-api =", "dispatch-scheduler ="):
                if command not in entries:
                    errors.append(f"control-plane wheel is missing entry point {command[:-2]}")

    with ZipFile(node_wheel) as archive:
        node_members = set(archive.namelist())
        required_node = {
            "agent/__init__.py",
            "agent/__main__.py",
            "agent/client.py",
            "agent/runner.py",
            "agent/supervisor.py",
        }
        for member in sorted(required_node - node_members):
            errors.append(f"node-agent wheel is missing {member}")
        if any(
            name.startswith(("app/", "dispatch_center/", "dispatch_agent/")) for name in node_members
        ):
            errors.append("node-agent wheel must not contain Control Plane or runner-agent packages")

        try:
            metadata_name = _member_with_suffix(node_members, ".dist-info/METADATA")
            metadata = archive.read(metadata_name).decode("utf-8")
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"node-agent metadata is unreadable: {exc}")
        else:
            if any(line.startswith("Requires-Dist:") for line in metadata.splitlines()):
                errors.append("node-agent wheel must have no runtime dependencies")

        try:
            entry_name = _member_with_suffix(node_members, ".dist-info/entry_points.txt")
            entries = archive.read(entry_name).decode("utf-8")
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"node-agent entry points are unreadable: {exc}")
        else:
            if "dispatch-node-agent = agent.__main__:main" not in entries:
                errors.append("node-agent wheel is missing dispatch-node-agent entry point")

    with ZipFile(runner_wheel) as archive:
        runner_members = set(archive.namelist())
        required_runner = {
            "dispatch_agent/__init__.py",
            "dispatch_agent/__main__.py",
            "dispatch_agent/client.py",
            "dispatch_agent/config.py",
            "dispatch_agent/permissions.py",
            "dispatch_agent/protocol.py",
            "dispatch_agent/sdk_adapter.py",
            "dispatch_agent/workspace.py",
            "dispatch_agent/mcp_bridge.py",
        }
        for member in sorted(required_runner - runner_members):
            errors.append(f"runner-agent wheel is missing {member}")
        if any(name.startswith(("app/", "dispatch_center/", "agent/")) for name in runner_members):
            errors.append("runner-agent wheel must not contain Control Plane or Node Agent packages")
        try:
            entry_name = _member_with_suffix(runner_members, ".dist-info/entry_points.txt")
            entries = archive.read(entry_name).decode("utf-8")
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"runner-agent entry points are unreadable: {exc}")
        else:
            if "dispatch-agent =" not in entries:
                errors.append("runner-agent wheel is missing entry point dispatch-agent")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path("dist"))
    args = parser.parse_args()
    errors = check_wheels(args.directory)
    if errors:
        print("wheel boundary check: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("wheel boundary check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
