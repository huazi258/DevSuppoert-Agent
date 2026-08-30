from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import get_type_hints

import httpx
import pytest

from devsupport_backend.agent.nodes import tool_execution
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.tools import adapter_contracts
from devsupport_backend.tools.adapter_contracts import (
    AdapterError,
    DeploymentAdapter,
    DeploymentQueryResult,
    LogEvent,
    LogQueryResult,
    LogsAdapter,
    MetricsAdapter,
    MetricsQueryResult,
    TraceQueryResult,
    TracesAdapter,
    TraceSpanRecord,
)
from devsupport_backend.tools.deployments import (
    DeploymentAdapterError,
    FaultLabDeploymentAdapter,
)
from devsupport_backend.tools.get_deployment_history import get_deployment_history
from devsupport_backend.tools.logs import FaultLabLogsAdapter, LogsAdapterError
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter, MetricsAdapterError
from devsupport_backend.tools.query_logs import query_logs
from devsupport_backend.tools.query_metrics import query_metrics
from devsupport_backend.tools.query_traces import query_traces
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    QueryLogsInput,
    QueryMetricsInput,
    QueryTracesInput,
)
from devsupport_backend.tools.traces import FaultLabTracesAdapter, TracesAdapterError


def test_normalized_records_cover_current_tool_facts_for_future_service_names() -> None:
    now = datetime.now(UTC)
    log_result = LogQueryResult(
        match_count=1,
        events=(
            LogEvent(
                timestamp=now,
                service="checkout",
                level="error",
                message="downstream request failed",
                request_id="request-1",
                trace_id="trace-1",
                error_type="Unavailable",
                status_code=503,
                duration_ms=125.0,
                downstream_service="payment",
            ),
        ),
    )
    metrics_result = MetricsQueryResult(
        service="checkout",
        health_status="ok",
        request_count=4,
        success_count=3,
        error_count=1,
        last_request_duration_ms=125.0,
        average_request_duration_ms=75.0,
    )
    trace_result = TraceQueryResult(
        spans=(
            TraceSpanRecord(
                trace_id="trace-1",
                span_id="payment-span",
                parent_span_id="checkout-span",
                service="payment",
                operation="Charge",
                start_time=now,
                end_time=now + timedelta(milliseconds=125),
                duration_ms=125.0,
                status="error",
                error="Unavailable",
            ),
        )
    )
    deployment_result = DeploymentQueryResult(
        service="checkout",
        current_version="v1.2.0",
        previous_version="v1.1.0",
        deployed_at=now,
    )

    assert log_result.events[0].downstream_service == "payment"
    assert metrics_result.error_rate == 0.25
    assert trace_result.spans[0].parent_span_id == "checkout-span"
    assert deployment_result.current_version == "v1.2.0"


def test_adapter_error_keeps_safe_code_message_and_retryability() -> None:
    error = AdapterError("provider_unavailable", "provider request failed", retryable=True)

    assert error.code == "provider_unavailable"
    assert error.message == "provider request failed"
    assert str(error) == "provider request failed"
    assert error.retryable


def test_protocols_use_frozen_tool_inputs_and_normalized_results() -> None:
    assert get_type_hints(LogsAdapter.query) == {
        "tool_input": QueryLogsInput,
        "return": LogQueryResult,
    }
    assert get_type_hints(MetricsAdapter.query) == {
        "tool_input": QueryMetricsInput,
        "return": MetricsQueryResult,
    }
    assert get_type_hints(TracesAdapter.query) == {
        "tool_input": QueryTracesInput,
        "return": TraceQueryResult,
    }
    assert get_type_hints(DeploymentAdapter.query) == {
        "tool_input": GetDeploymentHistoryInput,
        "return": DeploymentQueryResult,
    }


def test_contract_module_has_no_provider_implementation_dependency() -> None:
    source = inspect.getsource(adapter_contracts)

    assert "FaultLab" not in source
    assert all(
        module_name not in source
        for module_name in (
            "devsupport_backend.tools.logs",
            "devsupport_backend.tools.metrics",
            "devsupport_backend.tools.traces",
            "devsupport_backend.tools.deployments",
        )
    )


