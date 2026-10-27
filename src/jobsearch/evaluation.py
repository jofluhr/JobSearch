"""The eval: a read-only look at what the runs stored. No AI call, and it works retroactively.

- Changes (`jobsearch eval --changes`): each Daily run stores a snapshot of what was in effect,
  and the dates where the snapshot differs are the split points for comparisons.
- Stage metrics (`jobsearch eval`): what each stage let through, per period. A period holds the
  Jobs and Postings first seen in it, so `--split DATE` compares the weeks before and after a change.
- Fit score against Job status (`jobsearch eval --scores`): the candidate's own decisions as labels,
  per tier, optionally grouped by model, Profile, prompt version or month.
- Found elsewhere (`jobsearch eval --funnel`): openings the candidate found outside the tool, and the
  stage where the pipeline lost each one. This is the only part that sees what was never collected.
- AI usage (`jobsearch eval --usage`): tokens and API equivalent of the claude -p calls, per day,
  purpose and model, and the API equivalent per scored Job, per good Job and per career page.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import db, usage
from .companies import normalize_company
from .config import ConfigError
from .eligibility import NOT_FULLY_REMOTE_REASON
from .matching import SIMILAR_TITLE, title_similarity, title_tokens
from .scoring import AI_NOT_REMOTE_REASON
from .usage import Usage

LABELS = {
    "model_scoring": "Modell Bewertung",
    "model_extraction": "Modell Karriereseiten",
    "prompt_scoring": "Prompt Bewertung",
    "prompt_career_extract": "Prompt Karriereseiten",
    "prompt_search_terms": "Prompt Suchbegriffe",
    "prompt_weekly_report": "Prompt Wochenbericht",
    "profile": "Profil",
    "search_terms": "Suchbegriffe",
    "exclusions": "Ausschlussliste",
    "target_companies": "Zielunternehmen",
    "alert_mail": "Alert-Mail",
    "radius_km": "Radius (km)",
    "radius_target_company_km": "Radius Zielunternehmen (km)",
    "remote_max_window_days": "Zeitfenster Remote (Tage)",
    "reopening_days": "Reopening nach (Tagen)",
    "max_scored_per_run": "Bewertungen pro Lauf",
    "feedback_notes_in_prompt": "Feedback notes im Prompt",
}
# Stored as hashes, so only "changed" can be said about them.
HASHED = {"prompt_scoring", "prompt_career_extract", "prompt_search_terms", "prompt_weekly_report", "profile",
          "exclusions", "target_companies"}
# Job status values that mean the candidate wanted the Job. Rejected is the company's decision.
POSITIVE = ("interesting", "applied", "rejected")
SOURCES = {"bundesagentur": "Bundesagentur", "indeed": "Alert-Mail", "career_page": "Karriereseiten"}
STARS = {3: "★★★", 2: "★★", 1: "★"}


@dataclass(frozen=True)
class Period:
    label: str
    since: str | None = None  # first day, inclusive (YYYY-MM-DD)
    until: str | None = None  # day after the last one, exclusive

    def where(self, column: str) -> tuple[str, list[str]]:
        """A SQL condition on a date or timestamp column, and its parameters."""
        parts, params = [], []
        if self.since:
            parts.append(f"substr({column}, 1, 10) >= ?")
            params.append(self.since)
        if self.until:
            parts.append(f"substr({column}, 1, 10) < ?")
            params.append(self.until)
        return " AND ".join(parts) or "1", params


def periods(since: str | None, until: str | None, split: str | None) -> list[Period]:
    if not split:
        return [Period("gesamt", since, until)]
    if (since and split <= since) or (until and split >= until):
        raise ValueError(f"--split {split} liegt nicht im Zeitraum")
    return [Period(f"vor {split}", since, split), Period(f"ab {split}", split, until)]


# --- Changes -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    day: str  # of the first Daily run with the new settings
    lines: list[str]


def snapshots(conn: sqlite3.Connection, period: Period = Period("gesamt")) -> list[tuple[str, dict]]:
    cond, params = period.where("started_at")
    rows = conn.execute(f"""SELECT started_at, settings_json FROM runs
                            WHERE kind = 'daily' AND settings_json IS NOT NULL AND {cond} ORDER BY id""", params)
    return [(r["started_at"], json.loads(r["settings_json"])) for r in rows]


def _value(value) -> str:
    if isinstance(value, bool):
        return "an" if value else "aus"
    return "–" if value is None else str(value)


def describe(old: dict, new: dict) -> list[str]:
    """What differs between two snapshots, one German line per setting."""
    lines = []
    for key in dict.fromkeys([*new, *old]):
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        label = LABELS.get(key, key)
        if isinstance(before, list) or isinstance(after, list):
            before, after = before or [], after or []
            parts = [f"+ {t}" for t in after if t not in before] + [f"− {t}" for t in before if t not in after]
            lines.append(f"{label}: {', '.join(parts)}")
        elif key in HASHED:
            state = "fehlt" if after is None else "vorhanden" if before is None else "geändert"
            lines.append(f"{label}: {state}")
        else:
            lines.append(f"{label}: {_value(before)} → {_value(after)}")
    return lines


def changes(snaps: list[tuple[str, dict]]) -> list[Change]:
    result = []
    previous = None
    for started_at, snap in snaps:
        if previous is None:
            result.append(Change(started_at[:10], ["Beginn der Aufzeichnung"]))
        else:
            lines = describe(previous, snap)
            if lines:
                result.append(Change(started_at[:10], lines))
        previous = snap
    return result


def unmarked_scores(conn: sqlite3.Connection) -> int:
    """Fit scores from before prompt versions were stored."""
    return conn.execute("SELECT count(*) FROM fit_scores WHERE prompt_hash IS NULL").fetchone()[0]


def changes_report(conn: sqlite3.Connection) -> list[str]:
    found = changes(snapshots(conn))
    if not found:
        lines = ["Noch keine Daily runs mit gespeicherten Einstellungen. Die Aufzeichnung beginnt mit dem nächsten."]
    else:
        lines = ["Änderungen der Einstellungen (je erster Daily run mit den neuen Werten):"]
        for change in found:
            lines.append(f"  {change.day}  {change.lines[0]}")
            lines.extend(f"              {line}" for line in change.lines[1:])
    unmarked = unmarked_scores(conn)
    if unmarked:
        lines.append(f"Fit scores ohne Prompt-Version: {unmarked}. Sie stammen von vor der Aufzeichnung, "
                     "ihre Version zeigt 'git log' von scoring.md im Vergleich mit dem Bewertungsdatum.")
    return lines


# --- Stage metrics -------------------------------------------------------------------------------

# A rescored Job has several Fit scores; the metrics count it once, by its newest.
NEWEST_SCORE = f"LEFT JOIN fit_scores s ON s.id = {db.NEWEST_SCORE_ID}"

@dataclass
class StageMetrics:
    daily_runs: int = 0
    postings: dict[str, int] = field(default_factory=dict)  # new Postings per Source
    jobs: int = 0
    eligibility: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)  # ineligible Jobs, by kind of reason
    generic: int = 0
    tiers: Counter = field(default_factory=Counter)
    low_confidence: int = 0
    sorted_out: int = 0  # ★ Jobs the AI sorted out
    unscored: int = 0  # eligible, not scored yet
    indeed_jobs: int = 0  # Jobs with an alert mail Posting
    indeed_full_text_lookup: int = 0  # ... with a full text the full-text lookup found
    indeed_full_text_other: int = 0  # ... with a full text found otherwise, by a Search term or a Career scan

    @property
    def scored(self) -> int:
        return sum(self.tiers.values())


_SHORT_REASONS = {NOT_FULLY_REMOTE_REASON: "nicht voll remote (Regel)", AI_NOT_REMOTE_REASON: "nicht voll remote (KI)"}


def reason_kind(reason: str | None) -> str:
    """'Entfernung 55 km' and 'Ausschlussliste: Praktikum' become 'Entfernung' and 'Ausschlussliste'."""
    if reason in _SHORT_REASONS:
        return _SHORT_REASONS[reason]
    return re.sub(r"\s+\d+\s*km$", "", (reason or "ohne Grund").split(":")[0]).strip()


def stage_metrics(conn: sqlite3.Connection, period: Period) -> StageMetrics:
    m = StageMetrics()
    cond, params = period.where("started_at")
    m.daily_runs = conn.execute(f"SELECT count(*) FROM runs WHERE kind = 'daily' AND ok = 1 AND {cond}",
                                params).fetchone()[0]
    cond, params = period.where("first_seen")
    m.postings = dict(conn.execute(f"SELECT source, count(*) FROM postings WHERE {cond} GROUP BY source",
                                   params).fetchall())
    cond, params = period.where("j.first_seen")
    rows = conn.execute(
        f"""SELECT j.eligibility, j.ineligible_reason, j.is_generic, s.tier, s.low_confidence, s.sorted_out
            FROM jobs j {NEWEST_SCORE} WHERE {cond}""", params).fetchall()
    for r in rows:
        m.jobs += 1
        m.eligibility[r["eligibility"]] += 1
        if r["eligibility"] == "ineligible":
            m.reasons[reason_kind(r["ineligible_reason"])] += 1
        m.generic += r["is_generic"]
        if r["tier"] is not None:
            m.tiers[r["tier"]] += 1
            m.low_confidence += r["low_confidence"]
            m.sorted_out += r["sorted_out"]
        elif r["eligibility"] == "eligible":
            m.unscored += 1
    for r in conn.execute(
        f"""SELECT max(p.id IS NOT NULL) AS full_text,
                   max(coalesce(json_extract(p.raw_json, '$.found_by'), '') = 'lookup') AS lookup
            FROM jobs j LEFT JOIN postings p ON p.job_id = j.id AND p.is_snippet = 0 AND p.description IS NOT NULL
            WHERE {cond} AND EXISTS (SELECT 1 FROM postings i WHERE i.job_id = j.id AND i.source = 'indeed')
            GROUP BY j.id""", params):
        m.indeed_jobs += 1
        m.indeed_full_text_lookup += r["lookup"]
        m.indeed_full_text_other += r["full_text"] and not r["lookup"]
    return m


@dataclass
class TermMetrics:
    term: str
    jobs: int = 0
    only_here: int = 0  # Jobs no other Search term found
    eligible: int = 0
    tiers: Counter = field(default_factory=Counter)
    positive: int = 0


def term_metrics(conn: sqlite3.Connection, period: Period) -> list[TermMetrics]:
    """Jobs per Search term, from the Bundesagentur Postings each term found. Terms that were
    active but found nothing are listed too, since that is a result as well."""
    cond, params = period.where("p.first_seen")
    rows = conn.execute(
        f"""SELECT DISTINCT pt.term, j.id, j.eligibility, j.status, s.tier
            FROM posting_terms pt JOIN postings p ON p.id = pt.posting_id JOIN jobs j ON j.id = p.job_id
            {NEWEST_SCORE} WHERE {cond}""", params).fetchall()
    terms_per_job = Counter(r["id"] for r in rows)
    stats: dict[str, TermMetrics] = {}
    for _, snap in snapshots(conn, period):
        for term in snap.get("search_terms", []):
            stats.setdefault(term, TermMetrics(term))
    for r in rows:
        t = stats.setdefault(r["term"], TermMetrics(r["term"]))
        t.jobs += 1
        t.only_here += terms_per_job[r["id"]] == 1
        t.eligible += r["eligibility"] == "eligible"
        if r["tier"] is not None:
            t.tiers[r["tier"]] += 1
        t.positive += r["status"] in POSITIVE
    return sorted(stats.values(), key=lambda t: (-t.jobs, t.term.casefold()))


def _share(n: int, total: int) -> str:
    return f"{n} ({round(100 * n / total)} %)" if total and n else str(n)


def table(header: list[str], rows: list[tuple[str, list[str]]]) -> list[str]:
    width = max(len(label) for label, _ in rows) + 2
    cols = [max(len(h), *(len(values[i]) for _, values in rows)) for i, h in enumerate(header)]

    def line(label: str, values: list[str]) -> str:
        return (label.ljust(width) + "  ".join(v.rjust(c) for v, c in zip(values, cols))).rstrip()

    return [line("", header)] + [line(label, values) for label, values in rows]


def stage_report(conn: sqlite3.Connection, chosen: list[Period]) -> list[str]:
    ms = [stage_metrics(conn, p) for p in chosen]

    def row(label: str, value) -> tuple[str, list[str]]:
        return label, [value(m) for m in ms]

    rows = [row("Daily runs (erfolgreich)", lambda m: str(m.daily_runs)), row("Neue Postings", lambda m: "")]
    rows += [row(f"  {label}", lambda m, s=source: str(m.postings.get(s, 0))) for source, label in SOURCES.items()]
    rows.append(row("Neue Jobs", lambda m: str(m.jobs)))
    rows += [row(f"  {state}", lambda m, s=state: _share(m.eligibility[s], m.jobs))
             for state in ("eligible", "unclear", "ineligible")]
    kinds = sorted({k for m in ms for k in m.reasons}, key=lambda k: -sum(m.reasons[k] for m in ms))
    if kinds:
        rows.append(row("Ineligible nach Grund", lambda m: ""))
        rows += [row(f"  {kind}", lambda m, k=kind: _share(m.reasons[k], m.eligibility["ineligible"]))
                 for kind in kinds]
    rows.append(row("Bewertet", lambda m: str(m.scored)))
    rows += [row(f"  {STARS[tier]}", lambda m, t=tier: _share(m.tiers[t], m.scored)) for tier in (3, 2, 1)]
    rows.append(row("    davon aussortiert", lambda m: _share(m.sorted_out, m.tiers[1])))
    rows.append(row("  geringe Sicherheit", lambda m: _share(m.low_confidence, m.scored)))
    rows.append(row("  Sammelanzeigen", lambda m: _share(m.generic, m.scored)))
    rows.append(row("Eligible, noch unbewertet", lambda m: str(m.unscored)))
    rows.append(row("Indeed-Jobs", lambda m: str(m.indeed_jobs)))
    rows.append(row("  Volltext per Lookup", lambda m: _share(m.indeed_full_text_lookup, m.indeed_jobs)))
    rows.append(row("  Volltext anderweitig", lambda m: _share(m.indeed_full_text_other, m.indeed_jobs)))
    lines = ["Kennzahlen je Stufe (Jobs und Postings nach dem Tag, an dem sie zuerst gesehen wurden):"]
    lines += table([p.label for p in chosen], rows)
    for period in chosen:
        terms = term_metrics(conn, period)
        if not terms:
            continue
        lines += ["", f"Suchbegriffe ({period.label}, nur Bundesagentur; ein Job zählt bei jedem Begriff, "
                      "der ihn fand):"]
        lines += table(["Jobs", "nur hier", "eligible", "★★★", "★★", "★", "positiv"],
                        [(t.term, [str(t.jobs), str(t.only_here), str(t.eligible), str(t.tiers[3]),
                                        str(t.tiers[2]), str(t.tiers[1]), str(t.positive)]) for t in terms])
    return lines


# --- Fit score against Job status ----------------------------------------------------------------

GROUPS = ("model", "profile", "prompt", "month")
FEW_LABELS = 30  # below this, a difference of one or two Jobs is noise


def label(status: str) -> str | None:
    """The candidate's decision as a label: positive, negative, or None while it is still open."""
    if status in POSITIVE:
        return "positive"
    return "negative" if status == "not_interesting" else None


