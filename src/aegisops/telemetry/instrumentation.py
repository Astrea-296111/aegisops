from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram

TRACER = trace.get_tracer("aegisops")
COUNTER = Counter("aegis_operations_total", "Operations in this process", ["kind", "status"])
DURATION = Histogram("aegis_run_duration_seconds", "Completed run wall duration")


def configure(endpoint: str, service: str = "aegisops") -> None:
    if not endpoint:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=5)
        )
    )
    trace.set_tracer_provider(provider)


def span(name: str, **attributes: Any) -> Any:
    return TRACER.start_as_current_span(
        name, attributes=attributes, record_exception=False, set_status_on_exception=False
    )
