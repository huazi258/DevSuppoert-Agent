"""Minimal payment-service HTTP API for the DevSupport fault lab."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from opentelemetry import trace
from pydantic import BaseModel, Field, StringConstraints
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from payment_service.deployment_state import get_deployment_state
from payment_service.log_buffer import LogBuffer
from payment_service.runtime_state import (
    metrics_snapshot,
    record_payment_result,
    reset_runtime_state,
    response_delay_seconds,
)
from payment_service.telemetry import configure_telemetry

SERVICE_NAME = "payment-service"
REQUEST_ID_HEADER = "X-Request-ID"
INTERNAL_FAULT_LAB_RESET_PATH = "/internal/fault-lab/reset"


class JsonFormatter(logging.Formatter):
    """Format request logs as compact structured JSON."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "service": SERVICE_NAME,
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        for field in ("method", "path", "status_code", "duration_ms", "request_id"):
            value = getattr(record, field, None)
            if value is not None:
                log_entry[field] = value
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            log_entry["trace_id"] = f"{span_context.trace_id:032x}"
            log_entry["span_id"] = f"{span_context.span_id:016x}"
        return json.dumps(log_entry)


class BufferedJsonStreamHandler(logging.StreamHandler):
    """Write one formatted JSON event to stdout and the bounded in-memory buffer."""

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        """Keep stdout and the query buffer on the exact same formatted event."""
        try:
            serialized_event = self.format(record)
            self._buffer.append(json.loads(serialized_event))
            self.stream.write(serialized_event + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


def configure_logger() -> logging.Logger:
    """Configure the service logger once without adding external logging dependencies."""
    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = BufferedJsonStreamHandler(log_buffer)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    return logger


log_buffer = LogBuffer()
logger = configure_logger()

OrderId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PaymentRequest(BaseModel):
    order_id: OrderId
    amount: Annotated[float, Field(gt=0)]


class PaymentResponse(BaseModel):
    payment_id: str
    order_id: str
    status: str


app = FastAPI(title=SERVICE_NAME)


@app.middleware("http")
async def log_http_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Preserve or create a request ID and log request completion details."""
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
    started_at = perf_counter()

    response = await call_next(request)
    duration_ms = round((perf_counter() - started_at) * 1000, 2)
    response.headers[REQUEST_ID_HEADER] = request_id
    if request.url.path == INTERNAL_FAULT_LAB_RESET_PATH:
        return response
    logger.info(
        "http_request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "request_id": request_id,
        },
    )
    return response


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return the service health used by the fault lab."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/internal/metrics")
def internal_metrics() -> dict[str, str | int | float | None]:
    """Return minimal runtime evidence for the future metrics adapter."""
    return {"service": SERVICE_NAME, **metrics_snapshot()}


@app.get("/internal/logs")
def internal_logs(
    time_range_start: datetime,
    time_range_end: datetime,
    level: str | None = Query(default=None, max_length=20),
    query: str | None = Query(default=None, max_length=1_000),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    """Return bounded, filtered structured stdout events for the Fault Lab adapter."""
    if time_range_start.tzinfo is None or time_range_end.tzinfo is None:
        raise HTTPException(status_code=422, detail="time range must include a timezone")
    if time_range_start > time_range_end:
        raise HTTPException(
            status_code=422,
            detail="time_range_start must not be after time_range_end",
        )
    match_count, events = log_buffer.query(
        time_range_start=time_range_start,
        time_range_end=time_range_end,
        level=level,
        query=query,
        limit=limit,
    )
    return {"service": SERVICE_NAME, "match_count": match_count, "events": events}


@app.get("/internal/traces")
def internal_traces(
    time_range_start: datetime,
    time_range_end: datetime,
    trace_id: str | None = Query(default=None, min_length=1, max_length=128),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    """Return bounded facts from real ended OpenTelemetry spans in this service."""
    if time_range_start.tzinfo is None or time_range_end.tzinfo is None:
        raise HTTPException(status_code=422, detail="time range must include a timezone")
    if time_range_start > time_range_end:
        raise HTTPException(
            status_code=422,
            detail="time_range_start must not be after time_range_end",
        )
    match_count, spans = telemetry.span_buffer.query(
        time_range_start=time_range_start,
        time_range_end=time_range_end,
        trace_id=trace_id,
        limit=limit,
    )
    return {"service": SERVICE_NAME, "match_count": match_count, "spans": spans}


@app.get("/internal/deployment")
def internal_deployment() -> dict[str, str | None]:
    """Return stable deployment facts separate from runtime timeout state."""
    deployment = get_deployment_state()
    return {
        "service": deployment.service,
        "current_version": deployment.current_version,
        "previous_version": deployment.previous_version,
        "deployed_at": deployment.deployed_at,
    }


@app.post(INTERNAL_FAULT_LAB_RESET_PATH)
def internal_fault_lab_reset() -> dict[str, str]:
    """Reset this process's local Fault Lab state without retaining control evidence."""
    reset_runtime_state()
    log_buffer.clear()
    telemetry.span_buffer.clear()
    return {"service": SERVICE_NAME, "status": "reset"}


@app.post("/payments", response_model=PaymentResponse)
async def create_payment(payment: PaymentRequest) -> PaymentResponse:
    """Approve every valid payment request in the normal fault-lab state."""
    started_at = perf_counter()
    delay_seconds = response_delay_seconds()
    succeeded = False

    try:
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        response = PaymentResponse(
            payment_id=f"pay-{uuid4().hex}",
            order_id=payment.order_id,
            status="approved",
        )
        succeeded = True
        return response
    finally:
        record_payment_result(
            succeeded=succeeded,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )


telemetry = configure_telemetry(app, SERVICE_NAME)
app.router.on_shutdown.append(telemetry.shutdown)
