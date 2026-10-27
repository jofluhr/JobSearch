"""The Weekly report: the pipeline's own metrics, read by the AI, turned into Recommendations.

This module builds what the AI sees. Every number is a Metric with a stable ID and the count it
rests on, so the AI can cite IDs instead of writing numbers, and the report fills in the values.
The input holds aggregates only, plus the candidate's own Search terms and rule-made reasons:
no text from job ads, so this call has no Third-party text.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path

from . import ai, config, db, prompts
from .config import Config, ConfigError
from .evaluation import (SOURCES, STAGES, STARS, Change, Period, Trace, changes, read_found, score_tables, snapshots,
                         stage_metrics, term_metrics, trace)
from .search_terms import active_terms, derived_terms

log = logging.getLogger(__name__)

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold().translate(_UMLAUTS)).strip("-") or "x"


@dataclass(frozen=True)
class Metric:
    id: str
    value: int
    n: int  # the count the value rests on
    label: str  # German, for the report


@dataclass
class ReportInput:
    metrics: list[Metric] = field(default_factory=list)
    terms: dict[str, str] = field(default_factory=dict)  # slug in the IDs -> the Search term
    changes: list[Change] = field(default_factory=list)  # settings changes since the last report
    history: list[dict] = field(default_factory=list)  # earlier Recommendations and what became of them

    def term_slug(self, term: str) -> str:
        """A stable slug per term; terms that slug alike get -2, -3, ..."""
        for key, known in self.terms.items():
            if known == term:
                return key
        base = key = slug(term)
        number = 1
        while key in self.terms:
            number += 1
            key = f"{base}-{number}"
        self.terms[key] = term
        return key

    def add(self, id: str, value: int, n: int, label: str, keep_empty: bool = False) -> None:
        """A metric resting on nothing says nothing and only costs tokens, so it is left out."""
        if n or keep_empty:
            self.metrics.append(Metric(id, value, n, label))

    def to_prompt_json(self) -> str:
        return json.dumps({"metrics": [m.__dict__ for m in self.metrics],
                           "changes": [{"day": c.day, "lines": c.lines} for c in self.changes],
                           "history": self.history},
                          ensure_ascii=False, separators=(",", ":"))


def _periods(today: date) -> list[tuple[str, str, Period]]:
    """Suffix of the IDs, German label, and the period: the last 7 days and the whole history."""
    week = Period("7 Tage", (today - timedelta(days=6)).isoformat(), (today + timedelta(days=1)).isoformat())
    return [("7d", "letzte 7 Tage", week), ("all", "gesamt", Period("gesamt"))]


def _stages(inp: ReportInput, conn: sqlite3.Connection, key: str, name: str, period: Period) -> None:
    s = stage_metrics(conn, period)
    for source, label in SOURCES.items():
        n = s.postings.get(source, 0)
        inp.add(f"stage.postings.{source}.{key}", n, n, f"Neue Postings {label} ({name})")
    inp.add(f"stage.jobs.{key}", s.jobs, s.jobs, f"Neue Jobs ({name})")
    for state in ("eligible", "unclear", "ineligible"):
        inp.add(f"stage.{state}.{key}", s.eligibility[state], s.jobs, f"Jobs {state} ({name})")
    for reason, n in sorted(s.reasons.items()):
        inp.add(f"stage.ineligible.{slug(reason)}.{key}", n, s.eligibility["ineligible"],
                f"Ineligible wegen {reason} ({name})")
    inp.add(f"stage.generic.{key}", s.generic, s.jobs, f"Sammelanzeigen ({name})")
    for tier in (3, 2, 1):
        inp.add(f"stage.tier{tier}.{key}", s.tiers[tier], s.scored, f"Bewertet mit {STARS[tier]} ({name})")
    inp.add(f"stage.low_confidence.{key}", s.low_confidence, s.scored, f"Geringe Evidenz ({name})")
    inp.add(f"stage.sorted_out.{key}", s.sorted_out, s.scored, f"Von der KI aussortiert ({name})")
    inp.add(f"stage.unscored.{key}", s.unscored, s.eligibility["eligible"], f"Eligible, noch unbewertet ({name})")


def _terms(inp: ReportInput, conn: sqlite3.Connection, key: str, name: str, period: Period,
           active: set[str] | None) -> None:
    """Only active terms: a term that is no longer searched cannot be switched off, and its history
    would only cost tokens. None takes every term, for the before/after of a switched-off one."""
    for t in term_metrics(conn, period):
        if active is not None and t.term.casefold() not in active:
            continue
        prefix, label = f"term.{inp.term_slug(t.term)}", f"Suchbegriff '{t.term}'"
        inp.add(f"{prefix}.jobs.{key}", t.jobs, t.jobs, f"{label}: Jobs ({name})", keep_empty=True)
        inp.add(f"{prefix}.only_here.{key}", t.only_here, t.jobs, f"{label}: nur von diesem Begriff gefunden ({name})")
        inp.add(f"{prefix}.eligible.{key}", t.eligible, t.jobs, f"{label}: eligible ({name})")
        scored = sum(t.tiers.values())
        for tier in (3, 2, 1):
            inp.add(f"{prefix}.tier{tier}.{key}", t.tiers[tier], scored, f"{label}: {STARS[tier]} ({name})")
        inp.add(f"{prefix}.positive.{key}", t.positive, t.jobs, f"{label}: positiver Job status ({name})")


_LABELS = {"positive": "positiv", "negative": "negativ", None: "ohne Label"}


def _scores(inp: ReportInput, conn: sqlite3.Connection, key: str, name: str, period: Period) -> None:
    t = score_tables(conn, period).get("alle")
    if not t:
        return
    for tier in (3, 2, 1):
        in_tier = sum(t[(tier, lab)] for lab in _LABELS)
        for lab, german in _LABELS.items():
            inp.add(f"score.tier{tier}.{lab or 'unlabeled'}.{key}", t[(tier, lab)], in_tier,
                    f"{STARS[tier]}, Job status {german} ({name})")
    top_labeled = t[(3, "positive")] + t[(3, "negative")]
    if top_labeled:
        inp.add(f"score.tier3.precision.{key}", round(100 * t[(3, "positive")] / top_labeled), top_labeled,
                f"Treffsicherheit ★★★ in Prozent ({name})")
    positives = sum(t[(tier, "positive")] for tier in (3, 2, 1))
    inp.add(f"score.missed_in_tier1.{key}", t[(1, "positive")], positives,
            f"Positive Jobs, die nur ★ bekamen ({name})")
    inp.add(f"score.labeled.{key}", positives + sum(t[(tier, "negative")] for tier in (3, 2, 1)),
            sum(t.values()), f"Bewertete Jobs mit Label ({name})")


def _traces(conn: sqlite3.Connection, found_path: Path) -> list[Trace]:
    try:
        found = read_found(found_path)
    except ConfigError:  # the file is optional
        return []
    since = conn.execute("SELECT min(substr(started_at, 1, 10)) FROM runs WHERE kind = 'daily'").fetchone()[0]
    return [trace(conn, f, since) for f in found]


def _funnel(inp: ReportInput, traces: list[Trace], key: str, name: str, period: Period) -> None:
    """Openings found elsewhere, per stage. For the week, those whose found_on lies in it."""
    if period.since:
        traces = [t for t in traces if t.found.found_on and period.since <= t.found.found_on < period.until]
    for stage, n in Counter(t.stage for t in traces).items():
        inp.add(f"funnel.{stage}.{key}", n, len(traces), f"Fremdfunde: {STAGES[stage][0]} ({name})")


def _collect(inp: ReportInput, conn: sqlite3.Connection, key: str, name: str, period: Period,
             active: set[str] | None, traces: list[Trace]) -> None:
    """Every metric for one period, with `key` as the IDs' last part."""
    _stages(inp, conn, key, name, period)
    _terms(inp, conn, key, name, period, active)
    _scores(inp, conn, key, name, period)
    _funnel(inp, traces, key, name, period)


