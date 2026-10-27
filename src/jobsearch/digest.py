"""The Digest: a German Markdown summary per day in data/digests/YYYY-MM-DD.md.

It lists the Jobs scored that day by Fit score, the Reopenings found that day,
and how many Jobs still wait for scoring. Sorted out Jobs are only counted.
Jobs rescored that day are left out, since they were scored before. A second run on the same day rewrites
the file, so it always shows the whole day.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import db, usage
from .evaluation import day_usage
from .scoring import ScoreCounts, backlog

TIER_TITLES = {3: "★★★ Sehr passend", 2: "★★ Teilweise passend", 1: "★ Wenig passend"}
REMOTE_LABELS = {"full": "voll remote", "hybrid": "hybrid", "none": "vor Ort"}


@dataclass
class DayReport:
    day: date
    jobs: dict[int, list[dict]] = field(default_factory=lambda: {3: [], 2: [], 1: []})
    reopenings: list[dict] = field(default_factory=list)
    backlog: int = 0
    sorted_out: int = 0
    ai_usage: usage.Usage = field(default_factory=usage.Usage)

    @property
    def new_good(self) -> dict[int, int]:
        return {tier: len(self.jobs[tier]) for tier in (3, 2)}


def collect_day(conn: sqlite3.Connection, day: date) -> DayReport:
    report = DayReport(day, backlog=backlog(conn))
    rows = conn.execute(
        f"""SELECT j.id, j.title, j.company, j.location, j.distance_km, j.is_target_company, j.remote_type, j.is_generic,
                  s.tier, s.reasoning, s.matching_skills, s.missing_skills, s.part_time_signal, s.low_confidence,
                  s.sorted_out, (SELECT p.url FROM postings p WHERE p.job_id = j.id AND p.url IS NOT NULL
                   ORDER BY p.id LIMIT 1) AS url
           FROM fit_scores s JOIN jobs j ON j.id = s.job_id
           WHERE s.scored_at LIKE ? AND j.eligibility = 'eligible'
             AND s.id = {db.NEWEST_SCORE_ID}
             AND NOT EXISTS (SELECT 1 FROM fit_scores o WHERE o.job_id = j.id AND o.scored_at < ?)
           ORDER BY s.tier DESC, j.is_target_company DESC, j.distance_km""",
        (f"{day.isoformat()}%", day.isoformat()),
    ).fetchall()
    for row in rows:
        if row["sorted_out"]:
            report.sorted_out += 1
        else:
            report.jobs[row["tier"]].append(dict(row))
    report.reopenings = [dict(r) for r in conn.execute(
        """SELECT j.title, j.company, old.first_seen AS earlier FROM jobs j JOIN jobs old ON old.id = j.reopening_of
           WHERE j.first_seen = ? ORDER BY j.id""",
        (day.isoformat(),),
    )]
    report.ai_usage = day_usage(conn, day.isoformat())
    return report


def _link(job: dict) -> str:
    title = job["title"].replace("[", "(").replace("]", ")")
    return f"[{title}]({job['url']})" if job["url"] else title


def _facts(job: dict) -> str:
    facts = [job["location"] or "Ort unbekannt"]
    if job["distance_km"] is not None:
        facts.append(f"{job['distance_km']:.0f} km")
    if job["remote_type"] in REMOTE_LABELS:
        facts.append(REMOTE_LABELS[job["remote_type"]])
    if job["part_time_signal"] == "yes":
        facts.append("Teilzeit möglich")
    if job["is_target_company"]:
        facts.append("Zielunternehmen")
    if job["is_generic"]:
        facts.append("Sammelanzeige")
    if job["low_confidence"]:
        facts.append("geringe Evidenz (keine volle Beschreibung)")
    return " · ".join(facts)


def render(report: DayReport, scoring: ScoreCounts | None = None, warnings: list[str] = ()) -> str:
    total = sum(len(v) for v in report.jobs.values()) + report.sorted_out
    lines = [f"# Jobs vom {report.day:%d.%m.%Y}", ""]
    lines.append(f"{total} neu bewertete Jobs in Reichweite. Warten auf Bewertung: {report.backlog}.")
    if report.ai_usage.calls:
        lines += ["", f"KI-Verbrauch heute: {usage.summary(report.ai_usage)}"]
    if scoring and scoring.error:
        lines += ["", f"> ⚠️ Die Bewertung ist fehlgeschlagen: {scoring.error}"]
    if scoring and scoring.injection_suspected:
        lines += ["", f"> ⚠️ {scoring.injection_suspected} Stellenanzeige(n) mit möglichen Anweisungen an die KI, "
                      "trotzdem normal bewertet. Job-Nummern im Log."]
    for warning in warnings:
        lines += ["", f"> ⚠️ {warning}"]
    for tier in (3, 2):
        jobs = report.jobs[tier]
        lines += ["", f"## {TIER_TITLES[tier]} ({len(jobs)})"]
        if not jobs:
            lines += ["", "Keine."]
        for job in jobs:
            lines += ["", f"### {_link(job)} – {job['company']}", "", _facts(job), "", job["reasoning"]]
            matching, missing = json.loads(job["matching_skills"] or "[]"), json.loads(job["missing_skills"] or "[]")
            if matching:
                lines.append(f"\n**Passt:** {', '.join(matching)}")
            if missing:
                lines.append(f"\n**Fehlt:** {', '.join(missing)}")
    weak = report.jobs[1]
    lines += ["", f"## {TIER_TITLES[1]} ({len(weak)})", ""]
    lines += [f"- {_link(job)} – {job['company']}" for job in weak] or ["Keine."]
    if report.sorted_out:
        lines += ["", f"Von der KI aussortiert: {report.sorted_out}"]
    if report.reopenings:
        lines += ["", f"## Wieder ausgeschrieben ({len(report.reopenings)})", ""]
        lines += [f"- {r['title']} – {r['company']} (zuerst gesehen am {r['earlier']})" for r in report.reopenings]
    return "\n".join(lines) + "\n"


def write_digest(conn: sqlite3.Connection, digests_dir: Path, day: date,
                 scoring: ScoreCounts | None = None, warnings: list[str] = ()) -> tuple[Path, DayReport]:
    report = collect_day(conn, day)
    digests_dir.mkdir(parents=True, exist_ok=True)
    path = digests_dir / f"{day.isoformat()}.md"
    path.write_text(render(report, scoring, warnings), encoding="utf-8")
    return path, report
