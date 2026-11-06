import json
import subprocess

import pytest
from opentelemetry.trace import StatusCode

from jobsearch import ai, tracing


@pytest.fixture(autouse=True)
def claude_on_path(monkeypatch):
    monkeypatch.setattr(ai.shutil, "which", lambda name: "claude")


def envelope(structured=None, is_error=False, result="", model="claude-sonnet-test", usage=None):
    return json.dumps({"type": "result", "is_error": is_error, "result": result, "duration_ms": 1800,
                       "num_turns": 2, "structured_output": structured, "modelUsage": {model: usage or {}}})


USAGE = {"inputTokens": 12, "outputTokens": 340, "cacheReadInputTokens": 900, "cacheCreationInputTokens": 1600,
         "thinkingTokens": 80, "webSearchRequests": 0, "costUSD": 0.0123}


class FakeRunner:
    """Plays back prepared claude -p outputs and records the calls."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        stdout, code, stderr = self.outputs.pop(0)
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)


def ask(runner, check=None, recorder=None, purpose="scoring"):
    return ai.ask("prompt", model="sonnet", schema={"type": "object"}, system="sys", check=check, runner=runner,
                  purpose=purpose, usage_recorder=recorder)


def test_returns_structured_answer_and_model():
    runner = FakeRunner((envelope({"ok": True}), 0, ""))
    answer = ask(runner)
    assert answer.data == {"ok": True}
    assert answer.model == "claude-sonnet-test"
    cmd, kwargs = runner.calls[0]
    assert kwargs["input"] == "prompt"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--bare" not in cmd  # would ignore the subscription login


def test_invalid_answer_is_retried_once():
    def check(data):
        if not data.get("ok"):
            raise ai.InvalidAnswer("nicht ok")
    runner = FakeRunner((envelope({"ok": False}), 0, ""), (envelope({"ok": True}), 0, ""))
    assert ask(runner, check).data == {"ok": True}
    assert len(runner.calls) == 2


def test_gives_up_after_second_invalid_answer():
    runner = FakeRunner((envelope(None), 0, ""), (envelope(None), 0, ""))
    with pytest.raises(ai.AiError, match="zweimal"):
        ask(runner)


@pytest.mark.parametrize(
    "output, expected",
    [
        ((envelope(is_error=True, result="Claude usage limit reached. Resets at 5pm."), 1, ""), "Kontingent"),
        (("", 1, "Invalid API key · Please run /login"), "nicht angemeldet"),
        (("", 1, "something broke"), "fehlgeschlagen: something broke"),
    ],
)
def test_failures_are_not_retried_and_explained(output, expected):
    runner = FakeRunner(output)
    with pytest.raises(ai.AiError, match=expected):
        ask(runner)
    assert len(runner.calls) == 1


def test_missing_cli(monkeypatch):
    monkeypatch.setattr(ai.shutil, "which", lambda name: None)
    with pytest.raises(ai.AiError, match="nicht gefunden"):
        ask(FakeRunner())


def test_records_usage_of_a_successful_call():
    recorded = []
    ask(FakeRunner((envelope({"ok": True}, usage=USAGE), 0, "")), recorder=recorded.append)
    [u] = recorded
    assert (u.purpose, u.model, u.attempt, u.outcome) == ("scoring", "claude-sonnet-test", 1, "ok")
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens) == (12, 340, 900, 1600)
    assert (u.thinking_tokens, u.web_search_requests, u.cost_usd) == (80, 0, 0.0123)
    assert (u.duration_ms, u.num_turns) == (1800, 2)
    assert u.started_at


def test_records_each_attempt_of_a_retry():
    def check(data):
        if not data.get("ok"):
            raise ai.InvalidAnswer("nicht ok")
    recorded = []
    ask(FakeRunner((envelope({"ok": False}, usage=USAGE), 0, ""), (envelope({"ok": True}, usage=USAGE), 0, "")),
        check, recorded.append)
    assert [(u.attempt, u.outcome) for u in recorded] == [(1, "invalid"), (2, "ok")]
    assert recorded[0].output_tokens == 340  # a rejected answer used quota too


def test_records_a_quota_error_before_raising():
    recorded = []
    runner = FakeRunner((envelope(is_error=True, result="Claude usage limit reached.", usage=USAGE), 1, ""))
    with pytest.raises(ai.AiError, match="Kontingent"):
        ask(runner, recorder=recorded.append)
    [u] = recorded
    assert (u.outcome, u.cost_usd) == ("error", 0.0123)


def test_records_output_without_envelope_as_error_without_usage():
    recorded = []
    with pytest.raises(ai.AiError):
        ask(FakeRunner(("", 1, "something broke")), recorder=recorded.append)
    [u] = recorded
    assert (u.outcome, u.model, u.input_tokens, u.cost_usd) == ("error", "sonnet", None, None)


def test_records_a_timeout_without_usage():
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
    recorded = []
    with pytest.raises(ai.AiError, match="nicht geantwortet"):
        ask(runner, recorder=recorded.append)
    [u] = recorded
    assert (u.outcome, u.output_tokens, u.duration_ms) == ("timeout", None, None)


def test_a_failing_recorder_does_not_cost_the_answer():
    def recorder(usage):
        raise RuntimeError("database locked")
    assert ask(FakeRunner((envelope({"ok": True}), 0, "")), recorder=recorder).data == {"ok": True}


def test_unknown_purpose_is_refused():
    with pytest.raises(ValueError, match="purpose"):
        ask(FakeRunner(), purpose="scorring")


def test_usage_falls_back_to_the_call_wide_fields_without_model_usage():
    out = json.dumps({"type": "result", "is_error": True, "result": "something broke", "total_cost_usd": 0.02,
                      "usage": {"input_tokens": 3, "output_tokens": 50, "cache_read_input_tokens": 100,
                                "cache_creation_input_tokens": 200,
                                "server_tool_use": {"web_search_requests": 1}}})
    recorded = []
    with pytest.raises(ai.AiError):
        ask(FakeRunner((out, 1, "")), recorder=recorded.append)
    [u] = recorded
    assert (u.model, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens) == (
        "sonnet", 3, 50, 100, 200)
    assert (u.web_search_requests, u.thinking_tokens, u.cost_usd) == (1, None, 0.02)


@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    tracing.setup("http://localhost:6006/v1/traces", exporter=exporter)
    yield exporter
    tracing.shutdown()


def test_traces_a_successful_call(spans):
    ask(FakeRunner((envelope({"ok": True}, usage=USAGE), 0, "")))
    [span] = spans.get_finished_spans()
    a = span.attributes
    assert span.name == "claude -p scoring"
    assert span.status.status_code == StatusCode.UNSET
    assert a["openinference.span.kind"] == "LLM"
    assert (a["llm.input_messages.0.message.content"], a["input.value"]) == ("sys", "prompt")
    assert json.loads(a["output.value"]) == {"ok": True}
    assert a["llm.model_name"] == "claude-sonnet-test"
    assert (a["llm.token_count.prompt"], a["llm.token_count.completion"], a["llm.token_count.total"]) == (
        12 + 900 + 1600, 340, 12 + 900 + 1600 + 340)
    assert (a["jobsearch.outcome"], a["jobsearch.attempt"], a["jobsearch.cost_usd"]) == ("ok", 1, 0.0123)
    assert "claude" not in json.loads(a["llm.invocation_parameters"])[0]  # no executable path


def test_traces_each_attempt_of_a_retry(spans):
    def check(data):
        if not data.get("ok"):
            raise ai.InvalidAnswer("nicht ok")
    ask(FakeRunner((envelope({"ok": False}), 0, ""), (envelope({"ok": True}), 0, "")), check)
    first, second = spans.get_finished_spans()
    assert (first.attributes["jobsearch.outcome"], first.attributes["jobsearch.invalid_reason"]) == (
        "invalid", "nicht ok")
    assert (first.status.status_code, first.status.description) == (StatusCode.ERROR, "nicht ok")
    assert (second.attributes["jobsearch.attempt"], second.status.status_code) == (2, StatusCode.UNSET)


def test_traces_a_quota_error_and_still_raises(spans):
    runner = FakeRunner((envelope(is_error=True, result="Claude usage limit reached.", usage=USAGE), 1, ""))
    with pytest.raises(ai.AiError, match="Kontingent"):
        ask(runner)
    [span] = spans.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["jobsearch.outcome"] == "error"
    assert "Kontingent" in span.attributes["output.value"]


def test_traces_a_timeout(spans):
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
    with pytest.raises(ai.AiError):
        ask(runner)
    [span] = spans.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["jobsearch.outcome"] == "timeout"
    assert "llm.token_count.prompt" not in span.attributes
