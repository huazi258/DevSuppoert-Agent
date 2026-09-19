"""Target-aware, allowlisted construction of formal V2 runtime evidence adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.config import Settings, settings
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


class TargetAdapterResolver:
    """Resolve fixed adapter constructors through configured target capability references only."""

    def __init__(
        self,
        provider_configs: ProviderConfigRegistry,
        config: Settings = settings,
    ) -> None:
        self._provider_configs = provider_configs
        self._config = config

    @classmethod
    def from_settings(cls, config: Settings = settings) -> "TargetAdapterResolver":
        return cls(ProviderConfigRegistry.from_settings(config), config)

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
        self._provider_configs.require(provider_config_ref, capability_config.adapter_type)
        return self._build_registered_adapter(capability, capability_config.adapter_type)

    def _build_registered_adapter(
        self, capability: TargetCapability, adapter_type: AdapterType
    ) -> LogsAdapter | MetricsAdapter | TracesAdapter | DeploymentAdapter:
        if capability is TargetCapability.LOGS and adapter_type is AdapterType.FAULT_LAB:
            return FaultLabLogsAdapter.from_settings(self._config)
        if capability is TargetCapability.LOGS and adapter_type is AdapterType.OPENSEARCH:
            return OpenSearchLogsAdapter.from_settings(self._config)
        if capability is TargetCapability.METRICS and adapter_type is AdapterType.FAULT_LAB:
            return FaultLabMetricsAdapter.from_settings(self._config)
        if capability is TargetCapability.METRICS and adapter_type is AdapterType.PROMETHEUS:
            return PrometheusMetricsAdapter.from_settings(self._config)
        if capability is TargetCapability.TRACES and adapter_type is AdapterType.FAULT_LAB:
            return FaultLabTracesAdapter.from_settings(self._config)
        if (
            capability is TargetCapability.DEPLOYMENT_FACTS
            and adapter_type is AdapterType.FAULT_LAB
        ):
            return FaultLabDeploymentAdapter.from_settings(self._config)
        raise TargetAdapterResolutionError(
            f"no registered adapter for {capability.value}:{adapter_type.value}"
        )
