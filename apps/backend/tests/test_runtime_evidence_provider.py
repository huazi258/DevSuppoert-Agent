from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import devsupport_backend.adapter_runtime as adapter_runtime_module
import devsupport_backend.agent.nodes.tool_execution as execution_module
import devsupport_backend.workflow_console as console_module
from devsupport_backend.adapter_runtime import ProviderBackendConfigRegistry, TargetAdapterResolver
from devsupport_backend.agent.nodes.planner import (
    PlanningError,
    deterministic_initial_evidence_plan,
    investigation_planner_node,
)
from devsupport_backend.agent.nodes.tool_execution import (
    ToolExecutionDependencies,
    tool_execution_node,
)
from devsupport_backend.agent.state import (
    AgentStage,
    PendingToolCall,
    agent_state_to_checkpoint_payload,
    create_initial_agent_state,
)
from devsupport_backend.config import ProviderBackendConfig, Settings
from devsupport_backend.main import app
from devsupport_backend.models import Incident
from devsupport_backend.target_config import (
    AdapterType,
    CapabilityConfig,
    InvestigationTargetConfig,
    ProviderConfig,
    ProviderConfigRegistry,
    TargetConfigError,
    TargetServiceConfig,
)
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolStatus


class FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.system_prompt: str | None = None
        self.user_prompt: str | None = None

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self.response


def _state(description: str = "Checkout requests fail."):
    incident = Incident(
        id=uuid4(),
        service="checkout",
        environment="local",
        description=description,
        time_range_start=datetime(2026, 8, 30, tzinfo=UTC),
        time_range_end=datetime(2026, 8, 30, tzinfo=UTC) + timedelta(minutes=5),
    )
    return create_initial_agent_state(incident, symptoms=[description])


def test_runtime_evidence_provider_defaults_to_fault_lab_and_accepts_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNTIME_EVIDENCE_PROVIDER", raising=False)
    monkeypatch.delenv("DEVSUPPORT_RUNTIME_EVIDENCE_PROVIDER", raising=False)
    assert Settings().runtime_evidence_provider == "fault_lab"
    monkeypatch.setenv("RUNTIME_EVIDENCE_PROVIDER", "otel_demo")
    assert Settings().runtime_evidence_provider == "otel_demo"
    monkeypatch.delenv("RUNTIME_EVIDENCE_PROVIDER")
    monkeypatch.setenv("DEVSUPPORT_RUNTIME_EVIDENCE_PROVIDER", "otel_demo")
    assert Settings().runtime_evidence_provider == "otel_demo"


def _target(
    *,
    logs: AdapterType,
    metrics: AdapterType,
    traces: bool = False,
    deployment_facts: bool = False,
) -> InvestigationTargetConfig:
    provider_ref = {
        AdapterType.FAULT_LAB: "fault-lab-local",
        AdapterType.OPENSEARCH: "otel-logs",
        AdapterType.PROMETHEUS: "otel-metrics",
    }
    return InvestigationTargetConfig(
        target_id=uuid4(),
        slug=f"target-{uuid4()}",
        environment="local",
        services=[TargetServiceConfig(name="checkout")],
        logs=CapabilityConfig(
            enabled=True,
            adapter_type=logs,
            provider_config_ref=provider_ref[logs],
        ),
        metrics=CapabilityConfig(
            enabled=True, adapter_type=metrics, provider_config_ref=provider_ref[metrics]
        ),
        traces=(
            CapabilityConfig(
                enabled=True,
                adapter_type=AdapterType.FAULT_LAB,
                provider_config_ref="fault-lab-local",
            )
            if traces
            else CapabilityConfig()
        ),
        deployment_facts=(
            CapabilityConfig(
                enabled=True,
                adapter_type=AdapterType.FAULT_LAB,
                provider_config_ref="fault-lab-local",
            )
            if deployment_facts
            else CapabilityConfig()
        ),
    )


