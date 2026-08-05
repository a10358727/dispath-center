"""Typed settings groups and feature-flag lifecycle metadata."""

from app.settings.features import (
    FEATURE_FLAGS,
    FEATURE_FLAGS_BY_KEY,
    FeatureFlagSpec,
    validate_feature_flag_metadata,
)
from app.settings.model import (
    AuthSettings,
    DatabaseSettings,
    DatasetSettings,
    EngineeringSettings,
    ExecutionSettings,
    HttpSettings,
    LLMSettings,
    MachineSettings,
    NodeSettings,
    ObservabilitySettings,
    OIDCSettings,
    SchedulerSettings,
    Settings,
    SSHSettings,
)

__all__ = [
    "AuthSettings",
    "DatabaseSettings",
    "DatasetSettings",
    "EngineeringSettings",
    "ExecutionSettings",
    "FEATURE_FLAGS",
    "FEATURE_FLAGS_BY_KEY",
    "FeatureFlagSpec",
    "validate_feature_flag_metadata",
    "HttpSettings",
    "LLMSettings",
    "MachineSettings",
    "NodeSettings",
    "ObservabilitySettings",
    "OIDCSettings",
    "SchedulerSettings",
    "Settings",
    "SSHSettings",
]
