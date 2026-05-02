"""OpenTelemetry tracing setup (FASE 7.2.3).

V0 instrumentation strategy (mirrors brief 7.2.3):

- **Auto-instrumentation** for the cross-cutting boundaries: FastAPI HTTP
  server (every request → server span), SQLAlchemy (every query → DB
  span), HTTPX (every outbound call → client span — covers Anthropic and
  the Self verifier).

- **Manual spans** for agent semantics that no library can infer. Naming
  convention is ``<domain>.<entity>.<action>`` lowercase dot-separated,
  aligned with OpenTelemetry semantic conventions:

      agent.tick           top-level: one per AgentOrchestrator.run_tick
      agent.matching       sub-span: search_matches tool dispatch
      agent.negotiation    sub-span: send/counter/reject/accept-offer
      agent.signing        sub-span: accept_offer (deal creation)

  Only ``agent.tick`` and ``agent.matching`` are wired explicitly in V0;
  ``agent.negotiation`` / ``agent.signing`` are reserved names — the
  generic ``agent.tool`` span carries a ``tool.category`` attribute that
  maps to the same taxonomy without name proliferation.

Disabled mode: when ``settings.telemetry_enabled=False``, this module
does not call ``trace.set_tracer_provider``. The global tracer stays the
SDK NoOp and every ``with tracer.start_as_current_span(...):`` becomes a
free-running context manager — zero overhead in tests and CLI tools that
don't trigger lifespan startup.

Idempotency: ``setup_telemetry`` guards against double-init. The hook in
``app.main`` calls it once during lifespan startup, but tests that build
their own apps can also call it without conflict.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from app.core.config import settings
from app.core.logging import log

if TYPE_CHECKING:
    from fastapi import FastAPI


_initialized = False


def setup_telemetry(app: "FastAPI | None" = None) -> bool:
    """Initialise the global tracer provider + auto-instrumentation.

    Returns ``True`` if telemetry was activated this call, ``False`` if
    disabled by settings or already initialised.
    """
    global _initialized
    if _initialized:
        return False
    if not settings.telemetry_enabled:
        log.info("telemetry.disabled_by_settings")
        return False

    resource = Resource.create({
        "service.name": settings.app_name,
        "service.version": settings.app_version,
        "deployment.environment": settings.app_env,
    })
    provider = TracerProvider(resource=resource)

    if settings.telemetry_exporter == "console":
        provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter())
        )
    elif settings.telemetry_exporter == "otlp":
        # Lazy import: the gRPC exporter pulls grpcio (~10MB). No reason
        # to load it when running in console mode for dev / tests.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        exporter = OTLPSpanExporter(endpoint=settings.telemetry_otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        raise ValueError(
            f"unknown telemetry_exporter: {settings.telemetry_exporter!r}"
        )

    trace.set_tracer_provider(provider)

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # `/metrics` and `/health` are scrape/probe endpoints — emitting
        # one span per scrape pollutes trace storage with no signal.
        FastAPIInstrumentor.instrument_app(
            app, excluded_urls="/metrics,/health"
        )

    from app.core.db import engine, sync_engine
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    # Async engine: SQLAlchemyInstrumentor wants the underlying sync
    # engine handle. AsyncEngine.sync_engine is the canonical accessor.
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    SQLAlchemyInstrumentor().instrument(engine=sync_engine)

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()

    _initialized = True
    log.info(
        "telemetry.initialized",
        exporter=settings.telemetry_exporter,
        otlp_endpoint=(
            settings.telemetry_otlp_endpoint
            if settings.telemetry_exporter == "otlp"
            else None
        ),
    )
    return True


def shutdown_telemetry() -> None:
    """Flush + uninstrument. Safe to call when telemetry was never set up."""
    global _initialized
    if not _initialized:
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    try:
        HTTPXClientInstrumentor().uninstrument()
    except Exception:  # noqa: BLE001
        pass
    try:
        SQLAlchemyInstrumentor().uninstrument()
    except Exception:  # noqa: BLE001
        pass
    try:
        FastAPIInstrumentor.uninstrument()
    except Exception:  # noqa: BLE001
        pass

    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception:  # noqa: BLE001
            pass

    _initialized = False
    log.info("telemetry.shutdown")


def get_tracer(name: str) -> trace.Tracer:
    """Tracer scoped to a module/component. NoOp when telemetry disabled."""
    return trace.get_tracer(name)
