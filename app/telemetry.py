"""OpenTelemetry setup for Order Tracker.

Controlled by two environment variables so the same code works for both
stages of the homework:

- OTEL_EXPORTER=console (default) -> traces/metrics/logs are printed to
  stdout, so you can inspect them with `docker compose logs app`
  (Question 2).
- OTEL_EXPORTER=otlp -> traces/metrics/logs are sent to an OpenTelemetry
  Collector at OTEL_EXPORTER_OTLP_ENDPOINT (default
  http://otel-collector:4317), which forwards them to Tempo, Prometheus
  and Loki (Question 3 onward).
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

SERVICE_NAME = "order-tracker"
_METRIC_EXPORT_INTERVAL_MS = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS", "5000"))


def _use_otlp() -> bool:
    return os.getenv("OTEL_EXPORTER", "console").strip().lower() == "otlp"


def _otlp_endpoint() -> str:
    return os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")


def setup_telemetry():
    """Initialize tracing, metrics and logging. Returns (tracer, order_lookup_counter)."""
    resource = Resource.create({"service.name": SERVICE_NAME})
    use_otlp = _use_otlp()
    endpoint = _otlp_endpoint()

    # --- Traces ---
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True) if use_otlp else ConsoleSpanExporter()
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    metric_exporter = OTLPMetricExporter(endpoint=endpoint, insecure=True) if use_otlp else ConsoleMetricExporter()
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=_METRIC_EXPORT_INTERVAL_MS)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    # --- Logs ---
    logger_provider = LoggerProvider(resource=resource)
    log_exporter = OTLPLogExporter(endpoint=endpoint, insecure=True) if use_otlp else ConsoleLogExporter()
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

    otel_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    root_logger = logging.getLogger()
    root_logger.addHandler(otel_handler)
    root_logger.setLevel(logging.INFO)

    tracer = trace.get_tracer(SERVICE_NAME)
    meter = metrics.get_meter(SERVICE_NAME)

    # Counter name has no "_total" suffix: the Prometheus exporter (used in
    # the Collector pipeline) appends it automatically, producing the
    # Prometheus metric "order_lookup_requests_total".
    order_lookup_counter = meter.create_counter(
        "order_lookup_requests",
        description="Count of order lookup requests, labeled by route and HTTP status code",
    )

    return tracer, order_lookup_counter
