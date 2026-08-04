"""Persistence boundaries used by application services.

The first extraction keeps :class:`app.db.Database` as a compatibility facade.
New code can depend on the protocols and the SQLite UoW without importing the
monolithic facade directly; the adapter is intentionally additive until all
callers have moved.
"""

from .protocols import AuditRepository, ExecutionRepository, NodeRepository, UnitOfWork
from .sqlite import (
    SQLiteAuditRepository,
    SQLiteExecutionRepository,
    SQLiteNodeRepository,
    SQLiteUnitOfWork,
)

__all__ = [
    "AuditRepository",
    "ExecutionRepository",
    "NodeRepository",
    "SQLiteAuditRepository",
    "SQLiteExecutionRepository",
    "SQLiteNodeRepository",
    "SQLiteUnitOfWork",
    "UnitOfWork",
]
