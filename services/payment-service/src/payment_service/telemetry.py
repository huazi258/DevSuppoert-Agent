"""OpenTelemetry setup for payment-service traces."""

import os
from dataclasses import dataclass

from fastapi import FastAPI
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


@dataclass
class Telemetry:
    """Own the service-local tracing provider and optional exporter."""

    provider: TracerProvider

    def shutdown(self) -> None:
        self.provider.shutdown()


def configure_telemetry(app: FastAPI, service_name: str) -> Telemetry:
    """Instrument FastAPI and HTTPX, exporting only when an OTLP endpoint is set."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    httpx_instrumentor = HTTPXClientInstrumentor()
    if not httpx_instrumentor.is_instrumented_by_opentelemetry:
        httpx_instrumentor.instrument(tracer_provider=provider)

    return Telemetry(provider=provider)
