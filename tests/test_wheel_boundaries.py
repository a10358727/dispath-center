from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from scripts.check_wheel_boundaries import check_wheels


def _wheel(path: Path, members: dict[str, str]) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def _add_member(path: Path, member: str, value: str) -> None:
    with ZipFile(path, "a", ZIP_DEFLATED) as archive:
        archive.writestr(member, value)


def _replace_member(path: Path, member: str, value: str) -> None:
    replacement = path.with_suffix(".replacement.whl")
    with ZipFile(path) as source, ZipFile(replacement, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info, value if info.filename == member else source.read(info))
    replacement.replace(path)


def _valid_wheels(directory: Path) -> None:
    _wheel(
        directory / "dispatch_center-0.1.0-py3-none-any.whl",
        {
            "app/main.py": "",
            "app/settings/model.py": "",
            "dispatch_center/api/errors.py": "",
            "dispatch_center/api/routers/projects.py": "",
            "dispatch_center/cli.py": "",
            "dispatch_center_web/login.html": "",
            "dispatch_center_web/studio/index.html": "",
            "dispatch_center-0.1.0.dist-info/entry_points.txt": (
                "[console_scripts]\n"
                "dispatch = dispatch_center.cli:main\n"
                "dispatch-api = dispatch_center.cli:api\n"
                "dispatch-scheduler = dispatch_center.cli:scheduler\n"
                "dispatch-worker = dispatch_center.cli:worker\n"
            ),
        },
    )
    _wheel(
        directory / "dispatch_node_agent-1.0.0-py3-none-any.whl",
        {
            "agent/__init__.py": "",
            "agent/__main__.py": "",
            "agent/client.py": "",
            "agent/runner.py": "",
            "agent/supervisor.py": "",
            "dispatch_node_agent-1.0.0.dist-info/METADATA": (
                "Metadata-Version: 2.4\nName: dispatch-node-agent\nVersion: 1.0.0\n"
            ),
            "dispatch_node_agent-1.0.0.dist-info/entry_points.txt": (
                "[console_scripts]\n"
                "dispatch-node-agent = agent.__main__:main\n"
            ),
        },
    )
    _wheel(
        directory / "dispatch_agent-0.1.0-py3-none-any.whl",
        {
            "dispatch_agent/__init__.py": "",
            "dispatch_agent/__main__.py": "",
            "dispatch_agent/client.py": "",
            "dispatch_agent/config.py": "",
            "dispatch_agent/permissions.py": "",
            "dispatch_agent/protocol.py": "",
            "dispatch_agent/sdk_adapter.py": "",
            "dispatch_agent/workspace.py": "",
            "dispatch_agent/mcp_bridge.py": "",
            "dispatch_agent-0.1.0.dist-info/METADATA": (
                "Metadata-Version: 2.4\nName: dispatch-agent\nVersion: 0.1.0\n"
            ),
            "dispatch_agent-0.1.0.dist-info/entry_points.txt": (
                "[console_scripts]\n"
                "dispatch-agent = dispatch_agent.__main__:main\n"
            ),
        },
    )


def test_wheel_boundary_check_accepts_independent_distributions(tmp_path):
    _valid_wheels(tmp_path)

    assert check_wheels(tmp_path) == []


def test_wheel_boundary_check_rejects_control_plane_code_in_node_wheel(tmp_path):
    _valid_wheels(tmp_path)
    node_wheel = next(tmp_path.glob("dispatch_node_agent-*.whl"))
    with ZipFile(node_wheel, "a", ZIP_DEFLATED) as archive:
        archive.writestr("app/main.py", "")

    assert "node-agent wheel must not contain Control Plane or runner-agent packages" in check_wheels(
        tmp_path
    )


def test_wheel_boundary_check_rejects_node_runtime_dependencies(tmp_path):
    _valid_wheels(tmp_path)
    node_wheel = next(tmp_path.glob("dispatch_node_agent-*.whl"))
    _replace_member(
        node_wheel,
        "dispatch_node_agent-1.0.0.dist-info/METADATA",
        "Metadata-Version: 2.4\nRequires-Dist: fastapi\n",
    )

    assert "node-agent wheel must have no runtime dependencies" in check_wheels(tmp_path)


def test_wheel_boundary_check_rejects_control_plane_code_in_runner_wheel(tmp_path):
    _valid_wheels(tmp_path)
    runner_wheel = next(tmp_path.glob("dispatch_agent-*.whl"))
    _add_member(runner_wheel, "app/main.py", "")

    errors = check_wheels(tmp_path)

    assert "runner-agent wheel must not contain Control Plane or Node Agent packages" in errors


def test_wheel_boundary_check_requires_the_built_studio_in_the_control_wheel(tmp_path):
    _valid_wheels(tmp_path)
    control_wheel = next(tmp_path.glob("dispatch_center-*.whl"))
    with ZipFile(control_wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist() if not name.endswith("studio/index.html")}
    control_wheel.unlink()
    with ZipFile(control_wheel, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value)

    errors = check_wheels(tmp_path)

    assert "control-plane wheel is missing the built Studio (dispatch_center_web/studio/index.html)" in errors
