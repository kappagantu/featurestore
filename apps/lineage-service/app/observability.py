import json
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram, make_asgi_app


REQUEST_COUNTER = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["service", "method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "method", "path"],
)
REQUEST_ERRORS = Counter(
    "http_request_errors_total",
    "Total HTTP errors",
    ["service", "method", "path", "status"],
)


def configure_logging(service: str) -> logging.Logger:
    logger = logging.getLogger(service)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, default=str))


def configure_tracing(service: str) -> None:
    if os.getenv("OTEL_ENABLED", "false").lower() != "true":
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318/v1/traces")
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def instrument_fastapi(app: FastAPI, service: str, logger: logging.Logger) -> None:
    configure_tracing(service)
    tracer = trace.get_tracer(service)
    app.mount("/metrics", make_asgi_app())

    @app.middleware("http")
    async def metrics_and_logs(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        method = request.method
        path = request.url.path
        start = time.perf_counter()

        with tracer.start_as_current_span(f"{method} {path}") as span:
            span.set_attribute("http.method", method)
            span.set_attribute("http.route", path)
            span.set_attribute("request.id", request_id)

            try:
                response = await call_next(request)
                status = response.status_code
            except Exception:
                status = 500
                REQUEST_ERRORS.labels(service, method, path, str(status)).inc()
                log_event(
                    logger,
                    "request_error",
                    service=service,
                    request_id=request_id,
                    method=method,
                    path=path,
                    status=status,
                )
                raise

        elapsed = time.perf_counter() - start
        REQUEST_COUNTER.labels(service, method, path, str(status)).inc()
        REQUEST_LATENCY.labels(service, method, path).observe(elapsed)
        if status >= 500:
            REQUEST_ERRORS.labels(service, method, path, str(status)).inc()

        response.headers["X-Request-Id"] = request_id
        log_event(
            logger,
            "request_complete",
            service=service,
            request_id=request_id,
            method=method,
            path=path,
            status=status,
            latency_ms=round(elapsed * 1000, 2),
        )
        return response
