from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import get_type_hints

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
from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    QueryLogsInput,
    QueryMetricsInput,
    QueryTracesInput,
)


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
