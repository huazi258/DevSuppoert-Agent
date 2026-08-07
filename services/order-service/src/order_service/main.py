"""Minimal order-service HTTP API for the DevSupport fault lab."""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException
from opentelemetry import trace
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from order_service.config import settings
from order_service.runtime_state import (
    FAULTY_VERSION,
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


def configure_logger() -> logging.Logger:
    """Configure structured service logging with only the standard library."""
    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    return logger


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


async def get_payment_client() -> AsyncIterator[httpx.AsyncClient]:
    """Provide a timeout-bounded client for the downstream payment service."""
    async with httpx.AsyncClient(
        base_url=settings.payment_service_url.rstrip("/"),
        timeout=settings.payment_timeout_seconds,
    ) as client:
        yield client


app = FastAPI(title=SERVICE_NAME)


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

    if (
        runtime_state.order_service_version == FAULTY_VERSION
        and not runtime_state.payment_timeout_configured
    ):
        duration_ms = round((perf_counter() - started_at) * 1000, 2)
        logger.error(
            "required runtime configuration is missing",
            extra={
                "request_id": request_id,
                "error_type": "MissingRequiredConfiguration",
                "version": runtime_state.order_service_version,
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
