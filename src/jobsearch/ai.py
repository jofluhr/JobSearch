"""The only module that calls the AI: Claude Code headless (claude -p) on the subscription.

Every call gets a JSON schema, which the CLI enforces, so the answer arrives as
parsed JSON in `structured_output`. The caller can add its own check on top. An
answer that fails either check is asked for once more, then the call gives up.

Flags keep each call small and inert: no tools (except web search for the one-time
career page setup), no MCP servers, no skills, a
short system prompt instead of the coding one, no saved session, and a neutral
working directory so no project CLAUDE.md is loaded. --bare is not usable, since
it ignores the subscription login and accepts only an API key.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from . import tracing
from .usage import PURPOSES, CallUsage, from_envelope

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 900

_QUOTA = re.compile(r"usage limit|rate limit|limit reached|quota|too many requests|\b429\b", re.IGNORECASE)
_LOGIN = re.compile(r"log ?in|logged out|authenticat|unauthori[sz]ed|\b401\b|invalid api key", re.IGNORECASE)


class AiError(Exception):
    """The AI step failed. The message is German and meant for the user."""


class InvalidAnswer(ValueError):
    """Raised by a caller's check when an answer is well-formed JSON but unusable."""


@dataclass(frozen=True)
class Answer:
    data: dict
    model: str  # the model that actually answered, e.g. claude-sonnet-5-5


UsageRecorder = Callable[[CallUsage], None]
Runner = Callable[..., subprocess.CompletedProcess]
# Started from pythonw by Task Scheduler, a console program would otherwise open a window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def command(model: str, schema: dict, system: str, tools: tuple[str, ...] = ()) -> list[str]:
    exe = shutil.which("claude")
    if not exe:
        raise AiError("Der Befehl 'claude' wurde nicht gefunden. Ist Claude Code installiert und im PATH?")
    return [
        exe, "-p",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--system-prompt", system,
        "--tools", ",".join(tools),
        *(["--allowedTools", ",".join(tools)] if tools else []),
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]


def _failure(text: str) -> AiError:
    text = text.strip()[:500]
    if _QUOTA.search(text):
        return AiError(f"Claude-Kontingent erschöpft, die Jobs warten auf den nächsten Lauf. ({text})")
    if _LOGIN.search(text):
        return AiError(f"Claude ist nicht angemeldet. Einmal 'claude' im Terminal starten und anmelden. ({text})")
    return AiError(f"Claude-Aufruf fehlgeschlagen: {text or 'keine Ausgabe'}")


def _envelope(proc: subprocess.CompletedProcess) -> dict | None:
    try:
        envelope = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return envelope if isinstance(envelope, dict) else None


def _parse(proc: subprocess.CompletedProcess, envelope: dict | None) -> tuple[dict, str]:
    """The structured answer and the model name, or AiError for failures that a retry won't fix."""
    if envelope is None:
        raise _failure(proc.stderr or proc.stdout or f"Exit-Code {proc.returncode}")
    if proc.returncode != 0 or envelope.get("is_error"):
        raise _failure(str(envelope.get("result") or proc.stderr or envelope.get("subtype") or ""))
    models = list((envelope.get("modelUsage") or {}).keys())
    return envelope.get("structured_output"), (models[0] if models else "")


def _record(recorder: UsageRecorder | None, usage: CallUsage) -> None:
    if recorder is None:
        return
    try:
        recorder(usage)
    except Exception:  # measuring must never cost the answer
        log.warning("Could not record AI usage", exc_info=True)


# Attribute names of an AI call span follow OpenInference, so Phoenix shows prompt, answer and tokens.
def _span_input(model: str, tools: tuple[str, ...], system: str, prompt: str, purpose: str, attempt: int) -> dict:
    return {
        "openinference.span.kind": "LLM",
        "llm.provider": "anthropic",
        "llm.model_name": model,
        # The settings of the call; system prompt and prompt are in the messages, the schema is left out.
        "llm.invocation_parameters": json.dumps({"model": model, "tools": list(tools), "output_format": "json"}),
        "llm.input_messages.0.message.role": "system",
        "llm.input_messages.0.message.content": system,
        "llm.input_messages.1.message.role": "user",
        "llm.input_messages.1.message.content": prompt,
        "input.value": prompt,
        "input.mime_type": "text/plain",
        "jobsearch.purpose": purpose,
        "jobsearch.attempt": attempt,
    }


