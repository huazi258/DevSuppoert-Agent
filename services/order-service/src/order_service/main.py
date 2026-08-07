"""Minimal order-service HTTP API for the DevSupport fault lab."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from opentelemetry import trace
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from order_service.config import settings
from order_service.deployment_state import FAULTY_VERSION, get_deployment_state
from order_service.log_buffer import LogBuffer
from order_service.runtime_state import (
    get_runtime_state,
    metrics_snapshot,
    record_order_result,
)
from order_service.telemetry import configure_telemetry

SERVICE_NAME = "order-service"
PAYMENT_SERVICE_NAME = "payment-service"
REQUEST_ID_HEADER = "X-Request-ID"


class JsonFormatter(logging.Formatter):
    """Format service logs as compact structured JSON."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "service": SERVICE_NAME,
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        for field in (
            "method",
            "path",
            "status_code",
            "duration_ms",
            "request_id",
            "downstream_service",
            "downstream_status",
            "error_type",
            "version",
            "config_key",
        ):
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
    """Configure structured service logging with only the standard library."""
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


class OrderRequest(BaseModel):
    amount: Annotated[float, Field(gt=0)]


class PaymentResult(BaseModel):
    payment_id: str
    order_id: str
    status: str


class OrderResponse(BaseModel):
    order_id: str
    payment_id: str
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own one timeout-bounded downstream client for the app lifecycle."""
    async with httpx.AsyncClient(
        base_url=settings.payment_service_url.rstrip("/"),
        timeout=settings.payment_timeout_seconds,
    ) as payment_client:
        app.state.payment_client = payment_client
        yield


def get_payment_client(request: Request) -> httpx.AsyncClient:
    """Return the app-scoped downstream client for this request."""
    return request.app.state.payment_client


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


@app.middleware("http")
async def log_http_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Preserve or create a request ID and log request completion details."""
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
    request.state.request_id = request_id
    started_at = perf_counter()

    response = await call_next(request)
    duration_ms = round((perf_counter() - started_at) * 1000, 2)
    response.headers[REQUEST_ID_HEADER] = request_id
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
    """Return only this service's liveness state."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/internal/metrics")
def internal_metrics() -> dict[str, str | int | float | None]:
    """Return minimal runtime request metrics for the future metrics adapter."""
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
    """Return only deployment facts for the future deployment adapter."""
    deployment = get_deployment_state()
    return {
        "service": deployment.service,
        "current_version": deployment.current_version,
        "previous_version": deployment.previous_version,
        "deployed_at": deployment.deployed_at,
    }


@app.post("/orders", response_model=OrderResponse)
async def create_order(
    order: OrderRequest,
    request: Request,
    payment_client: httpx.AsyncClient = Depends(get_payment_client),
) -> OrderResponse:
    """Create a normal-state order after payment-service approval."""
    started_at = perf_counter()
    order_id = f"order-{uuid4().hex}"
    request_id = request.state.request_id
    runtime_state = get_runtime_state()
    deployment = get_deployment_state()

    if (
        deployment.current_version == FAULTY_VERSION
        and not runtime_state.payment_timeout_configured
    ):
        duration_ms = round((perf_counter() - started_at) * 1000, 2)
        logger.error(
            "required runtime configuration is missing",
            extra={
                "request_id": request_id,
                "error_type": "MissingRequiredConfiguration",
                "version": deployment.current_version,
                "config_key": "PAYMENT_TIMEOUT",
            },
        )
        record_order_result(succeeded=False, duration_ms=duration_ms)
        raise HTTPException(status_code=500, detail="Order service configuration error")

    try:
        payment_response = await payment_client.post(
            "/payments",
            json={"order_id": order_id, "amount": order.amount},
            headers={REQUEST_ID_HEADER: request_id},
        )
        payment_response.raise_for_status()
        payment = PaymentResult.model_validate(payment_response.json())
    except (httpx.HTTPError, ValidationError, ValueError) as error:
        downstream_status = getattr(getattr(error, "response", None), "status_code", None)
        logger.error(
            "payment_request_failed",
            extra={
                "request_id": request_id,
                "downstream_service": PAYMENT_SERVICE_NAME,
                "downstream_status": downstream_status,
                "error_type": type(error).__name__,
            },
        )
        record_order_result(
            succeeded=False,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        raise HTTPException(status_code=502, detail="Payment service unavailable") from error

    if payment.order_id != order_id or payment.status != "approved":
        logger.error(
            "payment_response_rejected",
            extra={
                "request_id": request_id,
                "downstream_service": PAYMENT_SERVICE_NAME,
                "downstream_status": payment_response.status_code,
            },
        )
        record_order_result(
            succeeded=False,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        raise HTTPException(status_code=502, detail="Payment service returned an invalid result")

    record_order_result(
        succeeded=True,
        duration_ms=round((perf_counter() - started_at) * 1000, 2),
    )
    return OrderResponse(
        order_id=order_id,
        payment_id=payment.payment_id,
        status="confirmed",
    )


telemetry = configure_telemetry(app, SERVICE_NAME)
app.router.on_shutdown.append(telemetry.shutdown)
