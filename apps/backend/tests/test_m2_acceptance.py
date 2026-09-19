"""M2.4 acceptance coverage for target-aware runtime evidence collection."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

import devsupport_backend.adapter_runtime as adapter_runtime_module
from devsupport_backend.adapter_runtime import ProviderBackendConfigRegistry, TargetAdapterResolver
from devsupport_backend.agent.nodes.tool_execution import tool_execution_node
from devsupport_backend.agent.state import AgentStage, PendingToolCall, create_initial_agent_state
from devsupport_backend.config import ProviderBackendConfig
from devsupport_backend.models import Incident
from devsupport_backend.target_config import (
    AdapterType,
    CapabilityConfig,
    InvestigationTargetConfig,
    ProviderConfig,
    ProviderConfigRegistry,
    TargetConfigRegistry,
    TargetServiceConfig,
)
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolStatus

NOW = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)


def _incident(target: InvestigationTargetConfig) -> Incident:
    return Incident(
        id=uuid4(),
        target_id=target.target_id,
        service_id=uuid4(),
        service=target.services[0].name,
        environment=target.environment,
        description="调查运行时证据。",
        time_range_start=NOW - timedelta(minutes=5),
        time_range_end=NOW,
        thread_id=str(uuid4()),
    )


def _dependencies_for_incident(
    incident: Incident,
    registry: TargetConfigRegistry,
    resolver: TargetAdapterResolver,
):
    """Follow the production selection boundary before executing any Tool."""
    target = registry.get(target_id=incident.target_id)
    registry.require_service(incident.service, target_id=target.target_id)
    return resolver.build_tool_execution_dependencies(target, object())


def _execute(
    incident: Incident,
    dependencies: object,
    tool_name: ToolName,
    tool_arguments: dict[str, object],
):
    state = create_initial_agent_state(incident)
    state["current_stage"] = AgentStage.TOOL_EXECUTION
    state["pending_tool_call"] = PendingToolCall(
        investigation_goal="收集受控运行时证据。",
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        reason="验收正式的目标感知 Tool Runtime。",
    )
    return tool_execution_node(state, dependencies)  # type: ignore[arg-type]


def _fault_lab_target() -> InvestigationTargetConfig:
    return InvestigationTargetConfig(
        target_id=uuid4(),
        slug="fault-lab-acceptance",
        environment="local",
        services=(TargetServiceConfig(name="order-service"),),
        logs=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.FAULT_LAB,
            provider_config_ref="fault-lab",
        ),
        metrics=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.FAULT_LAB,
            provider_config_ref="fault-lab",
        ),
        traces=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.FAULT_LAB,
            provider_config_ref="fault-lab",
        ),
        deployment_facts=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.FAULT_LAB,
            provider_config_ref="fault-lab",
        ),
    )


def _otel_target() -> InvestigationTargetConfig:
    return InvestigationTargetConfig(
        target_id=uuid4(),
        slug="otel-demo-acceptance",
        environment="local",
        services=(TargetServiceConfig(name="checkout"),),
        logs=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.OPENSEARCH,
            provider_config_ref="otel-logs",
        ),
        metrics=CapabilityConfig(
            enabled=True,
            adapter_type=AdapterType.PROMETHEUS,
            provider_config_ref="otel-metrics",
        ),
    )


def _resolver(
    *,
    fault_lab: bool = False,
    opensearch_url: str = "http://otel-opensearch.test",
    prometheus_url: str = "http://otel-prometheus.test",
) -> TargetAdapterResolver:
    providers: list[ProviderConfig] = [
        ProviderConfig(
            provider_config_ref="otel-logs",
            adapter_type=AdapterType.OPENSEARCH,
            backend_config_key="otel-logs-backend",
        ),
        ProviderConfig(
            provider_config_ref="otel-metrics",
            adapter_type=AdapterType.PROMETHEUS,
            backend_config_key="otel-metrics-backend",
        ),
    ]
    backends: list[ProviderBackendConfig] = [
        ProviderBackendConfig(
            backend_config_key="otel-logs-backend",
            adapter_type=AdapterType.OPENSEARCH,
            endpoint=opensearch_url,
        ),
        ProviderBackendConfig(
            backend_config_key="otel-metrics-backend",
            adapter_type=AdapterType.PROMETHEUS,
            endpoint=prometheus_url,
        ),
    ]
    if fault_lab:
        providers.append(
            ProviderConfig(
                provider_config_ref="fault-lab",
                adapter_type=AdapterType.FAULT_LAB,
                backend_config_key="fault-lab-backend",
            )
        )
        backends.append(
            ProviderBackendConfig(
                backend_config_key="fault-lab-backend",
                adapter_type=AdapterType.FAULT_LAB,
                order_service_url="http://fault-lab-order.test",
                payment_service_url="http://fault-lab-payment.test",
            )
        )
    return TargetAdapterResolver(
        ProviderConfigRegistry(providers), ProviderBackendConfigRegistry(backends)
    )


def _install_adapter_transport(
    monkeypatch: pytest.MonkeyPatch,
    adapter_name: str,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Inject a provider transport while leaving adapter construction to the resolver."""
    adapter_class = getattr(adapter_runtime_module, adapter_name)
    original_init = adapter_class.__init__

    def init_with_transport(instance: object, **kwargs: object) -> None:
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original_init(instance, **kwargs)

    monkeypatch.setattr(adapter_class, "__init__", init_with_transport)