def report_input(conn: sqlite3.Connection, today: date, found_path: Path, last_report_at: str | None,
                 active_terms: list[str]) -> ReportInput:
    inp = ReportInput()
    active = {t.casefold() for t in active_terms}
    since = last_report_at[:10] if last_report_at else ""
    inp.changes = [c for c in changes(snapshots(conn)) if c.day >= since]
    traces = _traces(conn, found_path)
    for key, name, period in reversed(_periods(today)):  # the whole history first, so slugs follow its order
        _collect(inp, conn, key, name, period, active, traces)
    _history(inp, conn, today, traces)
    return inp


def build_input(conn: sqlite3.Connection, cfg: Config, today: date) -> ReportInput:
    """The input as the next report would get it."""
    terms, _ = term_and_exclusion_lists(conn, cfg)
    return report_input(conn, today, cfg.found_elsewhere_path, last_period_end(conn), terms)


HISTORY_REPORTS = 4  # earlier reports whose Recommendations go into the next input
EFFECT_AFTER_DAYS = 7  # an accepted Recommendation gets before/after metrics once it is this old
EFFECT_UNTIL_DAYS = 56  # and keeps them for about two months


def _history(inp: ReportInput, conn: sqlite3.Connection, today: date, traces: list[Trace]) -> None:
    """Earlier Recommendations with their decision, so rejected ones are not repeated. Accepted ones
    old enough also get their evidence metrics before and after the decision, over two windows of the
    same length, so the AI can judge whether the change worked."""
    recent = [r[0] for r in conn.execute(
        "SELECT id FROM reports WHERE answer_json IS NOT NULL ORDER BY id DESC LIMIT ?", (HISTORY_REPORTS,))]
    rows = conn.execute(
        f"""SELECT r.*, p.period_end FROM recommendations r JOIN reports p ON p.id = r.report_id
            WHERE r.report_id IN ({",".join("?" * len(recent))})
               OR (r.decision = 'accepted' AND r.kind = 'change' AND r.decided_at >= ?)
            ORDER BY r.report_id, r.kind, r.position""",
        [*recent, (today - timedelta(days=EFFECT_UNTIL_DAYS)).isoformat()]).fetchall()
    for r in rows:
        inp.history.append({"id": r["id"], "report": r["period_end"], "kind": r["kind"], "lever": r["lever"],
                            "target": r["target"], "action": r["action"], "decision": r["decision"],
                            "decided_at": (r["decided_at"] or "")[:10]})
        if r["decision"] == "accepted" and r["kind"] == "change":
            _effect(inp, conn, today, r, traces)