def _group(row: sqlite3.Row, by: str | None) -> str:
    if by == "model":
        return row["model"]
    if by == "profile":
        return f"Profil {row['profile_hash'][:8]}"
    if by == "prompt":
        return f"Prompt {row['prompt_hash'][:8]}" if row["prompt_hash"] else "Prompt vor der Aufzeichnung"
    if by == "month":
        return row["scored_at"][:7]
    return "alle"


def score_tables(conn: sqlite3.Connection, period: Period, by: str | None = None) -> dict[str, Counter]:
    """Per group, a count per (tier, label) for the scored Jobs first seen in the period. Without `by`
    each Job counts once, by its newest Fit score. With `by` every Fit score counts in its group, so
    grouping by prompt compares the old and the new judgment of a rescored Job."""
    cond, params = period.where("j.first_seen")
    scores = "fit_scores s JOIN jobs j ON j.id = s.job_id" if by else f"jobs j JOIN fit_scores s ON s.id = {db.NEWEST_SCORE_ID}"
    rows = conn.execute(
        f"""SELECT s.tier, j.status, s.model, s.profile_hash, s.prompt_hash, s.scored_at
            FROM {scores} WHERE {cond} ORDER BY s.scored_at""", params)
    tables: dict[str, Counter] = {}
    for r in rows:
        tables.setdefault(_group(r, by), Counter())[(r["tier"], label(r["status"]))] += 1
    return tables