def _fault_lab_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/internal/logs":
        return httpx.Response(
            200,
            json={
                "service": "order-service",
                "match_count": 1,
                "events": [
                    {
                        "timestamp": NOW.isoformat(),
                        "service": "order-service",
                        "level": "error",
                        "message": "required runtime configuration is missing",
                        "error_type": "MissingRequiredConfiguration",
                    }
                ],
            },
            request=request,
        )
    if path == "/internal/metrics":
        return httpx.Response(
            200,
            json={
                "service": "order-service",
                "request_count": 1,
                "success_count": 0,
                "error_count": 1,
                "last_request_duration_ms": 12.0,
                "average_request_duration_ms": 12.0,
            },
            request=request,
        )
    if path == "/health":
        return httpx.Response(
            200,
            json={"service": "order-service", "status": "degraded"},
            request=request,
        )
    if path == "/internal/traces":
        service = "payment-service" if "payment" in request.url.host else "order-service"
        return httpx.Response(
            200,
            json={
                "service": service,
                "match_count": 1,
                "spans": [
                    {
                        "trace_id": "fault-lab-trace",
                        "span_id": f"{service}-span",
                        "parent_span_id": None,
                        "service": service,
                        "operation": "POST /orders",
                        "start_time": NOW.isoformat(),
                        "end_time": (NOW + timedelta(milliseconds=12)).isoformat(),
                        "duration_ms": 12.0,
                        "status": "error",
                        "error": "MissingRequiredConfiguration",
                    }
                ],
            },
            request=request,
        )
    if path == "/internal/deployment":
        return httpx.Response(
            200,
            json={
                "service": "order-service",
                "current_version": "v1.1.0",
                "previous_version": "v1.0.0",
                "deployed_at": NOW.isoformat(),
            },
            request=request,
        )
    raise AssertionError(f"Unexpected Fault Lab request: {request.url}")


def _otel_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/_search"):
        return httpx.Response(
            200,
            json={
                "hits": {
                    "total": {"value": 1, "relation": "eq"},
                    "hits": [
                        {
                            "_source": {
                                "@timestamp": NOW.isoformat(),
                                "body": "checkout completed",
                                "severity": {"text": "INFO"},
                                "resource": {"service.name": "checkout"},
                            }
                        }
                    ],
                }
            },
            request=request,
        )
    query = request.url.params["query"]
    value = "1" if query.startswith("count by") else "10"
    if "status_code" in query:
        value = "2"
    elif "duration_milliseconds_sum" in query:
        value = "250"
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"service_name": "checkout"}, "value": [NOW.timestamp(), value]}
                ],
            },
        },
        request=request,
    )


