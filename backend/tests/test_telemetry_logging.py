"""Trace-correlation logging tests (brief task 7.2.4).

Coverage:
  1. processor no-op when no span is active
  2. processor injects trace_id + span_id when span is active
  3. injected IDs match the active span's context (canonical hex)
  4. processor no-op when only an invalid span context is current
  5. trace_id and span_id are 32 / 16 lowercase hex chars

Pure unit tests on `add_trace_context`. We don't reconfigure structlog —
the processor's contract is `(logger, method_name, event_dict) -> dict`,
so we call it directly with a synthetic event_dict.
"""
from __future__ import annotations

import re

import pytest

from app.core.logging import add_trace_context


@pytest.fixture
def tracer():
    """Provide a real Tracer wired to a TracerProvider so spans are recording.

    No global state mutation: we hold a local provider and ask it for a
    tracer directly. This bypasses OTel's `_TRACER_PROVIDER_SET_ONCE` lock
    and avoids leaking into other tests.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    return provider.get_tracer("tests.test_telemetry_logging")


# ---------------------------------------------------------------------------


def test_processor_noop_when_no_span_active():
    """Outside any span → event_dict returned unchanged."""
    event_dict = {"event": "hello", "k": "v"}
    out = add_trace_context(None, "info", dict(event_dict))
    assert out == event_dict
    assert "trace_id" not in out
    assert "span_id" not in out


def test_processor_injects_trace_and_span_ids_when_span_active(tracer):
    """Inside `start_as_current_span` → both IDs present and well-formed."""
    with tracer.start_as_current_span("test.span"):
        out = add_trace_context(None, "info", {"event": "hello"})

    assert "trace_id" in out
    assert "span_id" in out
    assert re.fullmatch(r"[0-9a-f]{32}", out["trace_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", out["span_id"])


def test_injected_ids_match_active_span_context(tracer):
    """Hex-formatted IDs equal the active span's `SpanContext` values."""
    with tracer.start_as_current_span("test.span") as span:
        ctx = span.get_span_context()
        expected_trace = format(ctx.trace_id, "032x")
        expected_span = format(ctx.span_id, "016x")
        out = add_trace_context(None, "info", {"event": "hello"})

    assert out["trace_id"] == expected_trace
    assert out["span_id"] == expected_span


def test_nested_spans_log_under_inner_span(tracer):
    """Logs inside a child span carry the child's span_id, parent's trace_id."""
    with tracer.start_as_current_span("outer") as outer:
        outer_ctx = outer.get_span_context()
        with tracer.start_as_current_span("inner") as inner:
            inner_ctx = inner.get_span_context()
            out = add_trace_context(None, "info", {"event": "nested"})

    # Same trace, different spans.
    assert out["trace_id"] == format(outer_ctx.trace_id, "032x")
    assert out["trace_id"] == format(inner_ctx.trace_id, "032x")
    assert out["span_id"] == format(inner_ctx.span_id, "016x")
    assert out["span_id"] != format(outer_ctx.span_id, "016x")


def test_processor_preserves_existing_event_dict_keys(tracer):
    """Adding trace context must not clobber other fields."""
    with tracer.start_as_current_span("test.span"):
        out = add_trace_context(
            None, "info", {"event": "hi", "user_id": "u1", "k": 42}
        )

    assert out["event"] == "hi"
    assert out["user_id"] == "u1"
    assert out["k"] == 42
    assert "trace_id" in out
