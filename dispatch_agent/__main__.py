"""dispatch-agent entry point.

    dispatch-agent --check            # validate config/credentials/SDK, no network
    dispatch-agent run                # connect to Server A and serve sessions
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from dispatch_agent import __version__
from dispatch_agent.card import probe_agent_card
from dispatch_agent.client import RunnerClient, build_session_host_factory
from dispatch_agent.config import AgentConfig, ConfigError, load_config, scrub_environment, sdk_environment
from dispatch_agent.sdk_adapter import SdkTypes

DEFAULT_CONFIG_DIR = Path("~/.config/dispatch-agent")


def _sdk_available() -> tuple[bool, str]:
    try:
        import claude_agent_sdk  # noqa: F401

        return True, getattr(claude_agent_sdk, "__version__", "?")
    except Exception as exc:  # noqa: BLE001
        return False, exc.__class__.__name__


def make_sdk_bindings(config: AgentConfig) -> tuple[Any, Any, SdkTypes]:
    """Import the SDK once and return (options_factory, client_factory, types)."""

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny

    bridge_path = config.mcp_bridge_path or Path(__file__).with_name("mcp_bridge.py")

    def options_factory(*, cwd: str, can_use_tool: Any, resume: Optional[str], mcp_config_path: Optional[Path] = None) -> Any:
        mcp_servers: dict[str, Any] = {}
        if mcp_config_path is not None:
            # The bundled bridge is a byte-identical mirror of app/mcp_bridge.py;
            # tools.json/token live next to the workspace (INV-LLM-4: stdio, HTTP only).
            mcp_servers["dispatch"] = {
                "type": "stdio",
                "command": config.runner_python,
                "args": [str(bridge_path), "--stdio", "--config", str(mcp_config_path)],
            }
        return ClaudeAgentOptions(
            cwd=cwd,
            env=sdk_environment(config),
            allowed_tools=[],
            permission_mode="default",
            can_use_tool=can_use_tool,
            mcp_servers=mcp_servers,
            strict_mcp_config=True,
            setting_sources=[],
            max_turns=config.max_turns,
            max_budget_usd=config.max_budget_usd,
            include_partial_messages=True,
            resume=resume,
            model=config.model,
        )

    return options_factory, ClaudeSDKClient, SdkTypes(allow=PermissionResultAllow, deny=PermissionResultDeny)


def check(config_dir: Path) -> int:
    problems: list[str] = []
    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        print(f"FAIL config: {exc}")
        return 1
    print(f"ok  config: runner={config.runner_name} server={config.server_url} workspace_root={config.workspace_root}")
    print(f"ok  non-root: uid={os.geteuid()}")
    print("ok  claude token: " + ("present" if config.claude_oauth_token else "MISSING (claude setup-token)"))
    if not config.claude_oauth_token:
        problems.append("claude token missing")
    available, detail = _sdk_available()
    print(f"{'ok ' if available else 'FAIL'} claude-agent-sdk: {detail}")
    if not available:
        problems.append("sdk missing")
    card = probe_agent_card(runner_name=config.runner_name, version=__version__)
    print(f"ok  agent card: toolchains={card['capabilities']['toolchains']} gpus={len(card['capabilities']['gpus'])}")
    leaked = [key for key in os.environ if key.startswith("DISPATCH_AGENT_")]
    if leaked:
        problems.append(f"secrets in environment: {leaked}")
        print(f"FAIL environment carries {leaked} — remove them from the unit/shell (INV-AGENT-1)")
    return 1 if problems else 0


async def _run(config_dir: Path) -> int:
    config = load_config(config_dir)
    scrub_environment()
    options_factory, client_factory, types = make_sdk_bindings(config)
    import websockets

    async def connect(url: str, headers: dict[str, str]) -> Any:
        return await websockets.connect(url, additional_headers=headers, max_size=2 * 1024 * 1024)

    client = RunnerClient(
        config,
        agent_card=probe_agent_card(runner_name=config.runner_name, version=__version__),
        connect=connect,
        session_host_factory=build_session_host_factory(config, options_factory=options_factory, client_factory=client_factory, types=types),
    )
    try:
        await client.run_forever()
    finally:
        await client.stop()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="dispatch-agent", description=__doc__)
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    parser.add_argument("--check", action="store_true", help="validate configuration without any network I/O")
    parser.add_argument("command", nargs="?", choices=["run"], default=None)
    args = parser.parse_args(argv)
    config_dir = Path(os.path.expanduser(args.config_dir))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.check or args.command is None:
        return check(config_dir)
    try:
        return asyncio.run(_run(config_dir))
    except ConfigError as exc:
        print(f"FAIL config: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
