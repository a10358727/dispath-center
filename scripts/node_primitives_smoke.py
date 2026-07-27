"""Import the existing Node protocol primitives without claiming a daemon."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))


def main() -> int:
    control_plane_before = {
        name for name in sys.modules if name == "app" or name.startswith("app.")
    }
    from agent import SUPPORTED_PROTOCOL_OPERATIONS, __version__
    from agent.client import NodeAgentClient, command_digest
    from agent.runner import AttemptStore, plan_restart

    required_operations = {
        "poll",
        "ack",
        "heartbeat",
        "terminal",
        "stop-ack",
        "artifacts",
    }
    if set(SUPPORTED_PROTOCOL_OPERATIONS) != required_operations:
        raise SystemExit("Node primitives smoke: protocol operation drift")
    if not __version__ or not callable(command_digest):
        raise SystemExit("Node primitives smoke: invalid package metadata")
    if not all(callable(value) for value in (NodeAgentClient, AttemptStore, plan_restart)):
        raise SystemExit("Node primitives smoke: expected primitive is not callable")

    imported_control_plane = sorted(
        {
            name for name in sys.modules if name == "app" or name.startswith("app.")
        }
        - control_plane_before
    )
    if imported_control_plane:
        raise SystemExit(
            "Node primitives smoke: agent imported control-plane modules: "
            + ", ".join(imported_control_plane)
        )

    # Phase 0 intentionally records the honest boundary: protocol/client/runner
    # primitives import, but no runnable ``python -m agent`` entry point exists.
    if importlib.util.find_spec("agent.__main__") is not None:
        raise SystemExit(
            "Node primitives smoke: runnable daemon appeared before the Phase 4 gate"
        )

    print(
        "Node primitives smoke: PASS "
        f"(version={__version__}; protocol-primitives-only; daemon=absent)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
