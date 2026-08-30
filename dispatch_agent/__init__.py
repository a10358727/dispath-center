"""dispatch-agent: the runner-hosted Development Agent service (DG-AGENT-RUNTIME-V3).

Runs on each runner/worker as a non-root user, dials **out** to Server A over
WebSocket, and hosts Claude Agent SDK sessions whose workspace lives on this
machine (INV-AGENT-1).  Tool use inside a session is decided per call by
:mod:`dispatch_agent.permissions` (INV-AGENT-2): workspace file tools and
allow-listed validation commands run directly, everything else becomes a
permission prompt answered by the session owner in the browser.

This package never imports ``app.*``: it is shipped as its own wheel and only
speaks the JSON-RPC-over-WebSocket protocol in :mod:`dispatch_agent.protocol`.
"""

__version__ = "0.1.0"
