"""Claude account-quota adapter seam (DG-AI-USAGE-OVERVIEW-v1 U-3).

Live Claude account quota can only be obtained through an OAuth credential and
an undocumented endpoint; this packet deliberately returns ``unavailable``
instead of estimating or reading credentials. A future adapter implements
:class:`ClaudeQuotaAdapter` behind its own ruling; it stays separate from the
local transcript parser and must never become mandatory.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.ai_usage.common import account_quota_unavailable

REQUIRES_CREDENTIALED_API = "requires_credentialed_api"


class ClaudeQuotaAdapter(Protocol):
    def fetch(self) -> dict[str, Any]:
        """Return an ``AccountQuota`` dict (see ``app.ai_usage.projection``)."""


class UnavailableClaudeQuotaAdapter:
    """Default adapter: no credential access, no network, quota unavailable."""

    reason = REQUIRES_CREDENTIALED_API

    def fetch(self) -> dict[str, Any]:
        return account_quota_unavailable(self.reason)


__all__ = ["REQUIRES_CREDENTIALED_API", "ClaudeQuotaAdapter", "UnavailableClaudeQuotaAdapter"]