def test_fault_lab_adapters_map_validated_payloads_to_normalized_contracts() -> None:
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/logs":
            return httpx.Response(
                200,
                json={
                    "service": "order-service",
                    "match_count": 1,
                    "events": [
                        {
                            "timestamp": now.isoformat(),
                            "service": "order-service",
                            "level": "error",
                            "message": "downstream timeout",
                            "method": "POST",
                            "path": "/orders",
                            "request_id": "request-1",
                            "trace_id": "trace-1",
                            "error_type": "ReadTimeout",
                        }
                    ],
                },
                request=request,
            )
        if request.url.path == "/internal/metrics":
            return httpx.Response(
                200,
                json={
                    "service": "order-service",
                    "request_count": 2,
                    "success_count": 1,
                    "error_count": 1,
                    "last_request_duration_ms": 100.0,
                    "average_request_duration_ms": 75.0,
                },
                request=request,
            )
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"service": "order-service", "status": "degraded"},
                request=request,
            )
        if request.url.path == "/internal/traces":
            service = (
                "order-service"
                if request.url.host == "order-service.test"
                else "payment-service"
            )
            return httpx.Response(
                200,
                json={
                    "service": service,
                    "match_count": 1,
                    "spans": [
                        {
                            "trace_id": "trace-1",
                            "span_id": f"{service}-span",
                            "parent_span_id": None,
                            "service": service,
                            "operation": "request",
                            "start_time": now.isoformat(),
                            "end_time": (now + timedelta(milliseconds=100)).isoformat(),
                            "duration_ms": 100.0,
                            "status": "ok",
                            "error": None,
                        }
                    ],
                },
                request=request,
            )
        if request.url.path == "/internal/deployment":
            return httpx.Response(
                200,
                json={
                    "service": "order-service",
                    "current_version": "v1.1.0",
                    "previous_version": "v1.0.0",
                    "deployed_at": now.isoformat(),
                },
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter_args = {
        "order_service_url": "http://order-service.test",
        "payment_service_url": "http://payment-service.test",
        "http_client": http_client,
    }
    log_result = FaultLabLogsAdapter(**adapter_args).query(
        QueryLogsInput(
            service="order-service",
            environment="local",
            time_range_start=now - timedelta(minutes=1),
            time_range_end=now,
        )
    )
    metrics_result = FaultLabMetricsAdapter(**adapter_args).query(
        QueryMetricsInput(service="order-service", environment="local")
    )
    trace_result = FaultLabTracesAdapter(**adapter_args).query(
        QueryTracesInput(
            service="order-service",
            environment="local",
            time_range_start=now - timedelta(minutes=1),
            time_range_end=now,
        )
    )
    deployment_result = FaultLabDeploymentAdapter(**adapter_args).query(
        GetDeploymentHistoryInput(service="order-service", environment="local")
    )
    http_client.close()

    assert isinstance(log_result, LogQueryResult)
    assert isinstance(log_result.events[0], LogEvent)
    assert log_result.events[0].trace_id == "trace-1"
    assert not hasattr(log_result.events[0], "method")
    assert isinstance(metrics_result, MetricsQueryResult)
    assert isinstance(trace_result, TraceQueryResult)
    assert isinstance(trace_result.spans[0], TraceSpanRecord)
    assert trace_result.spans[0].service == "order-service"
    assert isinstance(deployment_result, DeploymentQueryResult)


def test_fault_lab_adapter_errors_preserve_the_shared_boundary() -> None:
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = FaultLabLogsAdapter(
        order_service_url="http://order-service.test",
        payment_service_url="http://payment-service.test",
        http_client=http_client,
    )

    with pytest.raises(AdapterError) as raised:
        adapter.query(
            QueryLogsInput(
                service="order-service",
                environment="local",
                time_range_start=now - timedelta(minutes=1),
                time_range_end=now,
            )
        )
    http_client.close()

    assert raised.value.code == "fault_lab_unavailable"
    assert raised.value.retryable


def test_fault_lab_provider_errors_share_adapter_error_semantics() -> None:
    for error_type in (
        LogsAdapterError,
        MetricsAdapterError,
        TracesAdapterError,
        DeploymentAdapterError,
    ):
        error = error_type("provider_unavailable", "provider request failed", retryable=True)

        assert isinstance(error, AdapterError)
        assert error.code == "provider_unavailable"
        assert error.retryable


def test_tool_consumers_depend_only_on_adapter_protocols() -> None:
    assert get_type_hints(query_logs)["logs_adapter"] is LogsAdapter
    assert get_type_hints(query_metrics)["metrics_adapter"] is MetricsAdapter
    assert get_type_hints(query_traces)["traces_adapter"] is TracesAdapter
    assert get_type_hints(get_deployment_history)["deployment_adapter"] is DeploymentAdapter
    dependency_hints = get_type_hints(ToolExecutionDependencies)
    assert dependency_hints["rag_service"] is tool_execution.RAGService
    assert dependency_hints["logs_adapter"] is LogsAdapter
    assert dependency_hints["metrics_adapter"] is MetricsAdapter
    assert dependency_hints["traces_adapter"] == TracesAdapter | None
    assert dependency_hints["deployment_adapter"] == DeploymentAdapter | None
    assert dependency_hints["available_tools"] == frozenset[ToolName]

    source = inspect.getsource(tool_execution)
    assert all(
        provider_name not in source
        for provider_name in (
            "FaultLabLogsAdapter",
            "FaultLabMetricsAdapter",
            "FaultLabTracesAdapter",
            "FaultLabDeploymentAdapter",
        )
    )
