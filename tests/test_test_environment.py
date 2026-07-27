import socket
from types import SimpleNamespace

import pytest

from tests.conftest import _install_ci_network_guard, _is_loopback_host


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("localhost.", True),
        ("198.51.100.8", False),
        ("example.invalid", False),
    ],
)
def test_loopback_host_classification(host, expected):
    assert _is_loopback_host(host) is expected


def test_ci_network_guard_rejects_external_connect_and_dns(monkeypatch):
    _install_ci_network_guard(monkeypatch)

    # Use a socket-shaped object so this boundary test also runs inside
    # restricted sandboxes that prohibit creating AF_INET sockets altogether.
    sock = SimpleNamespace(family=socket.AF_INET)
    with pytest.raises(RuntimeError, match="external network access"):
        socket.socket.connect(sock, ("198.51.100.8", 443))

    with pytest.raises(RuntimeError, match="external DNS/network access"):
        socket.getaddrinfo("example.invalid", 443)
