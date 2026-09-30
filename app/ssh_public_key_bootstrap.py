"""One-shot SSH public-key bootstrap (DG-SSH-KEY-BOOTSTRAP-v1).

A platform admin may type a worker account password **once** so the platform
can append its own public key (derived from the configured private key) to
that account's ``~/.ssh/authorized_keys``. The password lives only inside the
request coroutine: it is never persisted, logged, audited, echoed, cached in
the SSH pool, or used for anything except this single connection.

Boundaries:

- the host identity must already be trusted (canonical record); the password
  connection pins that exact host key and never learns a new one;
- the remote command is a closed builder over a validated public key
  (``shlex.quote``), never user text;
- password-authenticated connections are never stored: the connection is
  closed before this module returns.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from typing import Any, Optional

import asyncssh

from app.config import ServerConfig

#: OpenSSH public key line: type, base64 body, optional comment (no newlines,
#: no shell metacharacters survive the regex).
_PUBLIC_KEY_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r" [A-Za-z0-9+/]+=*( [A-Za-z0-9@._:+-]{1,128})?$"
)

INSTALL_OUTCOMES = frozenset(
    {"installed", "auth_failed", "host_identity_mismatch", "unreachable", "remote_failed"}
)


@dataclass(frozen=True)
class InstallOutcome:
    ok: bool
    outcome: str
    detail: str


def validate_public_key(public_key: str) -> str:
    """Return the normalized single-line key or raise ``ValueError``."""

    line = (public_key or "").strip()
    if "\n" in line or "\r" in line or not _PUBLIC_KEY_RE.match(line):
        raise ValueError("public key is not a single OpenSSH public-key line")
    return line


def derive_public_key(key_path: str) -> str:
    """Read the configured private key and export its public half (no shell)."""

    key = asyncssh.read_private_key(key_path)
    exported: Any = key.export_public_key("openssh")
    text = exported.decode("ascii") if isinstance(exported, bytes) else str(exported)
    return validate_public_key(text)


def build_install_authorized_key_command(public_key: str) -> str:
    """Closed remote command: idempotently append ``public_key`` to
    ``~/.ssh/authorized_keys`` with safe modes. Only bash builtins and
    coreutils/grep are used; the key is validated then quoted."""

    quoted = shlex.quote(validate_public_key(public_key))
    return (
        "umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys"
        f" && (grep -qxF -- {quoted} ~/.ssh/authorized_keys || printf '%s\\n' {quoted} >> ~/.ssh/authorized_keys)"
        " && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && echo PUBLIC_KEY_INSTALLED"
    )


async def install_public_key_with_password(
    server: ServerConfig,
    *,
    trusted_public_key: str,
    public_key: str,
    password: str,
    connect_timeout: float,
    command_timeout: float,
    connect: Any = None,
) -> InstallOutcome:
    """Open one password-authenticated connection pinned to the trusted host
    key, run the closed install command, and close the connection.

    ``connect`` is injectable for tests; production uses ``asyncssh.connect``.
    The returned outcome never contains the password or remote output beyond
    a bounded, classified detail string.
    """

    command = build_install_authorized_key_command(public_key)
    pinned = asyncssh.import_public_key(trusted_public_key.strip())
    connect_fn = connect or asyncssh.connect
    port = getattr(server, "port", 22) or 22
    try:
        conn = await asyncio.wait_for(
            connect_fn(
                server.host,
                port=port,
                username=server.user,
                password=password,
                client_keys=None,
                agent_path=None,
                known_hosts=([pinned], [], []),
                preferred_auth=["keyboard-interactive", "password"],
            ),
            timeout=connect_timeout,
        )
    except asyncssh.PermissionDenied:
        return InstallOutcome(False, "auth_failed", "password rejected for the configured SSH user")
    except asyncssh.HostKeyNotVerifiable:
        return InstallOutcome(False, "host_identity_mismatch", "observed host key differs from the trusted identity")
    except (asyncssh.Error, OSError, asyncio.TimeoutError):
        return InstallOutcome(False, "unreachable", "SSH connection could not be established")
    try:
        result = await asyncio.wait_for(conn.run(command, check=False), timeout=command_timeout)
    except (asyncssh.Error, OSError, asyncio.TimeoutError):
        return InstallOutcome(False, "remote_failed", "install command did not complete")
    finally:
        conn.close()
    stdout = str(result.stdout or "")
    if result.exit_status == 0 and "PUBLIC_KEY_INSTALLED" in stdout:
        return InstallOutcome(True, "installed", "public key present in authorized_keys")
    return InstallOutcome(False, "remote_failed", f"install command exited with status {result.exit_status}")


def classify_trusted_identity(identity: Optional[dict[str, Any]], server: ServerConfig) -> Optional[str]:
    """Return an error code when ``identity`` cannot pin a password connection."""

    if identity is None:
        return "ssh_host_identity_untrusted"
    port = getattr(server, "port", 22) or 22
    if identity.get("host") != server.host or int(identity.get("port") or 0) != port:
        return "ssh_host_identity_rebind_required"
    if identity.get("mismatch_observed_at") is not None:
        return "ssh_host_identity_changed"
    return None
