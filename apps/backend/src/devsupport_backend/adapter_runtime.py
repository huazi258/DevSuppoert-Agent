"""Target-aware, allowlisted construction of formal V2 runtime evidence adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.config import ProviderBackendConfig, Settings, settings
from devsupport_backend.rag.retrieval import RAGService
from devsupport_backend.target_config import (
    AdapterType,
    CapabilityConfig,
    InvestigationTargetConfig,
    ProviderConfigRegistry,
    TargetCapability,
    TargetConfigError,
)
from devsupport_backend.tools.deployments import FaultLabDeploymentAdapter
from devsupport_backend.tools.logs import FaultLabLogsAdapter
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter
from devsupport_backend.tools.opensearch_logs import OpenSearchLogsAdapter
from devsupport_backend.tools.prometheus_metrics import PrometheusMetricsAdapter
from devsupport_backend.tools.registry import V2_READ_ONLY_TOOL_NAMES, ToolName
from devsupport_backend.tools.traces import FaultLabTracesAdapter

if TYPE_CHECKING:
    from devsupport_backend.tools.adapter_contracts import (
        DeploymentAdapter,
        LogsAdapter,
        MetricsAdapter,
        TracesAdapter,
    )


class TargetAdapterResolutionError(TargetConfigError):
    """A target capability cannot be converted into a registered adapter safely."""


class ProviderBackendConfigRegistry:
    """Resolve opaque provider keys to deployment-only backend settings."""

    def __init__(self, providers: tuple[ProviderBackendConfig, ...] | list[ProviderBackendConfig]):
        self._providers: dict[str, ProviderBackendConfig] = {}
        for provider in providers:
            if provider.backend_config_key in self._providers:
                raise TargetAdapterResolutionError("backend_config_key values must be unique")
            self._providers[provider.backend_config_key] = provider

    @classmethod
    def from_settings(cls, config: Settings) -> "ProviderBackendConfigRegistry":
        return cls(config.provider_backend_configs)

    def require(self, backend_config_key: str, adapter_type: AdapterType) -> ProviderBackendConfig:
        provider = self._providers.get(backend_config_key)
        if provider is None:
            raise TargetAdapterResolutionError("unknown backend provider configuration")
        if provider.adapter_type is not adapter_type:
            raise TargetAdapterResolutionError(
                "backend provider configuration adapter type does not match provider reference"
            )
        return provider


class TargetAdapterResolver:
    """Resolve fixed adapter constructors through configured target capability references only."""

    def __init__(
        self,
        provider_configs: ProviderConfigRegistry,
        provider_backend_configs: ProviderBackendConfigRegistry,
    ) -> None:
        self._provider_configs = provider_configs
        self._provider_backend_configs = provider_backend_configs

    @classmethod
    def from_settings(cls, config: Settings = settings) -> "TargetAdapterResolver":
        return cls(
            ProviderConfigRegistry.from_settings(config),
            ProviderBackendConfigRegistry.from_settings(config),
        )

    def build_tool_execution_dependencies(
        self, target: InvestigationTargetConfig, rag_service: RAGService
    ) -> ToolExecutionDependencies:
        """Build exactly the read-only tools enabled by one target's capability matrix."""
        logs = self._adapter_for(TargetCapability.LOGS, target.logs)
        metrics = self._adapter_for(TargetCapability.METRICS, target.metrics)
        traces = self._adapter_for(TargetCapability.TRACES, target.traces)
        deployment_facts = self._adapter_for(
            TargetCapability.DEPLOYMENT_FACTS, target.deployment_facts
        )
        available_tools = {ToolName.SEARCH_KNOWLEDGE}
        if logs is not None:
            available_tools.add(ToolName.QUERY_LOGS)
        if metrics is not None:
            available_tools.add(ToolName.QUERY_METRICS)
        if traces is not None:
            available_tools.add(ToolName.QUERY_TRACES)
        if deployment_facts is not None:
            available_tools.add(ToolName.GET_DEPLOYMENT_HISTORY)
        return ToolExecutionDependencies(
            rag_service=rag_service,
            logs_adapter=logs,
            metrics_adapter=metrics,
            traces_adapter=traces,
            deployment_adapter=deployment_facts,
            available_tools=frozenset(available_tools & V2_READ_ONLY_TOOL_NAMES),
        )

    def _adapter_for(
        self, capability: TargetCapability, capability_config: CapabilityConfig
    ) -> LogsAdapter | MetricsAdapter | TracesAdapter | DeploymentAdapter | None:
        if not capability_config.enabled:
            return None
        provider_config_ref = capability_config.provider_config_ref
        if provider_config_ref is None:
            raise TargetAdapterResolutionError("enabled capability has no provider_config_ref")
        provider_config = self._provider_configs.require(
            provider_config_ref, capability_config.adapter_type
        )
        backend_config = self._provider_backend_configs.require(
            provider_config.backend_config_key, provider_config.adapter_type
        )
        return self._build_registered_adapter(capability, backend_config)

    def _build_registered_adapter(
        self, capability: TargetCapability, backend_config: ProviderBackendConfig
    ) -> LogsAdapter | MetricsAdapter | TracesAdapter | DeploymentAdapter:
        adapter_type = backend_config.adapter_type
        if capability is TargetCapability.LOGS and adapter_type is AdapterType.FAULT_LAB:
            return FaultLabLogsAdapter(**self._fault_lab_endpoints(backend_config))
        if capability is TargetCapability.LOGS and adapter_type is AdapterType.OPENSEARCH:
            return OpenSearchLogsAdapter(opensearch_url=self._endpoint(backend_config))
        if capability is TargetCapability.METRICS and adapter_type is AdapterType.FAULT_LAB:
            return FaultLabMetricsAdapter(**self._fault_lab_endpoints(backend_config))
        if capability is TargetCapability.METRICS and adapter_type is AdapterType.PROMETHEUS:
            return PrometheusMetricsAdapter(prometheus_url=self._endpoint(backend_config))
        if capability is TargetCapability.TRACES and adapter_type is AdapterType.FAULT_LAB:
            return FaultLabTracesAdapter(**self._fault_lab_endpoints(backend_config))
        if (
            capability is TargetCapability.DEPLOYMENT_FACTS
            and adapter_type is AdapterType.FAULT_LAB
        ):
            return FaultLabDeploymentAdapter(**self._fault_lab_endpoints(backend_config))
        raise TargetAdapterResolutionError(
            f"no registered adapter for {capability.value}:{adapter_type.value}"
        )

    @staticmethod
    def _endpoint(backend_config: ProviderBackendConfig) -> str:
        if backend_config.endpoint is None:
            raise TargetAdapterResolutionError("provider backend configuration has no endpoint")
        return backend_config.endpoint

    @staticmethod
    def _fault_lab_endpoints(backend_config: ProviderBackendConfig) -> dict[str, str]:
        if (
            backend_config.order_service_url is None
            or backend_config.payment_service_url is None
        ):
            raise TargetAdapterResolutionError(
                "fault_lab backend configuration has incomplete service endpoints"
            )
        return {
            "order_service_url": backend_config.order_service_url,
            "payment_service_url": backend_config.payment_service_url,
        }
