import json
import logging
import os
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram, start_http_server


JOBS_PROCESSED = Counter("worker_jobs_processed_total", "Total processed materialization jobs")
JOBS_FAILED = Counter("worker_jobs_failed_total", "Total failed materialization jobs")
JOB_DURATION = Histogram("worker_job_duration_seconds", "Materialization job duration seconds")


def configure_logging(service: str) -> logging.Logger:
    logger = logging.getLogger(service)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, default=str))


def configure_tracing(service: str):
    if os.getenv("OTEL_ENABLED", "false").lower() != "true":
        return trace.get_tracer(service)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318/v1/traces")
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service)


def start_metrics_server() -> None:
    port = int(os.getenv("WORKER_METRICS_PORT", "9101"))
    start_http_server(port)
