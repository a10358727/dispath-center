#!/usr/bin/env python3
"""Create one pending, immutable WP-2D canary approval.

This operator tool does not approve the request, create a Job, or contact the
worker.  Review and approve the returned ID in the normal Approvals UI.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database
from app.wp2d_canary import request_wp2d_canary_approval


def _head_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("jobqueue.db"))
    parser.add_argument("--audit", type=Path, default=Path("audit.jsonl"))
    parser.add_argument("--server", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--candidate-commit", default=None)
    parser.add_argument("--gpus-needed", type=int, default=0)
    parser.add_argument("--priority", choices=("low", "normal"), default="low")
    parser.add_argument(
        "--acknowledge-non-production",
        action="store_true",
        help="Required acknowledgement that the selected worker carries no production work.",
    )
    args = parser.parse_args(argv)

    database = Database(str(args.db))
    try:
        approval = request_wp2d_canary_approval(
            database,
            server_name=args.server,
            candidate_commit=args.candidate_commit or _head_commit(),
            command=args.command,
            acknowledge_non_production=args.acknowledge_non_production,
            gpus_needed=args.gpus_needed,
            priority=args.priority,
            audit_path=str(args.audit),
        )
    finally:
        database.close()
    print(
        json.dumps(
            {
                "approval_id": approval.id,
                "status": approval.status,
                "payload_sha256": approval.payload_sha256,
                "contract_version": approval.payload_contract_version,
                "next": f"Review and approve #{approval.id} in Approvals",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