def _effect(inp: ReportInput, conn: sqlite3.Connection, today: date, rec: sqlite3.Row,
            traces: list[Trace]) -> None:
    decided = date.fromisoformat(rec["decided_at"][:10])
    if not EFFECT_AFTER_DAYS <= (today - decided).days <= EFFECT_UNTIL_DAYS:
        return
    days = (today - decided).days + 1  # the after window runs from the decision day through today
    families = {e.rsplit(".", 1)[0] for e in json.loads(rec["evidence_json"])}
    windows = (("before", "vor der Änderung", Period("vorher", (decided - timedelta(days=days)).isoformat(),
                                                     decided.isoformat())),
               ("after", "nach der Änderung", Period("nachher", decided.isoformat(),
                                                     (decided + timedelta(days=days)).isoformat())))
    for key, name, period in windows:
        window = ReportInput(terms=dict(inp.terms))  # same slugs as the main input
        _collect(window, conn, key, name, period, None, traces)
        for m in window.metrics:
            if m.id.rsplit(".", 1)[0] in families:
                inp.add(f"rec.{rec['id']}.{m.id}", m.value, m.n, f"Empfehlung {rec['id']}: {m.label}",
                        keep_empty=True)


# --- The answer ----------------------------------------------------------------------------------

# Settings a Recommendation can change. The schema's enum is built from this list.
LEVERS = ("search_term_off", "search_term_new", "exclusion", "radius", "remote_phrase", "scoring_prompt",
          "profile", "career_url", "label_more")
LEVER_LABELS = {"search_term_off": "Suchbegriff abschalten", "search_term_new": "Suchbegriff ergänzen",
                "exclusion": "Ausschlussliste ergänzen", "radius": "Radius ändern",
                "remote_phrase": "Remote-Erkennung anpassen", "scoring_prompt": "Bewertungs-Prompt anpassen",
                "profile": "Profil ergänzen", "career_url": "Karriereseite prüfen",
                "label_more": "Mehr Jobs mit Status versehen"}