def _resolver() -> TargetAdapterResolver:
    return TargetAdapterResolver(
        ProviderConfigRegistry(
            [
                ProviderConfig(
                    provider_config_ref="fault-lab-local",
                    adapter_type=AdapterType.FAULT_LAB,
                    backend_config_key="fault-lab-local-settings",
                ),
                ProviderConfig(
                    provider_config_ref="otel-logs",
                    adapter_type=AdapterType.OPENSEARCH,
                    backend_config_key="otel-logs-settings",
                ),
                ProviderConfig(
                    provider_config_ref="otel-metrics",
                    adapter_type=AdapterType.PROMETHEUS,
                    backend_config_key="otel-metrics-settings",
                ),
            ]
        ),
        ProviderBackendConfigRegistry(
            [
                ProviderBackendConfig(
                    backend_config_key="fault-lab-local-settings",
                    adapter_type=AdapterType.FAULT_LAB,
                    order_service_url="http://fault-lab-orders.test",
                    payment_service_url="http://fault-lab-payments.test",
                ),
                ProviderBackendConfig(
                    backend_config_key="otel-logs-settings",
                    adapter_type=AdapterType.OPENSEARCH,
                    endpoint="http://otel-opensearch.test",
                ),
                ProviderBackendConfig(
                    backend_config_key="otel-metrics-settings",
                    adapter_type=AdapterType.PROMETHEUS,
                    endpoint="http://otel-prometheus.test",
                ),
            ]
        ),
    )


def test_target_adapter_resolver_uses_each_targets_logs_and_metrics_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_a = _target(logs=AdapterType.FAULT_LAB, metrics=AdapterType.FAULT_LAB)
    target_b = _target(logs=AdapterType.OPENSEARCH, metrics=AdapterType.PROMETHEUS)
    monkeypatch.setattr(
        adapter_runtime_module.FaultLabLogsAdapter, "__init__", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        adapter_runtime_module.FaultLabMetricsAdapter, "__init__", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        adapter_runtime_module.OpenSearchLogsAdapter, "__init__", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        adapter_runtime_module.PrometheusMetricsAdapter, "__init__", lambda *_args, **_kwargs: None
    )

    dependencies_a = _resolver().build_tool_execution_dependencies(target_a, object())
    dependencies_b = _resolver().build_tool_execution_dependencies(target_b, object())

    assert isinstance(dependencies_a.logs_adapter, adapter_runtime_module.FaultLabLogsAdapter)
    assert isinstance(
        dependencies_a.metrics_adapter, adapter_runtime_module.FaultLabMetricsAdapter
    )
    assert isinstance(dependencies_b.logs_adapter, adapter_runtime_module.OpenSearchLogsAdapter)
    assert isinstance(
        dependencies_b.metrics_adapter, adapter_runtime_module.PrometheusMetricsAdapter
    )
    assert dependencies_a.logs_adapter != dependencies_b.logs_adapter
    assert dependencies_a.metrics_adapter != dependencies_b.metrics_adapter


def test_same_adapter_type_uses_the_provider_backend_selected_by_each_target() -> None:
    target_a = _target(logs=AdapterType.OPENSEARCH, metrics=AdapterType.PROMETHEUS)
    target_b = _target(logs=AdapterType.OPENSEARCH, metrics=AdapterType.PROMETHEUS)
    target_a = target_a.model_copy(
        update={
            "logs": CapabilityConfig(
                enabled=True,
                adapter_type=AdapterType.OPENSEARCH,
                provider_config_ref="prod-opensearch",
            ),
            "metrics": CapabilityConfig(
                enabled=True,
                adapter_type=AdapterType.PROMETHEUS,
                provider_config_ref="prod-prometheus",
            ),
        }
    )
    target_b = target_b.model_copy(
        update={
            "logs": CapabilityConfig(
                enabled=True,
                adapter_type=AdapterType.OPENSEARCH,
                provider_config_ref="staging-opensearch",
            ),
            "metrics": CapabilityConfig(
                enabled=True,
                adapter_type=AdapterType.PROMETHEUS,
                provider_config_ref="staging-prometheus",
            ),
        }
    )
    resolver = TargetAdapterResolver(
        ProviderConfigRegistry(
            [
                ProviderConfig(
                    provider_config_ref="prod-opensearch",
                    adapter_type=AdapterType.OPENSEARCH,
                    backend_config_key="prod-opensearch-settings",
                ),
                ProviderConfig(
                    provider_config_ref="staging-opensearch",
                    adapter_type=AdapterType.OPENSEARCH,
                    backend_config_key="staging-opensearch-settings",
                ),
                ProviderConfig(
                    provider_config_ref="prod-prometheus",
                    adapter_type=AdapterType.PROMETHEUS,
                    backend_config_key="prod-prometheus-settings",
                ),
                ProviderConfig(
                    provider_config_ref="staging-prometheus",
                    adapter_type=AdapterType.PROMETHEUS,
                    backend_config_key="staging-prometheus-settings",
                ),
            ]
        ),
        ProviderBackendConfigRegistry(
            [
                ProviderBackendConfig(
                    backend_config_key="prod-opensearch-settings",
                    adapter_type=AdapterType.OPENSEARCH,
                    endpoint="https://opensearch.prod.internal",
                ),
                ProviderBackendConfig(
                    backend_config_key="staging-opensearch-settings",
                    adapter_type=AdapterType.OPENSEARCH,
                    endpoint="https://opensearch.staging.internal",
                ),
                ProviderBackendConfig(
                    backend_config_key="prod-prometheus-settings",
                    adapter_type=AdapterType.PROMETHEUS,
                    endpoint="https://prometheus.prod.internal",
                ),
                ProviderBackendConfig(
                    backend_config_key="staging-prometheus-settings",
                    adapter_type=AdapterType.PROMETHEUS,
                    endpoint="https://prometheus.staging.internal",
                ),
            ]
        ),
    )

    dependencies_a = resolver.build_tool_execution_dependencies(target_a, object())
    dependencies_b = resolver.build_tool_execution_dependencies(target_b, object())

    assert isinstance(dependencies_a.logs_adapter, adapter_runtime_module.OpenSearchLogsAdapter)
    assert isinstance(dependencies_b.logs_adapter, adapter_runtime_module.OpenSearchLogsAdapter)
    assert isinstance(
        dependencies_a.metrics_adapter, adapter_runtime_module.PrometheusMetricsAdapter
    )
    assert isinstance(
        dependencies_b.metrics_adapter, adapter_runtime_module.PrometheusMetricsAdapter
    )
    assert dependencies_a.logs_adapter._opensearch_url == "https://opensearch.prod.internal"
    assert dependencies_b.logs_adapter._opensearch_url == "https://opensearch.staging.internal"
    assert dependencies_a.metrics_adapter._prometheus_url == "https://prometheus.prod.internal"
    assert dependencies_b.metrics_adapter._prometheus_url == "https://prometheus.staging.internal"
    dependencies_a.logs_adapter._http_client.close()
    dependencies_b.logs_adapter._http_client.close()
    dependencies_a.metrics_adapter._http_client.close()
    dependencies_b.metrics_adapter._http_client.close()


