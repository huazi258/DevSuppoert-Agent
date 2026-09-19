"""M2.3 regressions for the provider-neutral V2 runtime Tool boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from devsupport_backend.tools.adapter_contracts import (
    AdapterError,
    DeploymentQueryResult,
    LogEvent,
    LogQueryResult,
    TraceQueryResult,
    TraceSpanRecord,
)
from devsupport_backend.tools.get_deployment_history import get_deployment_history
from devsupport_backend.tools.logs import FaultLabLogsAdapter
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter
from devsupport_backend.tools.opensearch_logs import OpenSearchLogsAdapter
from devsupport_backend.tools.prometheus_metrics import PrometheusMetricsAdapter
from devsupport_backend.tools.query_logs import query_logs
from devsupport_backend.tools.query_metrics import query_metrics
from devsupport_backend.tools.query_traces import query_traces
from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    QueryLogsInput,
    QueryMetricsInput,
    QueryTracesInput,
    ToolStatus,
)

NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def _logs_input() -> QueryLogsInput:
    return QueryLogsInput(
        service="checkout",
        environment="local",
        time_range_start=NOW - timedelta(minutes=5),
        time_range_end=NOW,
        limit=20,
    )


def _metrics_input() -> QueryMetricsInput:
    return QueryMetricsInput(service="checkout", environment="local")


def test_fault_lab_and_opensearch_logs_share_a_bounded_agent_facing_schema() -> None:
    def fault_lab_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/logs"
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
                        "message": "payment timed out",
                        "trace_id": "trace-1",
                        "error_type": "ReadTimeout",
                    }
                ],
            },
            request=request,
        )

    def opensearch_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": {
                    "total": {"value": 1, "relation": "eq"},
                    "hits": [
                        {
                            "_index": "provider-private-index",
                            "_score": 99,
                            "_source": {
                                "@timestamp": NOW.isoformat(),
                                "body": "payment timed out",
                                "severity": {"text": "ERROR", "number": 17},
                                "resource": {
                                    "service.name": "checkout",
                                    "service.version": "provider-private-version",
                                },
                                "traceId": "trace-1",
                                "attributes": {
                                    "exception.type": "ReadTimeout",
                                    "provider.private": "must-not-leak",
                                },
                            },
                        }
                    ],
                }
            },
            request=request,
        )

    fault_client = httpx.Client(transport=httpx.MockTransport(fault_lab_handler))
    open_client = httpx.Client(transport=httpx.MockTransport(opensearch_handler))
    fault_output = query_logs(
        QueryLogsInput(
            service="order-service",
            environment="local",
            time_range_start=NOW - timedelta(minutes=5),
            time_range_end=NOW,
        ),
        FaultLabLogsAdapter(
            order_service_url="http://fault-lab-orders.test",
            payment_service_url="http://fault-lab-payments.test",
            http_client=fault_client,
        ),
    )
    open_output = query_logs(
        _logs_input(),
        OpenSearchLogsAdapter(
            opensearch_url="http://opensearch.test",
            http_client=open_client,
        ),
    )
    fault_client.close()
    open_client.close()

    assert type(fault_output) is type(open_output)
    assert set(fault_output.model_dump()) == set(open_output.model_dump())
    assert fault_output.provenance is not None
    assert open_output.provenance is not None
    assert fault_output.provenance.source == "fault_lab"
    assert open_output.provenance.source == "opensearch"
    assert open_output.provenance.time_range_start == NOW - timedelta(minutes=5)
    assert "provider-private" not in str(open_output.model_dump())
    assert "http://opensearch.test" not in str(open_output.model_dump())


def test_fault_lab_and_prometheus_metrics_share_a_normalized_agent_facing_schema() -> None:
    def fault_lab_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/metrics":
            return httpx.Response(
                200,
                json={
                    "service": "order-service",
                    "request_count": 10,
                    "success_count": 8,
                    "error_count": 2,
                    "last_request_duration_ms": 25,
                    "average_request_duration_ms": 10,
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"service": "order-service", "status": "degraded"},
            request=request,
        )

    def prometheus_handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if query.startswith("count by"):
            value = "1"
        elif "status_code" in query:
            value = "2"
        elif "calls_total" in query:
            value = "10"
        elif "_count" in query:
            value = "10"
        else:
            value = "100"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {
                                "service_name": "checkout",
                                "provider_private": "must-not-leak",
                            },
                            "value": [NOW.timestamp(), value],
                        }
                    ],
                },
            },
            request=request,
        )

    fault_client = httpx.Client(transport=httpx.MockTransport(fault_lab_handler))
    prom_client = httpx.Client(transport=httpx.MockTransport(prometheus_handler))
    fault_output = query_metrics(
        QueryMetricsInput(service="order-service", environment="local"),
        FaultLabMetricsAdapter(
            order_service_url="http://fault-lab-orders.test",
            payment_service_url="http://fault-lab-payments.test",
            http_client=fault_client,
        ),
    )
    prom_output = query_metrics(
        _metrics_input(),
        PrometheusMetricsAdapter(
            prometheus_url="http://prometheus.test",
            http_client=prom_client,
        ),
    )
    fault_client.close()
    prom_client.close()

    assert type(fault_output) is type(prom_output)
    assert set(fault_output.model_dump()) == set(prom_output.model_dump())
    assert fault_output.provenance is not None
    assert prom_output.provenance is not None
    assert fault_output.provenance.source == "fault_lab"
    assert prom_output.provenance.source == "prometheus"
    assert "provider_private" not in str(prom_output.model_dump())
    assert "http://prometheus.test" not in str(prom_output.model_dump())


@dataclass(frozen=True)
class _FailingLogsAdapter:
    error: AdapterError

    def query(self, tool_input: QueryLogsInput) -> LogQueryResult:
        del tool_input
        raise self.error


def test_agent_facing_errors_are_normalized_and_do_not_leak_provider_secrets() -> None:
    secret = "provider-token-must-not-leak"
    for adapter_error, expected_code in (
        (AdapterError("timeout", f"Authorization: Bearer {secret}", retryable=True), "timeout"),
        (AdapterError("invalid_opensearch_response", secret), "invalid_provider_response"),
        (AdapterError("unsupported_environment", secret), "invalid_request"),
    ):
        output = query_logs(_logs_input(), _FailingLogsAdapter(adapter_error))

        assert output.status is ToolStatus.FAILURE
        assert output.error is not None
        assert output.error.code == expected_code
        assert secret not in output.error.message
        assert secret not in str(output.model_dump())


def test_actual_provider_timeouts_and_malformed_payloads_use_the_shared_error_contract() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider-token-must-not-leak", request=request)

    def malformed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    fault_log_client = httpx.Client(transport=httpx.MockTransport(timeout_handler))
    open_client = httpx.Client(transport=httpx.MockTransport(malformed_handler))
    fault_metric_client = httpx.Client(transport=httpx.MockTransport(timeout_handler))
    prom_client = httpx.Client(transport=httpx.MockTransport(malformed_handler))
    outputs = (
        query_logs(
            QueryLogsInput(
                service="order-service",
                environment="local",
                time_range_start=NOW - timedelta(minutes=5),
                time_range_end=NOW,
            ),
            FaultLabLogsAdapter(
                order_service_url="http://fault-lab-orders.test",
                payment_service_url="http://fault-lab-payments.test",
                http_client=fault_log_client,
            ),
        ),
        query_logs(
            _logs_input(),
            OpenSearchLogsAdapter(
                opensearch_url="http://opensearch.test",
                http_client=open_client,
            ),
        ),
        query_metrics(
            QueryMetricsInput(service="order-service", environment="local"),
            FaultLabMetricsAdapter(
                order_service_url="http://fault-lab-orders.test",
                payment_service_url="http://fault-lab-payments.test",
                http_client=fault_metric_client,
            ),
        ),
        query_metrics(
            _metrics_input(),
            PrometheusMetricsAdapter(
                prometheus_url="http://prometheus.test",
                http_client=prom_client,
            ),
        ),
    )
    fault_log_client.close()
    open_client.close()
    fault_metric_client.close()
    prom_client.close()

    assert [output.error.code if output.error else None for output in outputs] == [
        "timeout",
        "invalid_provider_response",
        "timeout",
        "invalid_provider_response",
    ]
    assert all("provider-token-must-not-leak" not in str(output.model_dump()) for output in outputs)


@dataclass(frozen=True)
class _OversizedLogsAdapter:
    def query(self, tool_input: QueryLogsInput) -> LogQueryResult:
        return LogQueryResult(
            match_count=500,
            events=tuple(
                LogEvent(
                    timestamp=NOW + timedelta(seconds=index),
                    service=tool_input.service,
                    level="error",
                    message="x" * 3_000,
                    trace_id=f"trace-{index}",
                )
                for index in range(500)
            ),
        )


def test_oversized_adapter_results_are_bounded_before_reaching_the_agent() -> None:
    output = query_logs(_logs_input().model_copy(update={"limit": 100}), _OversizedLogsAdapter())

    assert output.status is ToolStatus.SUCCESS
    assert len(output.samples) == 100
    assert len(output.trace_ids) == 100
    assert len(output.error_patterns) == 1
    assert len(output.samples[0].message) == 2_000


@dataclass(frozen=True)
class _TracesAdapter:
    def query(self, tool_input: QueryTracesInput) -> TraceQueryResult:
        return TraceQueryResult(
            spans=tuple(
                TraceSpanRecord(
                    trace_id="trace-1",
                    span_id=f"span-{index}",
                    parent_span_id=None,
                    service=tool_input.service,
                    operation="operation",
                    start_time=NOW + timedelta(milliseconds=index),
                    end_time=NOW + timedelta(milliseconds=index + 1),
                    duration_ms=1,
                    status="ok",
                )
                for index in range(150)
            )
        )


@dataclass(frozen=True)
class _DeploymentAdapter:
    def query(self, tool_input: GetDeploymentHistoryInput) -> DeploymentQueryResult:
        return DeploymentQueryResult(
            service=tool_input.service,
            current_version="v2",
            previous_version="v1",
            deployed_at=NOW,
        )


def test_traces_and_deployment_results_keep_the_same_safe_provenance_boundary() -> None:
    traces = query_traces(
        QueryTracesInput(
            service="checkout",
            environment="local",
            time_range_start=NOW - timedelta(minutes=5),
            time_range_end=NOW,
            limit=20,
        ),
        _TracesAdapter(),
    )
    deployments = get_deployment_history(
        GetDeploymentHistoryInput(service="checkout", environment="local"),
        _DeploymentAdapter(),
    )

    assert traces.status is ToolStatus.SUCCESS
    assert traces.provenance is not None
    assert len(traces.traces) == 1
    assert len(traces.traces[0].spans) == 100
    assert deployments.status is ToolStatus.SUCCESS
    assert deployments.provenance is not None
    assert len(deployments.deployments) == 1