CONFIDENCE = ("niedrig", "mittel", "hoch")
MAX_RECOMMENDATIONS = 5
MAX_NEW_LEVERS = 2
MIN_EVIDENCE_N = 5  # a Recommendation needs at least one metric resting on this many


@dataclass
class CheckedAnswer:
    summary: str
    summary_evidence: list[str]
    recommendations: list[dict] = field(default_factory=list)
    new_levers: list[dict] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # why, one line per dropped item


# "1.200" and "1.200,5" are German thousands; "3,5" and "3.5" decimals.
_NUMBER = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?")
DROPPED_SUMMARY = "Die Gesamteinschätzung wurde verworfen, weil sie Zahlen nannte, die nicht in den Kennzahlen stehen."


# Names, not statistics: a tier ("Stufe 3", "Tier 2", "3 Sterne") and the week ("7 Tage").
_NAMES = re.compile(r"\b(?:Stufe|Tier)\s+[1-3]\b|\b[1-3]\s+Stern(?:e|en)?\b|\b7\s+Tage?n?\b", re.IGNORECASE)


def _numbers(text: str) -> list[tuple[str, float]]:
    found = []
    for match in _NUMBER.findall(_NAMES.sub(" ", text)):
        plain = match.replace(".", "") if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", match) else match
        found.append((match, float(plain.replace(",", "."))))
    return found


def _problem(texts: list[str], evidence: list[str], metrics: dict[str, Metric], target: str = "",
             min_n: int = MIN_EVIDENCE_N) -> str | None:
    """Why an item is not grounded in the input, or None. Numbers in its text must be values of its
    own evidence, or part of its target."""
    unknown = [e for e in evidence if e not in metrics]
    if unknown:
        return f"unbekannte Kennzahl {', '.join(unknown)}"
    if not evidence:
        return "keine Kennzahl als Beleg"
    if all(metrics[e].n < min_n for e in evidence):
        return f"Belege beruhen auf zu wenigen Jobs (n unter {min_n})"
    allowed = {metrics[e].value for e in evidence} | {value for _, value in _numbers(target)}
    stray = [text for t in texts for text, value in _numbers(t) if value not in allowed]
    if stray:
        return f"Zahl ohne Beleg: {', '.join(stray)}"
    return None


def _target(item: dict, inp: ReportInput, active_terms: list[str], exclusions: list[str]) -> tuple[str, str | None]:
    """The target as it will be applied, and why it does not fit its Lever (or None)."""
    lever, target = item["lever"], item["target"].strip()
    active = {t.casefold(): t for t in active_terms}
    if lever == "search_term_off":
        term = active.get(target.casefold()) or active.get(inp.terms.get(target, "").casefold())
        return (term, None) if term else (target, f"'{target}' ist kein aktiver Suchbegriff")
    if lever == "search_term_new":
        if not target or target.casefold() in active:
            return target, f"'{target}' ist schon ein aktiver Suchbegriff" if target else "kein Suchbegriff genannt"
    if lever == "exclusion":
        if not target or target.casefold() in {e.casefold() for e in exclusions}:
            return target, f"'{target}' steht schon auf der Ausschlussliste" if target else "kein Wort genannt"
    return target, None


def check_answer(data: dict, inp: ReportInput, active_terms: list[str], exclusions: list[str]) -> CheckedAnswer:
    """Keep what is grounded in the input, drop the rest with its reason. Never raises."""
    metrics = {m.id: m for m in inp.metrics}
    result = CheckedAnswer(data["summary"], data["summary_evidence"])
    problem = _problem([data["summary"]], data["summary_evidence"], metrics, min_n=0)
    if problem:
        result.dropped.append(f"Gesamteinschätzung: {problem}")
        result.summary, result.summary_evidence = DROPPED_SUMMARY, []
    for item in data["recommendations"]:
        # Too few labels is exactly when asking for more of them is right.
        min_n = 0 if item["lever"] == "label_more" else MIN_EVIDENCE_N
        target, problem = _target(item, inp, active_terms, exclusions)
        problem = problem or _problem([item["action"]], item["evidence"], metrics, target, min_n)
        if not problem and len(result.recommendations) >= MAX_RECOMMENDATIONS:
            problem = f"mehr als {MAX_RECOMMENDATIONS} Empfehlungen"
        if problem:
            result.dropped.append(f"Empfehlung '{item['action']}': {problem}")
        else:
            result.recommendations.append({**item, "target": target})
    for idea in data["new_levers"]:
        problem = _problem([idea["what"], idea["why"]], idea["evidence"], metrics)
        if not problem and len(result.new_levers) >= MAX_NEW_LEVERS:
            problem = f"mehr als {MAX_NEW_LEVERS} Ideen"
        if problem:
            result.dropped.append(f"Idee '{idea['name']}': {problem}")
        else:
            result.new_levers.append(idea)
    return result