def test_disabled_target_capabilities_are_not_constructed_or_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(logs=AdapterType.FAULT_LAB, metrics=AdapterType.FAULT_LAB)
    monkeypatch.setattr(
        adapter_runtime_module.FaultLabLogsAdapter, "__init__", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        adapter_runtime_module.FaultLabMetricsAdapter, "__init__", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        adapter_runtime_module.FaultLabTracesAdapter,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled adapter must not construct")
        ),
    )

    dependencies = _resolver().build_tool_execution_dependencies(target, object())

    assert ToolName.QUERY_TRACES not in dependencies.available_tools
    assert ToolName.GET_DEPLOYMENT_HISTORY not in dependencies.available_tools
    assert dependencies.traces_adapter is None
    assert dependencies.deployment_adapter is None


def test_unknown_provider_reference_and_mismatched_adapter_fail_closed() -> None:
    target = _target(logs=AdapterType.FAULT_LAB, metrics=AdapterType.FAULT_LAB)
    unknown = target.model_copy(
        update={
            "logs": CapabilityConfig(
                enabled=True,
                adapter_type=AdapterType.FAULT_LAB,
                provider_config_ref="unknown-provider",
            )
        }
    )

    with pytest.raises(TargetConfigError, match="unknown provider_config_ref"):
        _resolver().build_tool_execution_dependencies(unknown, object())
    with pytest.raises(TargetConfigError, match="does not match"):
        ProviderConfigRegistry(
            [
                ProviderConfig(
                    provider_config_ref="fault-lab-local",
                    adapter_type=AdapterType.OPENSEARCH,
                    backend_config_key="opensearch-settings",
                )
            ]
        ).require("fault-lab-local", AdapterType.FAULT_LAB)

    with pytest.raises(ValueError):
        ProviderConfig.model_validate(
            {
                "provider_config_ref": "unknown-adapter",
                "adapter_type": "not-registered",
                "backend_config_key": "unknown-adapter-settings",
            }
        )


