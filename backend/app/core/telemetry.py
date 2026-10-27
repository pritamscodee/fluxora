import logging

from app.core.config import settings

log = logging.getLogger("fluxora")


def setup_telemetry(app=None) -> None:
    """Wire OpenTelemetry. A missing OTLP endpoint is normal in dev, so we degrade to
    in-process tracing rather than failing startup."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("opentelemetry sdk not installed; telemetry disabled")
        return

    resource = Resource.create(
        {SERVICE_NAME: settings.otel_service_name, "deployment.environment": settings.environment}
    )
    provider = TracerProvider(resource=resource)

    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces")
            )
        )
    trace.set_tracer_provider(provider)

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, excluded_urls="/health,/healthz")
        except ImportError:
            pass

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        from app.core.db import engine

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    except Exception as exc:  # instrumentation must never block boot
        log.debug("sqlalchemy instrumentation skipped: %s", exc)


def tracer(name: str = "fluxora"):
    from opentelemetry import trace

    return trace.get_tracer(name)
