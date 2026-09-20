"""Deployment-only investigation-target configuration and fail-closed lookup boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from devsupport_backend.config import Settings


class TargetCapability(StrEnum):
    """The finite runtime-evidence capabilities selectable for one target."""

    LOGS = "logs"
    METRICS = "metrics"
    TRACES = "traces"
    DEPLOYMENT_FACTS = "deployment_facts"


class AdapterType(StrEnum):
    """Registered adapter kinds, never arbitrary provider endpoints or code."""

    FAULT_LAB = "fault_lab"
    OPENSEARCH = "opensearch"
    PROMETHEUS = "prometheus"
    UNAVAILABLE = "unavailable"


_CAPABILITY_ADAPTERS: dict[TargetCapability, frozenset[AdapterType]] = {
    TargetCapability.LOGS: frozenset({AdapterType.FAULT_LAB, AdapterType.OPENSEARCH}),
    TargetCapability.METRICS: frozenset({AdapterType.FAULT_LAB, AdapterType.PROMETHEUS}),
    TargetCapability.TRACES: frozenset({AdapterType.FAULT_LAB}),
    TargetCapability.DEPLOYMENT_FACTS: frozenset({AdapterType.FAULT_LAB}),
}


class TargetConfigError(ValueError):
    """A deployment target, service, or capability cannot be safely selected."""


class CapabilityConfig(BaseModel):
    """One enabled adapter selection; provider addresses and credentials stay outside this model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    adapter_type: AdapterType = AdapterType.UNAVAILABLE
    provider_config_ref: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_enabled_adapter(self) -> "CapabilityConfig":
        if self.enabled and self.adapter_type is AdapterType.UNAVAILABLE:
            raise ValueError("an enabled capability requires a registered adapter_type")
        if self.enabled and self.provider_config_ref is None:
            raise ValueError("an enabled capability requires provider_config_ref")
        if not self.enabled and self.adapter_type is not AdapterType.UNAVAILABLE:
            raise ValueError("a disabled capability must use adapter_type=unavailable")
        if self.provider_config_ref and "://" in self.provider_config_ref:
            raise ValueError("provider_config_ref must be an opaque deployment reference")
        return self


class TargetServiceConfig(BaseModel):
    """A service name the deployment has explicitly approved for one target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("service name must not be blank")
        return normalized

    @field_validator("display_name", "description")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ProviderConfig(BaseModel):
    """A non-secret reference from target config to backend provider settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_config_ref: str = Field(min_length=1, max_length=100)
    adapter_type: AdapterType
    backend_config_key: str = Field(min_length=1, max_length=100)

    @field_validator("provider_config_ref", "backend_config_key")
    @classmethod
    def require_opaque_reference(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "://" in normalized:
            raise ValueError(
                "provider configuration references must be opaque deployment references"
            )
        return normalized


class InvestigationTargetConfig(BaseModel):
    """Non-secret deployment configuration matching exactly one persisted target identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    slug: str = Field(min_length=1, max_length=100)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    environment: str = Field(min_length=1, max_length=50)
    services: tuple[TargetServiceConfig, ...] = Field(min_length=1)
    logs: CapabilityConfig
    metrics: CapabilityConfig
    traces: CapabilityConfig = CapabilityConfig()
    deployment_facts: CapabilityConfig = CapabilityConfig()

    @field_validator("slug", "environment")
    @classmethod
    def normalize_identity_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("target identity fields must not be blank")
        return normalized

    @field_validator("display_name", "description")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_capability_adapter_types(self) -> "InvestigationTargetConfig":
        for capability in TargetCapability:
            capability_config = getattr(self, capability.value)
            if (
                capability_config.enabled
                and capability_config.adapter_type not in _CAPABILITY_ADAPTERS[capability]
            ):
                raise ValueError(
                    f"{capability.value} does not support {capability_config.adapter_type.value}"
                )
        names = [service.name for service in self.services]
        if len(names) != len(set(names)):
            raise ValueError("a target service whitelist cannot contain duplicate names")
        return self

    def capability(self, capability: TargetCapability) -> CapabilityConfig:
        return getattr(self, capability.value)


class TargetConfigRegistry:
    """Read-only deployment registry for target, service, and capability authorization."""

    def __init__(
        self, targets: tuple[InvestigationTargetConfig, ...] | list[InvestigationTargetConfig]
    ):
        self._by_id: dict[UUID, InvestigationTargetConfig] = {}
        self._by_slug: dict[str, InvestigationTargetConfig] = {}
        for target in targets:
            if target.target_id in self._by_id or target.slug in self._by_slug:
                raise TargetConfigError("target ids and slugs must be unique")
            self._by_id[target.target_id] = target
            self._by_slug[target.slug] = target

    @classmethod
    def from_settings(cls, settings: Settings) -> "TargetConfigRegistry":
        """Load the registry exclusively from backend deployment settings."""
        return cls(settings.investigation_target_configs)

    def get(
        self, *, target_id: UUID | None = None, slug: str | None = None
    ) -> InvestigationTargetConfig:
        if target_id is None and slug is None:
            raise TargetConfigError("target_id or slug is required")
        by_id = self._by_id.get(target_id) if target_id is not None else None
        by_slug = self._by_slug.get(slug) if slug is not None else None
        if target_id is not None and by_id is None:
            raise TargetConfigError("unknown InvestigationTarget")
        if slug is not None and by_slug is None:
            raise TargetConfigError("unknown InvestigationTarget")
        if by_id is not None and by_slug is not None and by_id is not by_slug:
            raise TargetConfigError("target_id and slug identify different InvestigationTargets")
        return by_id or by_slug  # type: ignore[return-value]

    def require_service(
        self, service_name: str, *, target_id: UUID | None = None, slug: str | None = None
    ) -> TargetServiceConfig:
        target = self.get(target_id=target_id, slug=slug)
        for service in target.services:
            if service.name == service_name:
                return service
        raise TargetConfigError("service is not whitelisted for InvestigationTarget")

    def require_capability(
        self,
        capability: TargetCapability,
        *,
        target_id: UUID | None = None,
        slug: str | None = None,
    ) -> CapabilityConfig:
        target = self.get(target_id=target_id, slug=slug)
        capability_config = target.capability(capability)
        if not capability_config.enabled:
            raise TargetConfigError(f"{capability.value} capability is unavailable")
        return capability_config


class ProviderConfigRegistry:
    """Allow only configured provider references; credentials remain outside target data."""

    def __init__(self, providers: tuple[ProviderConfig, ...] | list[ProviderConfig]):
        self._providers: dict[str, ProviderConfig] = {}
        for provider in providers:
            if provider.provider_config_ref in self._providers:
                raise TargetConfigError("provider_config_ref values must be unique")
            self._providers[provider.provider_config_ref] = provider

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProviderConfigRegistry":
        return cls(settings.provider_configs)

    def require(self, provider_config_ref: str, adapter_type: AdapterType) -> ProviderConfig:
        provider = self._providers.get(provider_config_ref)
        if provider is None:
            raise TargetConfigError("unknown provider_config_ref")
        if provider.adapter_type is not adapter_type:
            raise TargetConfigError(
                "provider_config_ref adapter type does not match target capability"
            )
        return provider
