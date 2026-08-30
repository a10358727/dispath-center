"""The runner wheel ships the MCP bridge as a byte-identical copy of
`app/mcp_bridge.py` so the two never drift (INV-LLM-4 applies to both)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runner_bridge_mirrors_the_control_plane_bridge():
    assert (ROOT / "dispatch_agent" / "mcp_bridge.py").read_bytes() == (ROOT / "app" / "mcp_bridge.py").read_bytes()