SYSTEM_PROMPT = ("You review the metrics of a job search pipeline and answer only with the requested JSON. "
                 "You never write numbers in the text, you cite metric IDs instead.")
_EVIDENCE = {"type": "array", "items": {"type": "string"}, "minItems": 1}
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "summary_evidence": _EVIDENCE,
        "recommendations": {
            "type": "array",
            "maxItems": MAX_RECOMMENDATIONS,
            "items": {
                "type": "object",
                "properties": {
                    "lever": {"type": "string", "enum": list(LEVERS)},
                    "target": {"type": "string"},
                    "action": {"type": "string"},
                    "evidence": _EVIDENCE,
                    "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                },
                "required": ["lever", "target", "action", "evidence", "confidence"],
                "additionalProperties": False,
            },
        },
        "new_levers": {
            "type": "array",
            "maxItems": MAX_NEW_LEVERS,
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "what": {"type": "string"}, "why": {"type": "string"},
                               "evidence": _EVIDENCE},
                "required": ["name", "what", "why", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "summary_evidence", "recommendations", "new_levers"],
    "additionalProperties": False,
}


def build_prompt(inp: ReportInput) -> str:
    rubric = resources.files("jobsearch.prompts").joinpath("weekly_report.md").read_text(encoding="utf-8")
    return f"{rubric}\n# Input\n\n{inp.to_prompt_json()}\n"


# --- Making and storing the report ---------------------------------------------------------------

@dataclass
class ReportCounts:
    recommendations: int = 0
    dropped: int = 0
    new_levers: int = 0


@dataclass
class ReportResult:
    report_id: int
    counts: ReportCounts = field(default_factory=ReportCounts)
    path: Path | None = None
    error: str | None = None


def term_and_exclusion_lists(conn: sqlite3.Connection, cfg: Config) -> tuple[list[str], list[str]]:
    """The active Search terms and the exclusion list, as the Daily run sees them."""
    manual = config.read_list_file(cfg.search_terms_path) if cfg.search_terms_path.exists() else []
    exclusions = config.read_list_file(cfg.exclusions_path) if cfg.exclusions_path.exists() else []
    return active_terms(manual, derived_terms(conn)), exclusions


def last_period_end(conn: sqlite3.Connection) -> str | None:
    """The last day the newest finished report looked at. Failed or interrupted ones do not count."""
    return conn.execute("SELECT max(period_end) FROM reports WHERE answer_json IS NOT NULL").fetchone()[0]


def report_due(conn: sqlite3.Connection, today: date, interval_days: int) -> bool:
    """Due when the newest finished report looked at a week `interval_days` or more ago. A failed
    report does not count, so it is tried again at the next run."""
    last = last_period_end(conn)
    return not last or (today - date.fromisoformat(last)).days >= interval_days