def _percent(n: int, total: int) -> str:
    return f"{round(100 * n / total)} % ({n} von {total})" if total else "–"


def score_report(conn: sqlite3.Connection, chosen: list[Period], by: str | None = None) -> list[str]:
    lines = []
    for period in chosen:
        for group, t in score_tables(conn, period, by).items():
            title = ", ".join(x for x in (period.label, group if by else None) if x)
            rows = [(STARS[tier], [str(t[(tier, "positive")]), str(t[(tier, "negative")]), str(t[(tier, None)])])
                    for tier in (3, 2, 1)]
            positives = sum(t[(tier, "positive")] for tier in STARS)
            labeled = positives + sum(t[(tier, "negative")] for tier in STARS)
            if lines:
                lines.append("")
            lines.append(f"Fit score gegen Job status ({title}):")
            lines += table(["positiv", "negativ", "ohne Label"], rows)
            top_labeled = t[(3, "positive")] + t[(3, "negative")]
            lines.append(f"Treffsicherheit ★★★: {_percent(t[(3, 'positive')], top_labeled)}")
            lines.append(f"Übersehen in ★: {t[(1, 'positive')]} von {positives} positiven")
            per_tier = []
            for tier in (3, 2, 1):
                in_tier = t[(tier, "positive")] + t[(tier, "negative")]
                per_tier.append(f"{STARS[tier]} {_percent(in_tier, in_tier + t[(tier, None)])}")
            lines.append("Mit Label je Tier: " + ", ".join(per_tier))
            lines.append(f"Labels: {labeled} von {sum(t.values())} bewerteten Jobs")
            if labeled < FEW_LABELS:
                lines.append("Wenige Labels: Unterschiede von ein, zwei Jobs sind hier Zufall.")
    if not lines:
        lines = ["Im Zeitraum gibt es keine bewerteten Jobs."]
    return lines


