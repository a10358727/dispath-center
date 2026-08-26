"""Default-off Product API v2 route boundary."""

from __future__ import annotations

from fastapi import Request

from dispatch_center.api.errors import APIError


API_V2_PREFIX = "/api/v2"


async def api_v2_feature_gate(request: Request) -> None:
    """Hide matched v2 routes while the rollback switch is disabled."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(getattr(config, "api_v2_enabled", False)):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


async def product_rbac_v2_feature_gate(request: Request) -> None:
    """Hide the Product RBAC package behind its independent rollback flag."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(getattr(config, "product_rbac_v2_enabled", False)):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


async def project_bootstrap_v2_feature_gate(request: Request) -> None:
    """Hide Project bootstrap surfaces behind their independent rollback flag."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(
        getattr(config, "project_bootstrap_v2_enabled", False)
    ):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


async def project_environments_v1_feature_gate(request: Request) -> None:
    """Hide Host Environment surfaces behind their independent rollback flag."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(
        getattr(config, "project_environments_v1_enabled", False)
    ):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


async def run_template_v2_feature_gate(request: Request) -> None:
    """Hide typed Run Template and Defaults surfaces behind one package flag."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(
        getattr(config, "run_template_v2_enabled", False)
    ):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


async def run_experience_v2_feature_gate(request: Request) -> None:
    """Hide Product ExecutionPlan v2 surfaces behind its rollback switch."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(
        getattr(config, "run_experience_v2_enabled", False)
    ):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


async def dataset_assets_v2_feature_gate(request: Request) -> None:
    """Hide Dataset asset, alias, and lineage surfaces behind one package flag."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(
        getattr(config, "dataset_assets_v2_enabled", False)
    ):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


async def dataset_sharing_v2_feature_gate(request: Request) -> None:
    """Hide cross-Project Dataset sharing behind its rollback switch."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(
        getattr(config, "dataset_sharing_v2_enabled", False)
    ):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


async def dataset_publish_v2_feature_gate(request: Request) -> None:
    """Hide Dataset publish preview/request behind its rollback switch."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(
        getattr(config, "dataset_publish_v2_enabled", False)
    ):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


async def experiment_v2_feature_gate(request: Request) -> None:
    """Hide Experiment (one-matrix-one-approval) surfaces behind its switch."""

    config = getattr(request.app.state, "dispatch_config", None)
    if config is None or not bool(
        getattr(config, "experiment_v2_enabled", False)
    ):
        raise APIError(
            code="not_found",
            message="Resource not found",
            status_code=404,
        )


__all__ = [
    "API_V2_PREFIX",
    "api_v2_feature_gate",
    "dataset_assets_v2_feature_gate",
    "dataset_publish_v2_feature_gate",
    "dataset_sharing_v2_feature_gate",
    "experiment_v2_feature_gate",
    "project_bootstrap_v2_feature_gate",
    "project_environments_v1_feature_gate",
    "product_rbac_v2_feature_gate",
    "run_experience_v2_feature_gate",
    "run_template_v2_feature_gate",
]
