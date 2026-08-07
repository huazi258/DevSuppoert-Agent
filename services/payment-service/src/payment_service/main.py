"""Minimal payment-service HTTP API for the DevSupport fault lab."""

import json
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel, Field, StringConstraints
from starlette.requests import Request
from starlette.responses import Response

SERVICE_NAME = "payment-service"
REQUEST_ID_HEADER = "X-Request-ID"


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
        return json.dumps(log_entry)


def configure_logger() -> logging.Logger:
    """Configure the service logger once without adding external logging dependencies."""
    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    return logger


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
async def log_http_request(request: Request, call_next: object) -> Response:
    """Preserve or create a request ID and log request completion details."""
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
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
    """Return the service health used by the fault lab."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/payments", response_model=PaymentResponse)
def create_payment(payment: PaymentRequest) -> PaymentResponse:
    """Approve every valid payment request in the normal fault-lab state."""
    return PaymentResponse(
        payment_id=f"pay-{uuid4().hex}",
        order_id=payment.order_id,
        status="approved",
    )
