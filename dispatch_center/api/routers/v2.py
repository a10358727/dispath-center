"""Product API v2 root router.

PR-01 intentionally adds no product endpoint. Future child routers inherit
this boundary and its default-off runtime dependency.
"""

from fastapi import APIRouter, Depends

from dispatch_center.api.v2 import API_V2_PREFIX, api_v2_feature_gate


def create_v2_router() -> APIRouter:
    return APIRouter(
        prefix=API_V2_PREFIX,
        dependencies=[Depends(api_v2_feature_gate)],
    )


router = create_v2_router()

__all__ = ["create_v2_router", "router"]
