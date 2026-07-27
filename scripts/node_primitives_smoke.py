"""Import the existing Node protocol primitives without claiming a daemon."""

from __future__ import annotations

import importlib.util
import pathlib
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

    # Phase 0 recorded the honest boundary as "no runnable daemon exists".
    # Phase 4 supplied one, so the boundary moves rather than disappears: the
    # daemon must exist, must still import no control-plane module, and must
    # open no listener.  Deleting this check instead of moving it would leave
    # the agent's isolation unverified.
    if importlib.util.find_spec("agent.__main__") is None:
        raise SystemExit("Node primitives smoke: runnable daemon entry point is missing")

    import agent.__main__ as daemon_module

    daemon_source = pathlib.Path(daemon_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("socket.bind", ".listen(", "HTTPServer", "socketserver", "uvicorn"):
        if forbidden in daemon_source:
            raise SystemExit(
                f"Node primitives smoke: daemon contains an inbound primitive: {forbidden}"
            )

    imported_after_daemon = sorted(
        {name for name in sys.modules if name == "app" or name.startswith("app.")}
        - control_plane_before
    )
    if imported_after_daemon:
        raise SystemExit(
            "Node primitives smoke: daemon imported control-plane modules: "
            + ", ".join(imported_after_daemon)
        )

    print(
        "Node primitives smoke: PASS "
        f"(version={__version__}; daemon=runnable, outbound-only, "
        "activation still gated by DG-NODE-V2)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
