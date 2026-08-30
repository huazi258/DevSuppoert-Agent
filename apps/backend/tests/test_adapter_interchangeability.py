"""Contract regressions for provider-neutral read-only investigation adapters."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

import devsupport_backend.agent.nodes.tool_execution as tool_execution_module
import devsupport_backend.tools.get_deployment_history as deployment_history_module
import devsupport_backend.tools.query_logs as query_logs_module
import devsupport_backend.tools.query_metrics as query_metrics_module
import devsupport_backend.tools.query_traces as query_traces_module
from devsupport_backend.agent.nodes.tool_execution import (
    ToolExecutionDependencies,
    tool_execution_node,
)
from devsupport_backend.agent.state import AgentStage, PendingToolCall, create_initial_agent_state
from devsupport_backend.models import Incident
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
from devsupport_backend.tools.deployments import FaultLabDeploymentAdapter
from devsupport_backend.tools.get_deployment_history import get_deployment_history
from devsupport_backend.tools.logs import FaultLabLogsAdapter
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter
from devsupport_backend.tools.query_logs import query_logs
from devsupport_backend.tools.query_metrics import query_metrics
from devsupport_backend.tools.query_traces import query_traces
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    QueryLogsInput,
    QueryMetricsInput,
    QueryTracesInput,
    ToolStatus,
)
from devsupport_backend.tools.traces import FaultLabTracesAdapter

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class InMemoryLogsAdapter:
    error: AdapterError | None = None

    def query(self, tool_input: QueryLogsInput) -> LogQueryResult:
        if self.error is not None:
            raise self.error
        return LogQueryResult(
            match_count=2,
            events=(
                LogEvent(
                    timestamp=NOW,
                    service=tool_input.service,
                    level="error",
                    message="checkout could not reach payment",
                    request_id="request-1",
                    trace_id="trace-1",
                    error_type="ReadTimeout",
                    status_code=504,
                    duration_ms=1_200.0,
                    downstream_service="payment",
                ),
                LogEvent(
                    timestamp=NOW + timedelta(seconds=1),
                    service=tool_input.service,
                    level="error",
                    message="checkout could not reach payment",
                    request_id="request-2",
                    trace_id="trace-2",
                    error_type="ReadTimeout",
                    status_code=504,
                    duration_ms=1_100.0,
                    downstream_service="payment",
                ),
            ),
        )


@dataclass(frozen=True)
class InMemoryMetricsAdapter:
    error: AdapterError | None = None

    def query(self, tool_input: QueryMetricsInput) -> MetricsQueryResult:
        if self.error is not None:
            raise self.error
        return MetricsQueryResult(
            service=tool_input.service,
            health_status="degraded",
            request_count=10,
            success_count=8,
            error_count=2,
            last_request_duration_ms=1_200.0,
            average_request_duration_ms=620.0,
        )


class InMemoryTracesAdapter:
    def query(self, tool_input: QueryTracesInput) -> TraceQueryResult:
        return TraceQueryResult(
            spans=(
                TraceSpanRecord(
                    trace_id="trace-1",
                    span_id="checkout-root",
                    parent_span_id=None,
                    service=tool_input.service,
                    operation="POST /checkout",
                    start_time=NOW,
                    end_time=NOW + timedelta(milliseconds=1_250),
                    duration_ms=1_250.0,
                    status="error",
                    error="payment timeout",
                ),
                TraceSpanRecord(
                    trace_id="trace-1",
                    span_id="payment-child",
                    parent_span_id="checkout-root",
                    service="payment",
                    operation="POST /charge",
                    start_time=NOW + timedelta(milliseconds=25),
                    end_time=NOW + timedelta(milliseconds=1_200),
                    duration_ms=1_175.0,
                    status="error",
                    error="deadline exceeded",
                ),
            )
        )


class InMemoryDeploymentAdapter:
    def query(self, tool_input: GetDeploymentHistoryInput) -> DeploymentQueryResult:
        return DeploymentQueryResult(
            service=tool_input.service,
            current_version="v2.4.0",
            previous_version="v2.3.0",
            deployed_at=NOW,
        )


def test_second_provider_and_fault_lab_adapters_share_runtime_protocols() -> None:
    in_memory_adapters = (
        (InMemoryLogsAdapter(), LogsAdapter),
        (InMemoryMetricsAdapter(), MetricsAdapter),
        (InMemoryTracesAdapter(), TracesAdapter),
        (InMemoryDeploymentAdapter(), DeploymentAdapter),
    )
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
    )
    adapter_args = {
        "order_service_url": "http://order-service.test",
        "payment_service_url": "http://payment-service.test",
        "http_client": http_client,
    }
    fault_lab_adapters = (
        (
            FaultLabLogsAdapter(**adapter_args),
            LogsAdapter,
        ),
        (
            FaultLabMetricsAdapter(**adapter_args),
            MetricsAdapter,
        ),
        (
            FaultLabTracesAdapter(**adapter_args),
            TracesAdapter,
        ),
        (
            FaultLabDeploymentAdapter(**adapter_args),
            DeploymentAdapter,
        ),
    )

    assert all(isinstance(adapter, contract) for adapter, contract in in_memory_adapters)
    assert all(isinstance(adapter, contract) for adapter, contract in fault_lab_adapters)
    http_client.close()


def test_second_provider_preserves_all_read_only_tool_output_contracts() -> None:
    logs = query_logs(
        QueryLogsInput(
            service="checkout",
            environment="local",
            time_range_start=NOW - timedelta(minutes=1),
            time_range_end=NOW + timedelta(minutes=1),
        ),
        InMemoryLogsAdapter(),
    )
    metrics = query_metrics(
        QueryMetricsInput(service="checkout", environment="local"),
        InMemoryMetricsAdapter(),
    )
    traces = query_traces(
        QueryTracesInput(
            service="checkout",
            environment="local",
            time_range_start=NOW - timedelta(minutes=1),
            time_range_end=NOW + timedelta(minutes=1),
        ),
        InMemoryTracesAdapter(),
    )
    deployments = get_deployment_history(
        GetDeploymentHistoryInput(service="checkout", environment="local"),
        InMemoryDeploymentAdapter(),
    )

    assert logs.status is ToolStatus.SUCCESS
    assert logs.match_count == 2
    assert logs.first_seen == NOW
    assert logs.last_seen == NOW + timedelta(seconds=1)
    assert logs.error_patterns[0].model_dump() == {"pattern": "ReadTimeout", "count": 2}
    assert [sample.request_id for sample in logs.samples] == ["request-1", "request-2"]
    assert logs.trace_ids == ["trace-1", "trace-2"]
    assert metrics.status is ToolStatus.SUCCESS
    assert metrics.metrics is not None
    assert metrics.metrics.service == "checkout"
    assert metrics.metrics.environment == "local"
    assert metrics.metrics.error_rate == 0.2
    assert traces.status is ToolStatus.SUCCESS
    assert [span.span_id for span in traces.traces[0].spans] == [
        "checkout-root",
        "payment-child",
    ]
    assert traces.traces[0].slowest_span is not None
    assert traces.traces[0].slowest_span.service == "checkout"
    assert deployments.status is ToolStatus.SUCCESS
    assert deployments.deployments[0].model_dump() == {
        "service": "checkout",
        "environment": "local",
        "current_version": "v2.4.0",
        "previous_version": "v2.3.0",
        "deployed_at": NOW,
    }


def test_second_provider_adapter_errors_use_the_existing_tool_failure_contract() -> None:
    error = AdapterError("provider_unavailable", "second provider unavailable", retryable=True)
    logs = query_logs(
        QueryLogsInput(
            service="checkout",
            environment="local",
            time_range_start=NOW - timedelta(minutes=1),
            time_range_end=NOW,
        ),
        InMemoryLogsAdapter(error),
    )
    metrics = query_metrics(
        QueryMetricsInput(service="checkout", environment="local"),
        InMemoryMetricsAdapter(error),
    )

    for output in (logs, metrics):
        assert output.status is ToolStatus.FAILURE
        assert output.error is not None
        assert output.error.code == "provider_unavailable"
        assert output.error.message == "second provider unavailable"
        assert output.error.retryable


def test_second_provider_result_reaches_runtime_evidence_through_tool_execution_node() -> None:
    incident = Incident(
        id=uuid4(),
        service="checkout",
        environment="local",
        description="Checkout requests are timing out.",
        time_range_start=NOW - timedelta(minutes=1),
        time_range_end=NOW + timedelta(minutes=1),
    )
    state = create_initial_agent_state(incident, symptoms=["Checkout requests time out"])
    state["current_goal"] = "Collect checkout error facts."
    state["current_stage"] = AgentStage.TOOL_EXECUTION
    state["pending_tool_call"] = PendingToolCall(
        investigation_goal=state["current_goal"],
        tool_name=ToolName.QUERY_LOGS,
        tool_arguments={
            "service": "checkout",
            "environment": "local",
            "time_range_start": (NOW - timedelta(minutes=1)).isoformat(),
            "time_range_end": NOW.isoformat(),
        },
        reason="Runtime logs can establish the checkout failure facts.",
    )
    dependencies = ToolExecutionDependencies(  # type: ignore[arg-type]
        rag_service=object(),
        logs_adapter=InMemoryLogsAdapter(),
        metrics_adapter=InMemoryMetricsAdapter(),
        traces_adapter=InMemoryTracesAdapter(),
        deployment_adapter=InMemoryDeploymentAdapter(),
    )

    updated = tool_execution_node(state, dependencies)

    assert updated["current_stage"] is AgentStage.HYPOTHESIS_UPDATE
    assert updated["tool_call_count"] == 1
    assert updated["tool_history"][0].status is ToolStatus.SUCCESS
    assert updated["tool_history"][0].tool_name is ToolName.QUERY_LOGS
    assert updated["evidence"][0].source == ToolName.QUERY_LOGS.value
    assert updated["evidence"][0].data["match_count"] == 2


def test_tool_layer_contains_no_concrete_provider_references() -> None:
    for module in (
        query_logs_module,
        query_metrics_module,
        query_traces_module,
        deployment_history_module,
        tool_execution_module,
    ):
        source = inspect.getsource(module)

        assert "FaultLab" not in source