def test_missing_or_mismatched_backend_provider_configuration_fails_closed() -> None:
    target = _target(logs=AdapterType.OPENSEARCH, metrics=AdapterType.PROMETHEUS)
    missing_backend = TargetAdapterResolver(
        ProviderConfigRegistry(
            [
                ProviderConfig(
                    provider_config_ref="otel-logs",
                    adapter_type=AdapterType.OPENSEARCH,
                    backend_config_key="missing-opensearch-settings",
                ),
                ProviderConfig(
                    provider_config_ref="otel-metrics",
                    adapter_type=AdapterType.PROMETHEUS,
                    backend_config_key="prometheus-settings",
                ),
            ]
        ),
        ProviderBackendConfigRegistry(
            [
                ProviderBackendConfig(
                    backend_config_key="prometheus-settings",
                    adapter_type=AdapterType.PROMETHEUS,
                    endpoint="https://prometheus.internal",
                )
            ]
        ),
    )
    with pytest.raises(TargetConfigError, match="unknown backend provider configuration"):
        missing_backend.build_tool_execution_dependencies(target, object())

    mismatched_backend = TargetAdapterResolver(
        ProviderConfigRegistry(
            [
                ProviderConfig(
                    provider_config_ref="otel-logs",
                    adapter_type=AdapterType.OPENSEARCH,
                    backend_config_key="shared-settings",
                ),
                ProviderConfig(
                    provider_config_ref="otel-metrics",
                    adapter_type=AdapterType.PROMETHEUS,
                    backend_config_key="prometheus-settings",
                ),
            ]
        ),
        ProviderBackendConfigRegistry(
            [
                ProviderBackendConfig(
                    backend_config_key="shared-settings",
                    adapter_type=AdapterType.PROMETHEUS,
                    endpoint="https://prometheus.other.internal",
                ),
                ProviderBackendConfig(
                    backend_config_key="prometheus-settings",
                    adapter_type=AdapterType.PROMETHEUS,
                    endpoint="https://prometheus.internal",
                ),
            ]
        ),
    )
    with pytest.raises(TargetConfigError, match="does not match provider reference"):
        mismatched_backend.build_tool_execution_dependencies(target, object())


def test_formal_v2_runtime_does_not_read_the_legacy_global_provider_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TargetSettings:
        provider_configs = [
            ProviderConfig(
                provider_config_ref="fault-lab-local",
                adapter_type=AdapterType.FAULT_LAB,
                backend_config_key="fault-lab-local-settings",
            )
        ]
        provider_backend_configs = [
            ProviderBackendConfig(
                backend_config_key="fault-lab-local-settings",
                adapter_type=AdapterType.FAULT_LAB,
                order_service_url="http://fault-lab-orders.test",
                payment_service_url="http://fault-lab-payments.test",
            )
        ]

        @property
        def runtime_evidence_provider(self) -> str:
            raise AssertionError("formal V2 runtime must not read the global provider switch")

    target = _target(logs=AdapterType.FAULT_LAB, metrics=AdapterType.FAULT_LAB)
    monkeypatch.setattr(console_module, "settings", _TargetSettings())
    monkeypatch.setattr(
        adapter_runtime_module.FaultLabLogsAdapter, "__init__", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        adapter_runtime_module.FaultLabMetricsAdapter, "__init__", lambda *_args, **_kwargs: None
    )

    dependencies = console_module.PostgresWorkflowRuntime._tool_execution_dependencies(
        object(), target
    )

    assert dependencies.available_tools == frozenset(
        {ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS, ToolName.QUERY_METRICS}
    )


def test_provider_references_do_not_enter_agent_state_or_tool_arguments() -> None:
    provider_secret = "deployment-only-provider-secret"
    backend_config = ProviderBackendConfig(
        backend_config_key="opensearch-secret-settings",
        adapter_type=AdapterType.OPENSEARCH,
        endpoint="https://opensearch.internal",
        credential=provider_secret,
    )
    state = _state()
    state["pending_tool_call"] = PendingToolCall(
        investigation_goal="inspect logs",
        tool_name=ToolName.QUERY_LOGS,
        tool_arguments={
            "service": "checkout",
            "environment": "local",
            "time_range_start": "2026-08-30T00:00:00+00:00",
            "time_range_end": "2026-08-30T00:05:00+00:00",
        },
        reason="runtime evidence",
    )

    payload = agent_state_to_checkpoint_payload(state)

    assert "fault-lab-local" not in str(payload)
    assert provider_secret not in str(payload)
    assert provider_secret not in str(state)
    assert provider_secret not in str(state["pending_tool_call"].tool_arguments)
    assert provider_secret not in str(backend_config)
    assert provider_secret not in json.dumps(app.openapi())
    assert "ProviderBackendConfig" not in app.openapi()["components"]["schemas"]
    assert "provider_config_ref" not in state["pending_tool_call"].tool_arguments