def _sum_known(*counts: int | None) -> int | None:
    """The sum of the counts that are known, or None if none is."""
    known = [n for n in counts if n is not None]
    return sum(known) if known else None


def _span_result(usage: CallUsage, output: str | None, mime_type: str) -> dict:
    # The prompt includes the cached tokens, as in the ai_calls rows and in Phoenix's view of a call.
    prompt_tokens = _sum_known(usage.input_tokens, usage.cache_read_tokens, usage.cache_creation_tokens)
    total = _sum_known(prompt_tokens, usage.output_tokens)
    attributes = {
        "llm.model_name": usage.model,
        "llm.token_count.prompt": prompt_tokens,
        "llm.token_count.prompt_details.cache_read": usage.cache_read_tokens,
        "llm.token_count.prompt_details.cache_write": usage.cache_creation_tokens,
        "llm.token_count.completion": usage.output_tokens,
        "llm.token_count.total": total,
        "jobsearch.outcome": usage.outcome,
        "jobsearch.cost_usd": usage.cost_usd,
        "jobsearch.duration_ms": usage.duration_ms,
        "jobsearch.num_turns": usage.num_turns,
    }
    if output is not None:
        attributes |= {"output.value": output, "output.mime_type": mime_type,
                       "llm.output_messages.0.message.role": "assistant",
                       "llm.output_messages.0.message.content": output}
    return attributes


def _finish(recorder: UsageRecorder | None, span: tracing.Span, usage: CallUsage, output: str | None = None,
            mime_type: str = "application/json") -> None:
    """Stores the attempt's AI usage and adds it to its span."""
    _record(recorder, usage)
    span.set(_span_result(usage, output, mime_type))


def ask(
    prompt: str,
    *,
    purpose: str,
    model: str,
    schema: dict,
    system: str,
    check: Callable[[dict], None] | None = None,
    tools: tuple[str, ...] = (),
    timeout: int = TIMEOUT_SECONDS,
    runner: Runner = subprocess.run,
    usage_recorder: UsageRecorder | None = None,
) -> Answer:
    """Send the prompt on stdin and return the checked answer. Raises AiError.

    `usage_recorder` gets the AI usage of every attempt, whatever its outcome."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    cmd = command(model, schema, system, tools)
    problem = ""
    for attempt in (1, 2):
        with tracing.span(f"claude -p {purpose}", _span_input(model, tools, system, prompt, purpose, attempt)) as span:
            started_at = datetime.now().isoformat(timespec="seconds")
            usage = dict(purpose=purpose, model=model, attempt=attempt, started_at=started_at)
            try:
                proc = runner(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, cwd=tempfile.gettempdir(), creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired:
                _finish(usage_recorder, span, from_envelope(None, outcome="timeout", **usage))
                raise AiError(f"Claude hat nach {timeout // 60} Minuten nicht geantwortet.") from None
            envelope = _envelope(proc)
            if envelope is not None:  # the raw numbers next to the stored row, for checking by hand
                log.info("AI usage (%s, attempt %d): %s", purpose, attempt, json.dumps(
                    {k: envelope.get(k) for k in ("modelUsage", "total_cost_usd", "duration_ms", "num_turns")}))
            try:
                data, used_model = _parse(proc, envelope)
            except AiError as e:
                _finish(usage_recorder, span, from_envelope(envelope, outcome="error", **usage), str(e), "text/plain")
                raise
            output = json.dumps(data, ensure_ascii=False)
            try:
                if not isinstance(data, dict):
                    raise InvalidAnswer("keine strukturierte Antwort")
                if check:
                    check(data)
            except InvalidAnswer as e:
                _finish(usage_recorder, span, from_envelope(envelope, outcome="invalid", **usage), output)
                problem = str(e)
                span.set({"jobsearch.invalid_reason": problem})
                span.fail(problem)
                log.warning("Invalid AI answer (attempt %d): %s", attempt, problem)
            else:
                _finish(usage_recorder, span, from_envelope(envelope, outcome="ok", **usage), output)
                return Answer(data, used_model or model)
    raise AiError(f"Claude hat zweimal keine brauchbare Antwort geliefert: {problem}")
