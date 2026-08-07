"""Structured Tool executor for current Fault Lab metric snapshots."""

from time import perf_counter

from devsupport_backend.tools.metrics import FaultLabMetricsAdapter, MetricsAdapterError
from devsupport_backend.tools.schemas import (
    MetricSnapshot,
    QueryMetricsInput,
    QueryMetricsOutput,
    ToolError,
    ToolStatus,
)


def query_metrics(
    tool_input: QueryMetricsInput,
    metrics_adapter: FaultLabMetricsAdapter,
) -> QueryMetricsOutput:
    """Return one real runtime snapshot without manufacturing metric time series."""
    started_at = perf_counter()
    try:
        result = metrics_adapter.query(tool_input)
    except MetricsAdapterError as error:
        return QueryMetricsOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(code=error.code, message=str(error), retryable=error.retryable),
            duration_ms=_duration_ms(started_at),
        )

    return QueryMetricsOutput(
        status=ToolStatus.SUCCESS,
        duration_ms=_duration_ms(started_at),
        metrics=MetricSnapshot(
            service=result.service,
            environment=tool_input.environment,
            health_status=result.health_status,
            request_count=result.request_count,
            success_count=result.success_count,
            error_count=result.error_count,
            error_rate=result.error_rate,
            last_request_duration_ms=result.last_request_duration_ms,
            average_request_duration_ms=result.average_request_duration_ms,
        ),
    )


def _duration_ms(started_at: float) -> float:
    """Return a non-negative elapsed duration for the future ToolCall audit."""
    return max(0.0, round((perf_counter() - started_at) * 1_000, 2))