def test_otel_capabilities_constrain_planner_contract_and_reject_unavailable_plan() -> None:
    available = frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS, ToolName.QUERY_METRICS})
    state = _state()
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    client = FakeLLM(
        '{"investigation_goal":"inspect trace","tool_name":"query_traces",'
        '"tool_arguments":{"service":"checkout","environment":"local",'
        '"time_range_start":"2026-08-30T00:00:00+00:00",'
        '"time_range_end":"2026-08-30T00:05:00+00:00"},"reason":"trace"}'
    )

    with pytest.raises(PlanningError, match="unavailable tool"):
        investigation_planner_node(state, client, available)

    assert client.user_prompt is not None
    assert client.system_prompt is not None
    assert "query_traces" not in client.user_prompt
    assert "get_deployment_history" not in client.user_prompt
    assert "query_traces" not in client.system_prompt
    assert "get_deployment_history" not in client.system_prompt


def test_deterministic_initial_plan_degrades_to_logs_then_metrics_without_traces_or_deployment(
) -> None:
    available = frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS, ToolName.QUERY_METRICS})
    state = _state("Checkout latency is high.")
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["tool_history"].append(
        SimpleNamespace(tool_name=ToolName.SEARCH_KNOWLEDGE)  # type: ignore[arg-type]
    )

    first = deterministic_initial_evidence_plan(state, available)
    assert first is not None
    assert first.tool_name is ToolName.QUERY_METRICS

    state["tool_history"].append(SimpleNamespace(tool_name=ToolName.QUERY_METRICS))  # type: ignore[arg-type]
    second = deterministic_initial_evidence_plan(state, available)
    assert second is not None
    assert second.tool_name is ToolName.QUERY_LOGS


def test_deterministic_initial_plan_degrades_to_logs_then_metrics_without_deployment() -> None:
    available = frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS, ToolName.QUERY_METRICS})
    state = _state("Checkout requests fail.")
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["tool_history"].append(
        SimpleNamespace(tool_name=ToolName.SEARCH_KNOWLEDGE)  # type: ignore[arg-type]
    )

    first = deterministic_initial_evidence_plan(state, available)
    assert first is not None
    assert first.tool_name is ToolName.QUERY_LOGS

    state["tool_history"].append(SimpleNamespace(tool_name=ToolName.QUERY_LOGS))  # type: ignore[arg-type]
    second = deterministic_initial_evidence_plan(state, available)
    assert second is not None
    assert second.tool_name is ToolName.QUERY_METRICS


def test_initial_metrics_probe_without_logs_has_no_nonexistent_complement() -> None:
    available = frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_METRICS})
    state = _state("Checkout latency is high.")
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["tool_history"].extend(
        [
            SimpleNamespace(tool_name=ToolName.SEARCH_KNOWLEDGE),  # type: ignore[arg-type]
            SimpleNamespace(tool_name=ToolName.QUERY_METRICS),  # type: ignore[arg-type]
        ]
    )

    assert deterministic_initial_evidence_plan(state, available) is None


def test_initial_logs_probe_without_deployment_or_metrics_has_no_nonexistent_complement() -> None:
    available = frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS})
    state = _state("Checkout requests fail.")
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["tool_history"].extend(
        [
            SimpleNamespace(tool_name=ToolName.SEARCH_KNOWLEDGE),  # type: ignore[arg-type]
            SimpleNamespace(tool_name=ToolName.QUERY_LOGS),  # type: ignore[arg-type]
        ]
    )

    assert deterministic_initial_evidence_plan(state, available) is None


def test_disabled_persisted_tool_is_recorded_unavailable_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state["current_stage"] = AgentStage.TOOL_EXECUTION
    state["pending_tool_call"] = PendingToolCall(
        investigation_goal="inspect traces",
        tool_name=ToolName.QUERY_TRACES,
        tool_arguments={
            "service": "checkout",
            "environment": "local",
            "time_range_start": "2026-08-30T00:00:00+00:00",
            "time_range_end": "2026-08-30T00:05:00+00:00",
        },
        reason="persisted old plan",
    )
    dependencies = ToolExecutionDependencies(  # type: ignore[arg-type]
        rag_service=object(),
        logs_adapter=object(),
        metrics_adapter=object(),
        traces_adapter=None,
        deployment_adapter=None,
        available_tools=frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS}),
    )
    monkeypatch.setattr(
        execution_module,
        "query_traces",
        lambda *_: (_ for _ in ()).throw(AssertionError("disabled tool must not dispatch")),
    )

    updated = tool_execution_node(state, dependencies)

    assert updated["current_stage"] is AgentStage.INVESTIGATION_PLANNING
    assert updated["tool_history"][0].status is ToolStatus.UNAVAILABLE
    assert updated["tool_history"][0].error is not None
    assert updated["tool_history"][0].error.code == "capability_unavailable"
