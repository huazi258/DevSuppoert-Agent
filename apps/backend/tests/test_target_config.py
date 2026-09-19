"""M2.1 deployment configuration contracts for investigation targets."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from devsupport_backend.config import Settings
from devsupport_backend.target_config import (
    AdapterType,
    CapabilityConfig,
    InvestigationTargetConfig,
    TargetCapability,
    TargetConfigError,
    TargetConfigRegistry,
    TargetServiceConfig,
)


def _target_config(*, target_id=None, slug: str = "orders-local") -> InvestigationTargetConfig:
    return InvestigationTargetConfig(
        target_id=target_id or uuid4(),
        slug=slug,
        environment="local",
        services=(
            TargetServiceConfig(name="order-service"),
            TargetServiceConfig(name="payment-service"),
        ),
        logs=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.OPENSEARCH,
            provider_config_ref="orders-observability",
        ),
        metrics=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.PROMETHEUS,
            provider_config_ref="orders-observability",
        ),
        traces=CapabilityConfig(),
        deployment_facts=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.FAULT_LAB,
            provider_config_ref="orders-deployments",
        ),
    )


def test_registry_loads_a_target_by_id_or_slug_and_allows_multiple_services() -> None:
    target = _target_config()
    registry = TargetConfigRegistry([target])

    assert registry.get(target_id=target.target_id) is target
    assert registry.get(slug=target.slug) is target
    assert (
        registry.require_service("order-service", target_id=target.target_id).name
        == "order-service"
    )
    assert registry.require_service("payment-service", slug=target.slug).name == "payment-service"


def test_registry_exposes_enabled_capabilities_and_adapter_types() -> None:
    target = _target_config()
    registry = TargetConfigRegistry([target])

    assert registry.require_capability(
        TargetCapability.LOGS, target_id=target.target_id
    ).adapter_type is AdapterType.OPENSEARCH
    assert registry.require_capability(
        TargetCapability.METRICS, slug=target.slug
    ).adapter_type is AdapterType.PROMETHEUS


def test_registry_fails_closed_for_unknown_target_service_or_disabled_capability() -> None:
    target = _target_config()
    registry = TargetConfigRegistry([target])

    with pytest.raises(TargetConfigError, match="unknown InvestigationTarget"):
        registry.get(target_id=uuid4())
    with pytest.raises(TargetConfigError, match="not whitelisted"):
        registry.require_service("unconfigured-service", target_id=target.target_id)
    with pytest.raises(TargetConfigError, match="unavailable"):
        registry.require_capability(TargetCapability.TRACES, target_id=target.target_id)


def test_capability_configuration_rejects_unregistered_adapter_combinations() -> None:
    target = _target_config()
    with pytest.raises(ValueError, match="logs does not support prometheus"):
        InvestigationTargetConfig.model_validate(
            {
                **target.model_dump(mode="json"),
                "logs": {"enabled": True, "adapter_type": "prometheus"},
            }
        )


def test_deployment_target_config_rejects_provider_secrets_and_urls() -> None:
    target = _target_config()

    with pytest.raises(ValidationError):
        InvestigationTargetConfig.model_validate(
            {**target.model_dump(mode="json"), "token": "must-not-be-configured-here"}
        )
    with pytest.raises(ValidationError, match="opaque deployment reference"):
        CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.OPENSEARCH,
            provider_config_ref="https://provider.example",
        )


def test_settings_loads_target_configs_from_deployment_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target_config()
    monkeypatch.setenv(
        "DEVSUPPORT_INVESTIGATION_TARGET_CONFIGS",
        json.dumps([target.model_dump(mode="json")]),
    )

    settings = Settings()

    registry = TargetConfigRegistry.from_settings(settings)
    assert registry.get(slug=target.slug) == target