# --- Found elsewhere -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Found:
    """An opening the candidate found outside the tool, from private/found_elsewhere.csv."""
    company: str
    title: str
    url: str = ""
    found_on: str = ""  # YYYY-MM-DD, optional


# Stage: label and the lever to pull when openings get lost there.
STAGES = {
    "before": ("vor Beginn der Sammlung", ""),
    "unknown_company": ("nicht gesammelt, Firma nie gesehen", "Sources, Zielunternehmen"),
    "not_collected": ("nicht gesammelt, Firma bekannt", "Suchbegriffe"),
    "ineligible": ("ineligible", "Radius, Ausschlussliste, Remote-Erkennung"),
    "waiting": ("unclear oder unbewertet", "Rückstau, fehlende Beschreibung"),
    "sorted_out": ("aussortiert", "Prompt, Profil, Feedback notes"),
    "tier1": ("★", "Prompt, Profil, Feedback notes"),
    "tier2": ("★★", ""),
    "tier3": ("★★★", ""),
}


@dataclass(frozen=True)
class Trace:
    found: Found
    stage: str
    detail: str = ""


def read_found(path: Path) -> list[Found]:
    if not path.exists():
        raise ConfigError(f"{path} fehlt. 'jobsearch init' legt die Datei an.")
    with path.open(encoding="utf-8-sig", newline="") as f:  # a spreadsheet may have added a BOM
        rows = [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]
    return [Found(r.get("company", ""), r.get("title", ""), r.get("url", ""), r.get("found_on", ""))
            for r in rows if r.get("title") or r.get("url")]


