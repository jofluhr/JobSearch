"""Optional tracing of runs and AI calls, viewed in a local Phoenix.

The only module that imports OpenTelemetry, and only when TRACING_ENDPOINT is set. Off, every
span is a no-op, so runs, tests and the scheduled task behave as without tracing.

Spans carry the prompts of the AI calls, which hold the Profile and Third-party text, so the
endpoint must be on this machine. Tracing never fails a run: export happens in the background,
and an unreachable Phoenix only loses the spans.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

from .config import ConfigError

log = logging.getLogger(__name__)

PROJECT = "jobsearch"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
EXPORT_TIMEOUT_SECONDS = 2
FLUSH_TIMEOUT_MILLIS = 5000

# OpenTelemetry's loggers that report a failed export, at ERROR, when Phoenix is not running.
EXPORT_LOGGERS = ("opentelemetry.exporter.otlp.proto.http.trace_exporter", "opentelemetry.sdk.trace.export",
                  "opentelemetry.sdk._shared_internal")

_provider = None
_tracer = None


class _AtMostWarning(logging.Filter):
    """A failed export loses only spans, never part of the run, so it is reported as a warning."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.WARNING:
            record.levelno, record.levelname = logging.WARNING, "WARNING"
        return True


class Span:
    """A span to add attributes to, or nothing when tracing is off."""

    def __init__(self, otel_span=None):
        self._span = otel_span

    def set(self, attributes: dict) -> None:
        """Adds the attributes, skipping None values, which OpenTelemetry does not accept."""
        if self._span is None:
            return
        for key, value in attributes.items():
            if value is not None:
                self._span.set_attribute(key, value)

    def fail(self, message: str) -> None:
        """Marks the span as failed without an exception, e.g. for an answer that was asked for again."""
        if self._span is None:
            return
        from opentelemetry.trace import Status, StatusCode

        self._span.set_status(Status(StatusCode.ERROR, message))


def check_local(endpoint: str) -> None:
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or parts.hostname not in LOCAL_HOSTS:
        raise ConfigError(f"TRACING_ENDPOINT muss auf diesen Rechner zeigen (localhost), nicht auf {endpoint!r}. "
                          "Die Traces enthalten das Profil und Stellenanzeigen.")


def setup(endpoint: str, exporter=None) -> bool:
    """Starts tracing when an endpoint is set. Returns whether tracing is on.

    `exporter` replaces the OTLP exporter and exports each span at once (for tests)."""
    global _provider, _tracer
    endpoint = endpoint.strip()
    if not endpoint:
        return False
    check_local(endpoint)
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        log.warning("Tracing ist aus: OpenTelemetry fehlt. Installieren mit: pip install -e \".[trace]\"")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": PROJECT,
                                                        "openinference.project.name": PROJECT}))
    for name in EXPORT_LOGGERS:  # filters act only on the logger a record starts at, not on its parents
        logger = logging.getLogger(name)
        if not any(isinstance(f, _AtMostWarning) for f in logger.filters):
            logger.addFilter(_AtMostWarning())
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, timeout=EXPORT_TIMEOUT_SECONDS)))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Kept to this module instead of set globally, so setup can run again (tests) and nothing
    # else in the process picks the provider up.
    _provider, _tracer = provider, provider.get_tracer(PROJECT)
    return True


@contextmanager
def span(name: str, attributes: dict | None = None) -> Iterator[Span]:
    """A span around the block, nested in the current one. An exception marks it failed and propagates."""
    if _tracer is None:
        yield Span()
        return
    with _tracer.start_as_current_span(name, record_exception=True, set_status_on_exception=True) as otel_span:
        wrapped = Span(otel_span)
        wrapped.set(attributes or {})
        yield wrapped


def numbers(counts) -> dict:
    """The numbers of a counts dataclass as span attributes. A list counts its entries; text is left out,
    so no Third-party text reaches a step's attributes."""
    attributes = {}
    for f in dataclasses.fields(counts):
        value = getattr(counts, f.name)
        if isinstance(value, list):
            value = len(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            attributes[f"jobsearch.{f.name}"] = value
    return attributes


@contextmanager
def step(name: str, counts) -> Iterator[Span]:
    """A span around one step of a run, carrying the numbers of `counts` the step changed."""
    if _tracer is None:
        yield Span()
        return
    before = numbers(counts)
    with span(name) as current:
        try:
            yield current
        finally:
            current.set({k: v for k, v in numbers(counts).items() if v != before.get(k)})


def shutdown() -> None:
    """Sends the spans still waiting, with a bounded wait, and turns tracing off."""
    global _provider, _tracer
    if _provider is None:
        return
    try:
        _provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        _provider.shutdown()
    except Exception:  # tracing must never cost the run
        log.warning("Tracing could not be flushed", exc_info=True)
    _provider = _tracer = None