def make_report(conn: sqlite3.Connection, cfg: Config, today: date, ask=ai.ask) -> ReportResult:
    """One AI call, the checks, the rows and the Markdown file. An AI failure is stored, never raised."""
    inp = build_input(conn, cfg, today)
    with conn:
        report_id = conn.execute(
            """INSERT INTO reports (created_at, period_start, period_end, prompt_hash, input_json)
               VALUES (?, ?, ?, ?, ?)""",
            (db.now(), (today - timedelta(days=6)).isoformat(), today.isoformat(),
             prompts.version("weekly_report.md"), inp.to_prompt_json()),
        ).lastrowid
    result = ReportResult(report_id)
    try:
        answer = ask(build_prompt(inp), purpose="weekly_report", model=cfg.model_scoring, schema=SCHEMA,
                     system=SYSTEM_PROMPT, usage_recorder=db.usage_recorder(conn))
    except ai.AiError as e:
        log.error("Weekly report failed: %s", e)
        result.error = str(e)
        with conn:
            conn.execute("UPDATE reports SET error = ? WHERE id = ?", (result.error, report_id))
        return result
    checked = check_answer(answer.data, inp, *term_and_exclusion_lists(conn, cfg))
    for reason in checked.dropped:
        log.info("Weekly report dropped %s", reason)
    result.counts = ReportCounts(len(checked.recommendations), len(checked.dropped), len(checked.new_levers))
    _store(conn, report_id, answer.model, checked, result.counts)
    result.path = write_report(conn, cfg.reports_dir, report_id)
    return result


