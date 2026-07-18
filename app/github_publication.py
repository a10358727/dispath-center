"""D6 bounded first slice: GitHub publication interface only (no adapter).

See docs/DECISIONS.md and docs/AI_ENGINEERING_DECISION_GATE.md §D6. The
approved scope for this round is "permit only a provider interface and
fake-provider tests"; operational activation is a separate, explicitly named
decision that still requires an approved GitHub App registration, repository
allowlist, installation scope, egress policy, and a short-lived
installation-token broker.

Consequently this module defines only an abstract interface and immutable,
credential-free request/result shapes. It registers no concrete provider, is
not imported by any approval, API, or execution code path, and contacts no
network. The only implementations anywhere in this repository are fake
in-process test doubles constructed by tests (see
``tests/test_github_publication.py``); nothing here can create, merge, or
push anything.

The interface exposes exactly one operation because the approved first
publication action is always a draft pull request: implementations must never
merge, deploy, push to a protected branch, or hand any installation/access
token to the coding agent — there is no field through which a credential
could flow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class GitHubPublicationCapabilityUnavailableError(RuntimeError):
    """Raised when a provider does not support the requested operation."""


@dataclass(frozen=True)
class DraftPullRequestRequest:
    """Inputs for the only publication operation this interface exposes.

    ``bundle_ref`` is an opaque pointer to an already-collected, already
    reviewed result (e.g. a Git ref or bundle digest) — never raw diff bytes.
    A future operational slice would additionally check ``repository`` against
    an approved allowlist; this bounded interface has no allowlist to enforce.
    """

    repository: str
    base_branch: str
    head_branch: str
    title: str
    body: str
    bundle_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "repository",
            "base_branch",
            "head_branch",
            "title",
            "bundle_ref",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.body, str):
            raise ValueError("body must be a string")


@dataclass(frozen=True)
class DraftPullRequestResult:
    """Immutable, safe-to-persist result of one draft pull request creation.

    ``is_draft`` is not a caller preference: this shape can only ever
    represent a draft PR, so a provider that somehow produced a non-draft
    result fails validation here rather than being trusted.
    """

    provider_id: str
    repository: str
    pull_request_number: int
    pull_request_url: str
    head_branch: str
    is_draft: bool

    def __post_init__(self) -> None:
        if not self.is_draft:
            raise ValueError(
                "this interface only ever represents draft pull requests"
            )
        if self.pull_request_number < 1:
            raise ValueError("pull_request_number must be positive")
        for field_name in (
            "provider_id",
            "repository",
            "pull_request_url",
            "head_branch",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")


class GitHubPublicationProvider(ABC):
    """Reviewed-adapter interface; no concrete production implementation
    exists in this repository (D6 bounded first slice)."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Return this provider's reviewed, versioned identifier."""

    @abstractmethod
    def create_draft_pull_request(
        self, request: DraftPullRequestRequest
    ) -> DraftPullRequestResult:
        """Create exactly one draft pull request, or fail closed.

        Implementations must never merge, deploy, push directly to a
        protected branch, or expose an installation/access token to a
        caller — this interface has no field through which one could flow.
        """