_POSTINGS = f"""SELECT p.company, p.title AS posting_title, j.eligibility, j.ineligible_reason, j.status, s.tier,
                       s.sorted_out
                FROM postings p JOIN jobs j ON j.id = p.job_id {NEWEST_SCORE}"""


def _same_company(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Either name's tokens all appear in the other, so 'Example Tech' finds 'Example Tech Services AG'."""
    return bool(a and b) and (set(a) <= set(b) or set(b) <= set(a))


def trace(conn: sqlite3.Connection, found: Found, collecting_since: str | None) -> Trace:
    """Where the pipeline stands with an opening found elsewhere."""
    from .dashboard import STATUS_LABELS  # not at the top, so the CLI starts without loading Flask

    row = None
    if found.url:
        row = conn.execute(f"{_POSTINGS} WHERE rtrim(p.url, '/') = ?", (found.url.rstrip("/"),)).fetchone()
    company_seen = row is not None
    if row is None:
        company, title = normalize_company(found.company), title_tokens(found.title)
        best = 0.0
        for r in conn.execute(_POSTINGS):
            if not _same_company(company, normalize_company(r["company"])):
                continue
            company_seen = True
            similarity = title_similarity(title, title_tokens(r["posting_title"]))
            if similarity >= SIMILAR_TITLE and similarity > best:
                best, row = similarity, r
    if row is None:
        if found.found_on and collecting_since and found.found_on < collecting_since:
            return Trace(found, "before")
        return Trace(found, "not_collected" if company_seen else "unknown_company")
    details = []
    if row["posting_title"] != found.title:
        details.append(f"als '{row['posting_title']}'")
    if row["status"] != "new":
        details.append(f"Status {STATUS_LABELS.get(row['status'], row['status'])}")
    if row["eligibility"] == "ineligible":
        stage = "ineligible"
        details.insert(0, reason_kind(row["ineligible_reason"]))
    elif row["sorted_out"]:
        stage = "sorted_out"
    elif row["tier"] is not None:
        stage = f"tier{row['tier']}"
    else:
        stage = "waiting"
    return Trace(found, stage, ", ".join(details))


def funnel_report(conn: sqlite3.Connection, path: Path) -> list[str]:
    found = read_found(path)
    if not found:
        return [f"Noch keine Fremdfunde in {path}. Trage Stellen ein, die du außerhalb des Tools findest."]
    since = conn.execute("SELECT min(substr(started_at, 1, 10)) FROM runs WHERE kind = 'daily'").fetchone()[0]
    traces = [trace(conn, f, since) for f in found]
    per_stage = Counter(t.stage for t in traces)
    width = max(len(name) for name, _ in STAGES.values()) + 4
    lines = [f"Fremdfunde: {len(found)} (aus {path.name})"]
    for stage, (name, lever) in STAGES.items():
        n = per_stage[stage]
        lines.append(f"  {name.ljust(width)}{n:>3}" + (f"  → {lever}" if n and lever else ""))
    lines += ["", "Einzeln:"]
    for t in traces:
        what = " – ".join(x for x in (t.found.company, t.found.title or t.found.url) if x)
        lines.append(f"  {what}: {STAGES[t.stage][0]}" + (f" ({t.detail})" if t.detail else ""))
    return lines


# --- AI usage ------------------------------------------------------------------------------------

@dataclass
class UsageMetrics:
    total: Usage = field(default_factory=Usage)
    by_day: dict[str, Usage] = field(default_factory=dict)
    by_purpose: dict[str, Usage] = field(default_factory=dict)
    by_model: dict[str, Usage] = field(default_factory=dict)
    recording_since: str | None = None  # first day with a recorded call; nothing before it is counted
    scored: int = 0  # Fit scores since then
    good: int = 0  # ★★★ and ★★ among them
    pages: int = 0  # career pages sent to the AI since then
    retries: Counter = field(default_factory=Counter)  # second attempts per purpose
    # Fixed overhead ("Grundlast"): cache tokens read and written per call, per purpose. The system
    # prompt, schema and Profile come with every call, so it shows what batching saves.
    cache_per_call: dict[str, float] = field(default_factory=dict)

    @property
    def per_scored(self) -> float | None:
        return _per(self.by_purpose.get("scoring", Usage()).cost_usd, self.scored)

    @property
    def per_good(self) -> float | None:
        return _per(self.total.cost_usd, self.good)

    @property
    def per_page(self) -> float | None:
        return _per(self.by_purpose.get("career_extract", Usage()).cost_usd, self.pages)


def _per(cost: float, n: int) -> float | None:
    return cost / n if n else None


def usage_metrics(conn: sqlite3.Connection, period: Period) -> UsageMetrics:
    m = UsageMetrics()
    first = conn.execute("SELECT min(started_at) FROM ai_calls").fetchone()[0]
    if not first:
        return m
    m.recording_since = first[:10]
    # Scores and scans from before the first recorded call would look free, so they are left out,
    # to the second: the day recording began usually had a run before it. A scan counts by its end,
    # since its run starts before its AI call.
    cond, params = period.where("scored_at")
    m.scored, m.good = conn.execute(
        # A Job's first Fit score only: later ones come from rescoring calls, not from scoring calls.
        f"""SELECT count(*), coalesce(sum(tier >= 2), 0) FROM fit_scores f WHERE {cond} AND scored_at >= ?
            AND f.id = (SELECT min(id) FROM fit_scores WHERE job_id = f.job_id)""",
        [*params, first]).fetchone()
    cond, params = period.where("started_at")
    m.pages = sum(json.loads(r["counts_json"] or "{}").get("extracted", 0) for r in conn.execute(
        f"""SELECT counts_json FROM runs WHERE kind = 'career_scan' AND {cond}
            AND coalesce(finished_at, started_at) >= ?""", [*params, first]))
    cond, params = period.where("started_at")
    cache: dict[str, list[int]] = {}
    for row in conn.execute(f"SELECT * FROM ai_calls WHERE {cond} ORDER BY id", params):
        m.total.add(row)
        for groups, key in ((m.by_day, row["started_at"][:10]), (m.by_purpose, row["purpose"]),
                            (m.by_model, row["model"])):
            groups.setdefault(key, Usage()).add(row)
        m.retries[row["purpose"]] += row["attempt"] == 2
        if row["cache_read_tokens"] is not None or row["cache_creation_tokens"] is not None:  # not a timeout
            cache.setdefault(row["purpose"], []).append((row["cache_read_tokens"] or 0)
                                                        + (row["cache_creation_tokens"] or 0))
    m.cache_per_call = {purpose: sum(v) / len(v) for purpose, v in cache.items()}
    return m


def day_usage(conn: sqlite3.Connection, day: str) -> Usage:
    """The AI usage of one day (YYYY-MM-DD), for the Digest."""
    return usage_metrics(conn, Period(day, day, (date.fromisoformat(day) + timedelta(days=1)).isoformat())).total


def usage_report(conn: sqlite3.Connection, chosen: list[Period]) -> list[str]:
    ms = [usage_metrics(conn, p) for p in chosen]
    if not ms[0].recording_since:
        return ["Noch keine KI-Aufrufe aufgezeichnet."]
    lines = [f"KI-Verbrauch, API-Gegenwert (Listenpreis) in Dollar, gezählt ab {ms[0].recording_since}.",
             "Das ist kein echter Preis (die Aufrufe laufen über das Abo) und kein Anteil am Kontingent:",
             "dessen Grenze ist nicht in Tokens bekannt, und die interaktive Nutzung fehlt hier."]
    rows = [("Aufrufe", [str(m.total.calls) for m in ms]),
            ("API-Gegenwert", [usage.money(m.total.cost_usd) for m in ms]),
            ("  pro bewertetem Job", [usage.money(m.per_scored) for m in ms]),
            ("  pro ★★★/★★-Job (alle Zwecke)", [usage.money(m.per_good) for m in ms]),
            ("  pro ausgelesener Karriereseite", [usage.money(m.per_page) for m in ms])]
    lines += [""] + table([p.label for p in chosen], rows)
    for period, m in zip(chosen, ms):
        if not m.total.calls:
            continue
        lines += ["", f"Nach Zweck ({period.label}; Grundlast = Cache-Tokens pro Aufruf):"]
        lines += table(usage.HEADER + ["Wiederholt", "Grundlast"],
                        [(usage.PURPOSES.get(p, p), usage.values(u) + [_share(m.retries[p], u.calls),
                                                                      usage.tokens(m.cache_per_call.get(p, 0))])
                         for p, u in sorted(m.by_purpose.items(), key=lambda kv: -kv[1].cost_usd)])
        lines += ["", f"Nach Modell ({period.label}):"]
        lines += table(usage.HEADER, [(model, usage.values(u)) for model, u in sorted(m.by_model.items())])
        lines += ["", f"Nach Tag ({period.label}):"]
        lines += table(usage.HEADER, [(day, usage.values(u)) for day, u in sorted(m.by_day.items())])
    return lines
