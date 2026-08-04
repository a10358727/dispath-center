"""Shared, side-effect-free validators for typed application settings."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


AUTHORIZATION_MODES = frozenset({"off", "shadow", "enforce"})
PROCESS_ROLES = frozenset({"all", "api", "scheduler"})
MAX_OIDC_CLOCK_SKEW_LEEWAY_SEC = 300
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_PRIVATE_BIND_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def validate_cookie_name(value: str, setting_name: str) -> None:
    """Reject blank or response-splitting cookie names."""

    if not isinstance(value, str) or not _COOKIE_NAME_RE.fullmatch(value):
        raise ValueError(f"{setting_name} must be a non-empty HTTP cookie token")


def validate_api_bind(host: str, port: int) -> None:
    """Fail closed before Uvicorn can expose the control plane publicly."""

    if not isinstance(host, str) or not host or host != host.strip():
        raise ValueError(
            "API_HOST must resolve only to loopback/private addresses; "
            "use localhost or a numeric private address"
        )
    if host == "localhost":
        address = ipaddress.ip_address("127.0.0.1")
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(
                "API_HOST must resolve only to loopback/private addresses; "
                "use localhost or a numeric private address; "
                "unverified hostnames and wildcard/public binds are refused"
            ) from None
    if not any(address in network for network in _PRIVATE_BIND_NETWORKS):
        raise ValueError(
            "API_HOST must resolve only to loopback/private addresses; "
            "allowed ranges are loopback, RFC1918, link-local, ULA, or "
            "Tailscale CGNAT space; wildcard/public binds are refused"
        )
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError("API_PORT must be between 1 and 65535")


def validate_oidc_https_url(
    value: str,
    setting_name: str,
    *,
    callback: bool = False,
) -> None:
    """Validate security-sensitive OIDC endpoints without provider I/O."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{setting_name} must be a non-empty HTTPS URL")
    if value != value.strip():
        raise ValueError(f"{setting_name} must be an absolute HTTPS URL")
    try:
        parsed = urlsplit(value)
        # Accessing port performs urllib's numeric/range validation.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be an absolute HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{setting_name} must be an absolute HTTPS URL")
    if callback:
        if parsed.path != "/auth/callback" or parsed.query:
            raise ValueError(
                "OIDC_REDIRECT_URI must use the exact /auth/callback path "
                "without query or fragment"
            )
    elif parsed.query:
        raise ValueError("OIDC_ISSUER must not contain a query or fragment")
