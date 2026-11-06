import subprocess
import sys

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from jobsearch import tracing
from jobsearch.config import ConfigError

ENDPOINT = "http://localhost:6006/v1/traces"


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    assert tracing.setup(ENDPOINT, exporter=exporter)
    yield exporter
    tracing.shutdown()


def test_off_without_endpoint():
    assert not tracing.setup("")
    with tracing.span("step", {"a": 1}) as span:
        span.set({"b": 2})
        span.fail("egal")


def test_off_imports_nothing_from_opentelemetry():
    code = ("import sys, jobsearch.cli, jobsearch.tracing as t; t.setup('');"
            "\nwith t.span('x'): pass\nprint(any(m.startswith('opentelemetry') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False"


@pytest.mark.parametrize("endpoint", ["http://example.org:6006/v1/traces", "http://10.0.0.5:6006/v1/traces",
                                      "localhost:6006", "ftp://localhost/v1/traces"])
def test_non_local_endpoint_is_refused(endpoint):
    with pytest.raises(ConfigError, match="localhost"):
        tracing.setup(endpoint)


@pytest.mark.parametrize("endpoint", [ENDPOINT, "http://127.0.0.1:6006/v1/traces", "http://[::1]:6006/v1/traces"])
def test_local_endpoints_are_accepted(endpoint):
    tracing.check_local(endpoint)


def test_spans_nest_and_carry_project(spans):
    with tracing.span("outer"):
        with tracing.span("inner", {"jobsearch.count": 3, "skipped": None}):
            pass
    inner, outer = spans.get_finished_spans()
    assert inner.parent.span_id == outer.context.span_id
    assert dict(inner.attributes) == {"jobsearch.count": 3}
    assert outer.resource.attributes["openinference.project.name"] == "jobsearch"


def test_exception_marks_span_and_propagates(spans):
    with pytest.raises(ValueError):
        with tracing.span("step"):
            raise ValueError("kaputt")
    (span,) = spans.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[0].name == "exception"


def test_fail_marks_span_without_exception(spans):
    with tracing.span("step") as span:
        span.fail("unbrauchbar")
    (finished,) = spans.get_finished_spans()
    assert finished.status.status_code == StatusCode.ERROR
    assert finished.status.description == "unbrauchbar"


def test_shutdown_turns_tracing_off(spans):
    tracing.shutdown()
    with tracing.span("after"):
        pass
    assert spans.get_finished_spans() == ()


def test_failed_export_is_logged_as_warning_at_most(spans, caplog):
    import logging

    logging.getLogger(tracing.EXPORT_LOGGERS[0]).error("Failed to export spans batch")
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_step_carries_only_the_numbers_it_changed(spans):
    import dataclasses

    @dataclasses.dataclass
    class Counts:
        seen: int = 0
        kept: int = 2
        errors: list = dataclasses.field(default_factory=list)
        error: str | None = None
        done: bool = False

    counts = Counts()
    with tracing.step("collect", counts):
        counts.seen = 5
        counts.errors.append("Suche 'Beispielberuf': kaputt")
        counts.error = "Text aus einer Anzeige"
    (span,) = spans.get_finished_spans()
    assert dict(span.attributes) == {"jobsearch.seen": 5, "jobsearch.errors": 1}


def test_step_is_a_no_op_when_off():
    class NotADataclass:
        pass

    with tracing.step("collect", NotADataclass()) as span:
        span.set({"a": 1})
