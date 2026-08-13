from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from agent import __version__ as node_agent_version
from dispatch_center import __version__ as control_plane_version
from scripts.check_requirements_lock import _parse_requirements


ROOT = Path(__file__).resolve().parents[1]


def _metadata(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _requirements(values: list[str]) -> dict[str, Requirement]:
    return {
        canonicalize_name(requirement.name): requirement
        for requirement in map(Requirement, values)
    }


def test_control_plane_metadata_matches_core_runtime_manifest():
    metadata = _metadata(ROOT / "pyproject.toml")
    packaged = _requirements(metadata["project"]["dependencies"])
    manifested = _parse_requirements(ROOT / "requirements.txt")

    assert packaged.keys() == manifested.keys()
    for name, requirement in packaged.items():
        assert requirement.specifier == manifested[name].specifier


def test_development_manifest_is_the_test_and_dev_extras_aggregate():
    metadata = _metadata(ROOT / "pyproject.toml")
    extras = metadata["project"]["optional-dependencies"]
    expected = _requirements([*extras["test"], *extras["dev"]])

    assert _parse_requirements(ROOT / "requirements-dev.txt") == expected


def test_control_plane_package_version_and_boundaries_are_explicit():
    metadata = _metadata(ROOT / "pyproject.toml")

    assert metadata["project"]["version"] == control_plane_version
    assert set(metadata["tool"]["setuptools"]["packages"]) == {
        "app",
        "app.settings",
        "dispatch_center",
        "dispatch_center.infrastructure",
        "dispatch_center.infrastructure.db",
        "dispatch_center.api",
        "dispatch_center.api.routers",
        "dispatch_center_web",
    }
    assert "agent" not in metadata["tool"]["setuptools"]["packages"]


def test_node_agent_is_a_dependency_free_independent_distribution():
    metadata = _metadata(ROOT / "agent" / "pyproject.toml")

    assert metadata["project"]["name"] == "dispatch-node-agent"
    assert metadata["project"]["dependencies"] == []
    assert metadata["project"]["dynamic"] == ["version"]
    assert metadata["project"]["scripts"] == {
        "dispatch-node-agent": "agent.__main__:main"
    }
    assert metadata["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "agent.__version__"
    }
    assert node_agent_version == "1.0.0"
