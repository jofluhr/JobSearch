# Issue: Trace runs and AI calls in a local Phoenix

Status: open. Implement on branch `implement-tracing`.

## Goal

Today a run can be followed only through the log file, the `ai_calls` table and `jobsearch eval --usage`. They show counts, tokens and cost, but neither the order and duration of the steps nor what an AI call actually got and answered. Prompt and answer text are deliberately not stored (see `docs/issues/ai-usage.md`).

This issue adds optional tracing with OpenTelemetry, viewed in a local [Phoenix](https://github.com/Arize-ai/phoenix) (Arize Phoenix, open source). A Daily run becomes one trace: a tree of steps with their durations, and each `claude -p` call is a step that shows its system prompt, prompt, structured answer, model, tokens and outcome.

Tracing is off by default. It makes no AI calls of its own and adds no migration.

## Decisions

- **Spans are written by hand, not by an auto-instrumentor.** Phoenix's instrumentors hook into SDKs (Anthropic, OpenAI, LangChain). This project never imports one; `ai.ask()` runs `claude -p` as a subprocess. The AI call spans use the OpenInference attribute names, so Phoenix shows them as LLM spans with prompt, answer and tokens.
- **Local only.** The prompts contain the Profile and Third-party text. `TRACING_ENDPOINT` must point to a local address (`localhost`, `127.0.0.1`, `::1`). Any other host is refused with a German error at startup, so a typo or a hosted Phoenix can never receive personal data. Phoenix keeps its data in its own folder outside the repo, so nothing reaches git.
- **Off unless configured.** With `TRACING_ENDPOINT` empty, nothing is imported from OpenTelemetry and every span helper is a no-op. Runs, tests and the scheduled task behave exactly as today.
- **Tracing never costs a run.** An unreachable Phoenix, a missing package or an export error is logged at WARNING at most and never fails or noticeably slows a run. Export runs in the background with a short timeout. Spans are flushed at the end of the command with a bounded wait, so the trace of a scheduled `pythonw` run is complete when Phoenix is running.
- **Phoenix is not a dependency of this project.** The candidate runs Phoenix separately (for example `pip install arize-phoenix` in its own venv, then `phoenix serve`; the UI and the OTLP endpoint are on port 6006). This project only needs the OpenTelemetry SDK and the OTLP/HTTP exporter, as an optional extra `trace`.
- **No Phoenix features that call a model.** Phoenix has a playground and evals that call LLM APIs with an API key. They are not used and not documented here; the no-API-costs rule stands.
- **The `ai_calls` table stays the record of AI usage.** Tracing is for looking at single runs and calls, not for metrics. Nothing reads traces back.

## Facts to build on

- `ai.ask()` in `src/jobsearch/ai.py` is the only place that runs `claude -p`. Each attempt is one process. It already reads the JSON envelope and builds a `CallUsage` via `usage.from_envelope()` with tokens, cost, duration and outcome (`ok`, `invalid`, `error`, `timeout`).
- `cli.main()` builds the `Config` and sets up logging (`_setup_logging`) before running a command. `cmd_run` is the scheduled entry point under `pythonw`, without a console.
- A Daily run goes `cmd_daily` → `terms_for_run` → `daily.run_daily` (collect, store, alert mails, Indeed full text lookup, missing descriptions, remote Jobs) → `career_scan` when due → `cmd_score` → `weekly_report`.
- `config.load()` reads settings from the environment; `.env.example` holds the dummy values. `TRACING_ENDPOINT` is not personal, so it does not go into `guard.ENV_KEYS`.

## Part 1: Tracing module and AI call spans

- `pyproject.toml`: optional extra `trace = ["opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-http"]` with minimum versions. The `dev` extra gets the same packages so the tests run.
- `config.py` and `.env.example`: `TRACING_ENDPOINT=` (empty), with a comment naming the Phoenix default `http://localhost:6006/v1/traces`.
- New module `src/jobsearch/tracing.py`, the only module that imports OpenTelemetry:
  - `setup(cfg, command: str)`: does nothing when the endpoint is empty. Otherwise checks the host is local (`ConfigError` in German if not), imports the SDK lazily (a German hint to install the `trace` extra if missing), and sets a tracer provider with service name `jobsearch`, a batch processor and the OTLP/HTTP exporter with a short timeout.
  - `span(name, **attributes)`: a context manager that yields the span, or a no-op object with the same methods when tracing is off. An exception inside marks the span as error and is re-raised.
  - `shutdown()`: flushes with a bounded wait; does nothing when off.
- `cli.main()` calls `setup()` after logging and `shutdown()` in a `finally`. The command name becomes the root span (`jobsearch daily`, `jobsearch run`, ...).
- `ai.ask()`: one span `claude -p <purpose>` per attempt, with:
  - `openinference.span.kind` = `LLM`, `llm.model_name` (the model that answered, else the requested one), `llm.invocation_parameters` (the CLI flags without the prompt);
  - `llm.input_messages`: the system prompt (role `system`) and the prompt (role `user`); `input.value` = the prompt;
  - `output.value` and `output.mime_type` = `application/json`: the structured answer, or the CLI's error text;
  - `llm.token_count.prompt`, `.completion`, `.total` from the `CallUsage` (input including cache tokens, so the totals match Phoenix's view of a call);
  - `jobsearch.purpose`, `jobsearch.attempt`, `jobsearch.outcome`, `jobsearch.cost_usd`, `jobsearch.duration_ms`, `jobsearch.num_turns`, and the reason when the answer was `invalid`;
  - span status error for `error`, `invalid` and `timeout`.
- Tests (`tests/test_tracing.py`, `tests/test_ai.py`), with OpenTelemetry's in-memory exporter, no network:
  - off: `span()` is a no-op, nothing is imported from OpenTelemetry, `ask()` works as today;
  - a non-local endpoint is refused at setup;
  - a successful call yields one LLM span with prompt, system prompt, answer, model and tokens;
  - an invalid first answer yields two spans, the first with status error and the reason;
  - a timeout and an error envelope yield an error span; a quota error still raises `AiError` after the span ends;
  - an exception inside `span()` marks the span and propagates.

## Part 2: Spans for the run's steps

- Spans around the steps of a Daily run, nested under the command's root span:
  - `cmd_daily`: `search terms`, `daily run`, `career scan`, `scoring`, `weekly report`;
  - `run_daily`: `collect`, `store postings`, `alert mails`, `full text lookup`, `missing descriptions`, `settle remote jobs`;
  - each span carries the counts it changed (from `Counts`, `ScanCounts` and the scoring result) as `jobsearch.*` attributes, and the run id where there is one.
- The other commands that call the AI (`score`, `rescore`, `career-setup`, `career-scan`, `report`, `market`, `check-injection`) get their root span from Part 1; inner spans only where a step takes noticeable time.
- No Third-party text in step attributes: titles, companies and URLs stay out. Only the AI call spans carry text, since that is what they are for.
- Tests: a Daily run with fake client and fake `ask` produces the expected tree of span names, the AI call spans sit under `scoring`, and a step that raises ends its span with status error.

## Docs

- `README.md`: a short section "Tracing (optional)": install the `trace` extra, start Phoenix in its own venv, set `TRACING_ENDPOINT`, open `http://localhost:6006`. State that traces contain the Profile and job ads and stay on the local machine, and that a scheduled run is only traced while Phoenix runs.
- `CONTEXT.md`: **Trace**: the recorded steps of one command, with durations and the AI calls' prompts and answers, viewed in a local Phoenix. Not a record of AI usage; that is the `ai_calls` table.

## Done when

On the live setup with Phoenix running locally and `TRACING_ENDPOINT` set: `jobsearch daily` produces one trace in Phoenix that shows the steps with their durations, and the scoring call opens to show its system prompt, prompt, answer, model and tokens, with the same tokens as its row in `ai_calls`. With Phoenix stopped, the same command finishes normally and takes at most a few seconds longer. With `TRACING_ENDPOINT` empty, nothing changes. Findings go into `docs/implementation-plan.md`.
