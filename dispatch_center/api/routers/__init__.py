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

from dispatch_center.api.routers.v2 import router as v2_router
from dispatch_center.api.routers.identity_workspace_v2 import (
    router as identity_workspace_v2_router,
)
from dispatch_center.api.routers.approvals_v2 import router as approvals_v2_router
from dispatch_center.api.routers.agent_runners_v2 import router as agent_runners_v2_router
from dispatch_center.api.routers.studio_v2 import router as studio_v2_router
from dispatch_center.api.routers.project_bootstrap_v2 import (
    router as project_bootstrap_v2_router,
)
from dispatch_center.api.routers.project_environments_v1 import (
    router as project_environments_v1_router,
)
from dispatch_center.api.routers.run_templates_v2 import router as run_templates_v2_router
from dispatch_center.api.routers.dataset_assets_v2 import router as dataset_assets_v2_router
from dispatch_center.api.routers.runs_v2 import router as runs_v2_router
from dispatch_center.api.routers.project_instance_update_v2 import (
    router as project_instance_update_v2_router,
)
from dispatch_center.api.routers.experiments_v2 import router as experiments_v2_router
from dispatch_center.api.routers.project_roles_v2 import router as project_roles_v2_router
from dispatch_center.api.routers.jobs_v2 import router as jobs_v2_router
from dispatch_center.api.routers.infrastructure_v2 import router as infrastructure_v2_router
from dispatch_center.api.routers.projects_legacy_v2 import router as projects_legacy_v2_router
from dispatch_center.api.routers.engineering_v2 import router as engineering_v2_router
from dispatch_center.api.routers.ai_providers_v2 import router as ai_providers_v2_router


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

# Single source of truth for the product (v2) router mount order. This is the
# exact sequence `app/main.py` includes today (verified against its explicit
# `app.include_router(...)` calls) — changing this list changes what main.py
# mounts, so keep it in lockstep with any future router addition/removal.
PRODUCT_ROUTERS = (
    v2_router,
    identity_workspace_v2_router,
    approvals_v2_router,
    agent_runners_v2_router,
    studio_v2_router,
    project_bootstrap_v2_router,
    project_environments_v1_router,
    run_templates_v2_router,
    dataset_assets_v2_router,
    runs_v2_router,
    project_instance_update_v2_router,
    experiments_v2_router,
    project_roles_v2_router,
    jobs_v2_router,
    infrastructure_v2_router,
    projects_legacy_v2_router,
    engineering_v2_router,
    ai_providers_v2_router,
)

__all__ = [
    "PRODUCT_ROUTERS",
    "ROUTERS",
    "agent_router",
    "agent_runners_v2_router",
    "ai_providers_v2_router",
    "approvals_router",
    "approvals_v2_router",
    "auth_router",
    "dataset_assets_v2_router",
    "datasets_router",
    "engineering_router",
    "engineering_v2_router",
    "experiments_v2_router",
    "identities_router",
    "identity_workspace_v2_router",
    "infrastructure_v2_router",
    "inventory_router",
    "jobs_v2_router",
    "nodes_router",
    "operations_router",
    "project_bootstrap_v2_router",
    "project_environments_v1_router",
    "project_instance_update_v2_router",
    "project_roles_v2_router",
    "projects_legacy_v2_router",
    "projects_router",
    "run_templates_v2_router",
    "runs_router",
    "runs_v2_router",
    "servers_router",
    "studio_v2_router",
    "v2_router",
]