def test_fault_lab_missing_config_acceptance_uses_the_target_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing deterministic Scenario A reaches normalized evidence through V2 Runtime."""
    for adapter_name in (
        "FaultLabLogsAdapter",
        "FaultLabMetricsAdapter",
        "FaultLabTracesAdapter",
        "FaultLabDeploymentAdapter",
    ):
        _install_adapter_transport(monkeypatch, adapter_name, _fault_lab_handler)

    target = _fault_lab_target()
    incident = _incident(target)
    dependencies = _dependencies_for_incident(
        incident, TargetConfigRegistry([target]), _resolver(fault_lab=True)
    )

    log_state = _execute(
        incident,
        dependencies,
        ToolName.QUERY_LOGS,
        {
            "service": "order-service",
            "environment": "local",
            "time_range_start": incident.time_range_start.isoformat(),
            "time_range_end": incident.time_range_end.isoformat(),
        },
    )
    metric_state = _execute(
        incident,
        dependencies,
        ToolName.QUERY_METRICS,
        {"service": "order-service", "environment": "local"},
    )
    trace_state = _execute(
        incident,
        dependencies,
        ToolName.QUERY_TRACES,
        {
            "service": "order-service",
            "environment": "local",
            "time_range_start": incident.time_range_start.isoformat(),
            "time_range_end": incident.time_range_end.isoformat(),
        },
    )
    deployment_state = _execute(
        incident,
        dependencies,
        ToolName.GET_DEPLOYMENT_HISTORY,
        {"service": "order-service", "environment": "local"},
    )

    assert dependencies.available_tools == frozenset(
        {
            ToolName.SEARCH_KNOWLEDGE,
            ToolName.QUERY_LOGS,
            ToolName.QUERY_METRICS,
            ToolName.QUERY_TRACES,
            ToolName.GET_DEPLOYMENT_HISTORY,
        }
    )
    assert log_state["tool_history"][0].status is ToolStatus.SUCCESS
    assert log_state["evidence"][0].data["error_patterns"] == [
        {"pattern": "MissingRequiredConfiguration", "count": 1}
    ]
    assert metric_state["evidence"][0].data["metrics"]["error_count"] == 1
    assert trace_state["evidence"][0].data["trace_count"] == 1
    assert deployment_state["evidence"][0].data["deployments"][0]["current_version"] == "v1.1.0"


def test_otel_demo_acceptance_uses_only_logs_and_metrics_and_normalizes_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_adapter_transport(monkeypatch, "OpenSearchLogsAdapter", _otel_handler)
    _install_adapter_transport(monkeypatch, "PrometheusMetricsAdapter", _otel_handler)
    target = _otel_target()
    incident = _incident(target)
    dependencies = _dependencies_for_incident(
        incident, TargetConfigRegistry([target]), _resolver()
    )

    log_state = _execute(
        incident,
        dependencies,
        ToolName.QUERY_LOGS,
        {
            "service": "checkout",
            "environment": "local",
            "time_range_start": incident.time_range_start.isoformat(),
            "time_range_end": incident.time_range_end.isoformat(),
        },
    )
    metric_state = _execute(
        incident,
        dependencies,
        ToolName.QUERY_METRICS,
        {"service": "checkout", "environment": "local"},
    )
    unavailable_trace_state = _execute(
        incident,
        dependencies,
        ToolName.QUERY_TRACES,
        {
            "service": "checkout",
            "environment": "local",
            "time_range_start": incident.time_range_start.isoformat(),
            "time_range_end": incident.time_range_end.isoformat(),
        },
    )
    unavailable_deployment_state = _execute(
        incident,
        dependencies,
        ToolName.GET_DEPLOYMENT_HISTORY,
        {"service": "checkout", "environment": "local"},
    )

    assert dependencies.available_tools == frozenset(
        {ToolName.SEARCH_KNOWLEDGE, ToolName.QUERY_LOGS, ToolName.QUERY_METRICS}
    )
    assert log_state["evidence"][0].data["provenance"]["source"] == "opensearch"
    assert log_state["evidence"][0].data["match_count"] == 1
    assert metric_state["evidence"][0].data["provenance"]["source"] == "prometheus"
    assert metric_state["evidence"][0].data["metrics"]["error_rate"] == 0.2
    assert unavailable_trace_state["tool_history"][0].status is ToolStatus.UNAVAILABLE
    assert unavailable_trace_state["tool_history"][0].error.code == "capability_unavailable"
    assert unavailable_trace_state["evidence"] == []
    assert unavailable_deployment_state["tool_history"][0].status is ToolStatus.UNAVAILABLE
    assert unavailable_deployment_state["tool_history"][0].error.code == "capability_unavailable"
    assert unavailable_deployment_state["evidence"] == []


def test_unavailable_otel_provider_records_failure_without_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    _install_adapter_transport(monkeypatch, "OpenSearchLogsAdapter", unavailable_handler)
    target = _otel_target()
    incident = _incident(target)
    dependencies = _dependencies_for_incident(
        incident, TargetConfigRegistry([target]), _resolver()
    )

    state = _execute(
        incident,
        dependencies,
        ToolName.QUERY_LOGS,
        {
            "service": "checkout",
            "environment": "local",
            "time_range_start": incident.time_range_start.isoformat(),
            "time_range_end": incident.time_range_end.isoformat(),
        },
    )

    assert state["tool_history"][0].status is ToolStatus.FAILURE
    assert state["tool_history"][0].error.code == "provider_unavailable"
    assert state["evidence"] == []


@pytest.mark.skipif(
    os.getenv("DEVSUPPORT_RUN_OTEL_DEMO_ACCEPTANCE") != "1",
    reason="requires a running OpenTelemetry Demo with recent checkout traffic",
)
def test_live_otel_demo_acceptance_collects_real_logs_and_metrics() -> None:
    """Optional live acceptance; endpoints are deployment inputs, never Tool arguments."""
    opensearch_url = os.environ["DEVSUPPORT_OTEL_DEMO_OPENSEARCH_URL"]
    prometheus_url = os.environ["DEVSUPPORT_OTEL_DEMO_PROMETHEUS_URL"]
    target = _otel_target()
    incident = _incident(target)
    incident.time_range_start = datetime.now(UTC) - timedelta(minutes=15)
    incident.time_range_end = datetime.now(UTC)
    dependencies = _dependencies_for_incident(
        incident,
        TargetConfigRegistry([target]),
        _resolver(opensearch_url=opensearch_url, prometheus_url=prometheus_url),
    )

    log_state = _execute(
        incident,
        dependencies,
        ToolName.QUERY_LOGS,
        {
            "service": "checkout",
            "environment": "local",
            "time_range_start": incident.time_range_start.isoformat(),
            "time_range_end": incident.time_range_end.isoformat(),
        },
    )
    metric_state = _execute(
        incident,
        dependencies,
        ToolName.QUERY_METRICS,
        {"service": "checkout", "environment": "local"},
    )

    assert log_state["tool_history"][0].status is ToolStatus.SUCCESS
    assert log_state["evidence"][0].data["match_count"] > 0
    assert metric_state["tool_history"][0].status is ToolStatus.SUCCESS
    assert metric_state["evidence"][0].data["metrics"]["request_count"] > 0
