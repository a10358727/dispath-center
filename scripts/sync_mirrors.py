"""Keep the wheel-boundary mirror files byte-identical (整頓 C9, DG-CONSOLIDATION-v1).

Two modules exist twice because the runner wheel (`dispatch_agent/`) must not
import the platform package and vice versa:

* `app/mcp_bridge.py` (canonical) -> `dispatch_agent/mcp_bridge.py`
* `dispatch_agent/protocol.py` (canonical) -> `dispatch_center/agent_protocol.py`

`--check` (default) exits 1 when any mirror differs from its canonical file;
`--write` copies each canonical file over its mirror. Edit only the canonical
side, then run `--write`. The pin tests (`tests/test_mcp_bridge.py`,
`tests/test_agent_protocol_mirror.py`) still guard the boundary in CI.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (canonical, mirror), relative to the repository root.
MIRROR_PAIRS: tuple[tuple[str, str], ...] = (
    ("app/mcp_bridge.py", "dispatch_agent/mcp_bridge.py"),
    ("dispatch_agent/protocol.py", "dispatch_center/agent_protocol.py"),
)


def drift(root: Path = ROOT) -> list[tuple[str, str]]:
    """Return the (canonical, mirror) pairs whose bytes differ or whose mirror is missing."""

    out = []
    for canonical, mirror in MIRROR_PAIRS:
        src = root / canonical
        dst = root / mirror
        if not dst.is_file() or src.read_bytes() != dst.read_bytes():
            out.append((canonical, mirror))
    return out


def write(root: Path = ROOT) -> list[tuple[str, str]]:
    """Copy every drifted canonical file over its mirror; return what was written."""

    written = drift(root)
    for canonical, mirror in written:
        shutil.copyfile(root / canonical, root / mirror)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail when a mirror differs (default)")
    mode.add_argument("--write", action="store_true", help="copy canonical files over their mirrors")
    args = parser.parse_args(argv)

    if args.write:
        for canonical, mirror in write():
            print(f"synced {mirror} <- {canonical}")
        return 0
    drifted = drift()
    for canonical, mirror in drifted:
        print(f"DRIFT: {mirror} differs from {canonical} (run scripts/sync_mirrors.py --write)")
    if drifted:
        return 1
    print("mirrors in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
