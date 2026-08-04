"""Persistence protocols for the Node and Execution application services.

These protocols describe use-case operations rather than SQL.  The return
values intentionally stay compatible with the existing ``app.db`` facade
while the domain/application layers are migrated incrementally.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Callable, Protocol, TypeVar


ResultT = TypeVar("ResultT")


class NodeRepository(Protocol):
    """Atomic Node claim, receipt, artifact, and stop operations."""

    def lease_legacy(self, **kwargs: Any) -> dict[str, Any]: ...

    def acknowledge(self, **kwargs: Any) -> dict[str, Any]: ...

    def acknowledge_legacy(self, attempt_id: str, node_id: str) -> bool: ...

    def terminal(self, **kwargs: Any) -> dict[str, Any]: ...

    def legacy_terminal(self, **kwargs: Any) -> dict[str, Any]: ...

    def artifacts(self, **kwargs: Any) -> int: ...

    def request_stop(self, attempt_id: str) -> bool: ...

    def acknowledge_stop(self, attempt_id: str, node_id: str) -> bool: ...


class ExecutionRepository(Protocol):
    """CAS operations for the canonical execution-attempt state machine."""

    def create(self, **kwargs: Any) -> dict[str, Any]: ...

    def get(self, attempt_id: str) -> dict[str, Any] | None: ...

    def transition(self, **kwargs: Any) -> dict[str, Any]: ...

    def refresh_observation(self, **kwargs: Any) -> dict[str, Any]: ...


class AuditRepository(Protocol):
    """Durable audit append used inside the owning transaction."""

    def append(self, cursor: Any, **kwargs: Any) -> dict[str, Any]: ...


class UnitOfWork(AbstractContextManager["UnitOfWork"], Protocol):
    """A transaction boundary shared by related repository writes."""

    nodes: NodeRepository
    executions: ExecutionRepository
    audit: AuditRepository

    def run(self, operation: Callable[[Any], ResultT]) -> ResultT: ...
