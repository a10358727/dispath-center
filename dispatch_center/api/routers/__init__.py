"""Bounded router registry for the compatibility API surface."""

from dispatch_center.api.routers.agent import router as agent_router
from dispatch_center.api.routers.approvals import router as approvals_router
from dispatch_center.api.routers.auth import router as auth_router
from dispatch_center.api.routers.datasets import router as datasets_router
from dispatch_center.api.routers.engineering import router as engineering_router
from dispatch_center.api.routers.identities import router as identities_router
from dispatch_center.api.routers.inventory import router as inventory_router
from dispatch_center.api.routers.operations import router as operations_router
from dispatch_center.api.routers.projects import router as projects_router
from dispatch_center.api.routers.runs import router as runs_router
from dispatch_center.api.routers.servers import router as servers_router
from dispatch_center.api.routers.nodes import router as nodes_router


ROUTERS = (
    auth_router,
    servers_router,
    nodes_router,
    identities_router,
    projects_router,
    operations_router,
    engineering_router,
    inventory_router,
    datasets_router,
    runs_router,
    approvals_router,
    agent_router,
)

__all__ = [
    "ROUTERS",
    "agent_router",
    "approvals_router",
    "auth_router",
    "datasets_router",
    "engineering_router",
    "identities_router",
    "inventory_router",
    "nodes_router",
    "operations_router",
    "projects_router",
    "runs_router",
    "servers_router",
]
