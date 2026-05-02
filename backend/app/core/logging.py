"""Structured logging via structlog → JSON lines on stdout."""
import logging
import sys

import structlog
from opentelemetry import trace

from app.core.config import settings


def add_trace_context(_logger, _method_name, event_dict):
    """structlog processor: inject `trace_id` + `span_id` from active OTel span.

    Additive and defensive: when telemetry is disabled the global tracer
    is a NoOp and no recording span is ever current, so this is a clean
    no-op. When a span is active (FastAPI request, agent.tick, manual
    span anywhere in the call stack), the log entry gets both IDs in
    canonical OTel hex format (32-char trace, 16-char span) — the same
    encoding used by Jaeger / Tempo / Loki for cross-tool correlation.
    """
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return event_dict
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return event_dict
    event_dict["trace_id"] = format(ctx.trace_id, "032x")
    event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging() -> None:
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.dict_tracebacks,
            # Add trace correlation here — after the raw enrichers, before
            # the renderers that finalise the wire format. Order matters:
            # JSONRenderer serialises whatever's in the dict at this point.
            add_trace_context,
            structlog.processors.EventRenamer("msg"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger()
