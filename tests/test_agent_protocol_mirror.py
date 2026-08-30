"""The runner package and Server A share one wire protocol; the control-plane
copy must be byte-identical to the runner's canonical module (neither wheel
imports the other)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_control_plane_agent_protocol_mirrors_the_runner_module():
    canonical = (ROOT / "dispatch_agent" / "protocol.py").read_bytes()
    mirror = (ROOT / "dispatch_center" / "agent_protocol.py").read_bytes()
    assert canonical == mirror