def _store(conn: sqlite3.Connection, report_id: int, model: str, checked: CheckedAnswer,
           counts: ReportCounts) -> None:
    rows = [(report_id, i, "change", r["lever"], r["target"], r["action"], None, json.dumps(r["evidence"]),
             r["confidence"]) for i, r in enumerate(checked.recommendations)]
    rows += [(report_id, i, "new_lever", idea["name"], "", idea["what"], idea["why"],
              json.dumps(idea["evidence"]), None) for i, idea in enumerate(checked.new_levers)]
    answer = {"summary": checked.summary, "summary_evidence": checked.summary_evidence, "dropped": checked.dropped}
    with conn:
        conn.execute("UPDATE reports SET model = ?, answer_json = ?, counts_json = ? WHERE id = ?",
                     (model, json.dumps(answer, ensure_ascii=False), json.dumps(counts.__dict__), report_id))
        conn.executemany(
            """INSERT INTO recommendations (report_id, position, kind, lever, target, action, why, evidence_json,
                                            confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)


# --- Reading the report back ---------------------------------------------------------------------

@dataclass
class Recommendation:
    id: int
    kind: str  # change | new_lever
    lever: str  # for a new_lever: its proposed name
    target: str
    action: str  # for a new_lever: what it would change
    why: str | None
    evidence: list[Metric]
    confidence: str | None
    decision: str  # open | accepted | rejected
    decided_at: str | None


@dataclass
class Report:
    id: int
    created_at: str
    period_start: str
    period_end: str
    model: str
    summary: str
    summary_evidence: list[Metric]
    recommendations: list[Recommendation]
    dropped: list[str]
    counts: ReportCounts

    @property
    def week(self) -> tuple[int, int]:
        return week_of(self.period_end)

    @property
    def made(self) -> str:
        return _made(self.created_at)


def week_of(period_end: str) -> tuple[int, int]:
    """Year and ISO week a report is named after."""
    year, week, _ = date.fromisoformat(period_end).isocalendar()
    return year, week


def _made(created_at: str) -> str:
    return created_at[:16].replace("T", " ")


@dataclass(frozen=True)
class ReportEntry:
    id: int
    week: tuple[int, int]
    made: str


def list_reports(conn: sqlite3.Connection) -> list[ReportEntry]:
    """Every report whose AI call worked, newest first."""
    return [ReportEntry(r["id"], week_of(r["period_end"]), _made(r["created_at"])) for r in conn.execute(
        "SELECT id, period_end, created_at FROM reports WHERE answer_json IS NOT NULL ORDER BY id DESC")]


def load_report(conn: sqlite3.Connection, report_id: int) -> Report:
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    metrics = {m["id"]: Metric(**m) for m in json.loads(row["input_json"])["metrics"]}
    answer = json.loads(row["answer_json"])
    recs = [Recommendation(r["id"], r["kind"], r["lever"], r["target"], r["action"], r["why"],
                           [metrics[e] for e in json.loads(r["evidence_json"])], r["confidence"], r["decision"],
                           r["decided_at"])
            for r in conn.execute("SELECT * FROM recommendations WHERE report_id = ? ORDER BY kind, position",
                                  (report_id,))]
    return Report(row["id"], row["created_at"], row["period_start"], row["period_end"], row["model"],
                  answer["summary"], [metrics[e] for e in answer["summary_evidence"]], recs, answer["dropped"],
                  ReportCounts(**json.loads(row["counts_json"])))


def latest_report(conn: sqlite3.Connection) -> Report | None:
    """The newest report whose AI call worked."""
    entries = list_reports(conn)
    return load_report(conn, entries[0].id) if entries else None


def _evidence_table(evidence: list[Metric]) -> list[str]:
    return ["", "| Kennzahl | Wert | n |", "|---|---:|---:|"] + [f"| {m.label} | {m.value} | {m.n} |"
                                                                for m in evidence]


def render(report: Report) -> str:
    year, week = report.week
    lines = [f"# Wochenbericht KW {week}/{year}", "",
             f"Zeitraum {report.period_start} bis {report.period_end}, erstellt {report.made} "
             f"mit {report.model}.", "", "## Einschätzung", "", report.summary]
    if report.summary_evidence:
        lines += _evidence_table(report.summary_evidence)
    changes = [r for r in report.recommendations if r.kind == "change"]
    lines += ["", f"## Empfehlungen ({len(changes)})"]
    if not changes:
        lines += ["", "Keine Änderung nötig."]
    for i, r in enumerate(changes, 1):
        target = f" `{r.target}`" if r.target else ""
        lines += ["", f"### {i}. {LEVER_LABELS.get(r.lever, r.lever)}{target} (Sicherheit: {r.confidence})", "",
                  r.action]
        lines += _evidence_table(r.evidence)
    ideas = [r for r in report.recommendations if r.kind == "new_lever"]
    if ideas:
        lines += ["", "## Ideen für neue Stellschrauben"]
        for r in ideas:
            lines += ["", f"### {r.lever}", "", f"{r.action} {r.why}"]
            lines += _evidence_table(r.evidence)
    lines += ["", f"verworfen: {report.counts.dropped} (Gründe im Log)"]
    return "\n".join(lines) + "\n"


def write_report(conn: sqlite3.Connection, reports_dir: Path, report_id: int) -> Path:
    """data/reports/YYYY-Www.md; a second report in the same week replaces the file, the rows stay."""
    report = load_report(conn, report_id)
    year, week = report.week
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{year}-W{week:02d}.md"
    path.write_text(render(report), encoding="utf-8")
    return path


# --- Deciding on a Recommendation ----------------------------------------------------------------

# The only Levers applied by the tool: one line appended to a private file. Everything else the
# candidate changes by hand and only marks as done.
APPLIED = {"search_term_off": ("search_terms_path", "-{}"), "search_term_new": ("search_terms_path", "{}"),
           "exclusion": ("exclusions_path", "{}")}
DECISIONS = ("accepted", "rejected")


def applied_file(cfg: Config, lever: str) -> Path:
    """The private file an applied Lever writes to."""
    return getattr(cfg, APPLIED[lever][0])


class DecisionError(Exception):
    """The decision was not stored. The message is German, for the dashboard."""


def _append_line(path: Path, line: str) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}{line}\n", encoding="utf-8")


def decide(conn: sqlite3.Connection, cfg: Config, rec_id: int, decision: str, now: datetime) -> None:
    """Store the candidate's decision; accepting an applied Lever appends its line first. Final:
    a decided Recommendation is not decided again, the file stays editable by hand."""
    row = conn.execute("SELECT * FROM recommendations WHERE id = ?", (rec_id,)).fetchone()
    if row is None or decision not in DECISIONS:
        raise DecisionError("Diese Empfehlung gibt es nicht.")
    if row["decision"] != "open":
        raise DecisionError("Über diese Empfehlung ist schon entschieden.")
    if decision == "accepted" and row["kind"] == "change" and row["lever"] in APPLIED:
        target = row["target"].strip()
        if not target or any(c in target for c in "\r\n") or target[0] in "#-":
            raise DecisionError(f"'{target}' lässt sich nicht als Zeile eintragen. Bitte von Hand ändern.")
        _append_line(applied_file(cfg, row["lever"]), APPLIED[row["lever"]][1].format(target))
    with conn:
        conn.execute("UPDATE recommendations SET decision = ?, decided_at = ? WHERE id = ?",
                     (decision, now.isoformat(timespec="seconds"), rec_id))
