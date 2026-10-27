"""Attaching Postings to Jobs, with matching across Sources and Reopenings.

A new Posting is first recognized by its source ID. Otherwise plain rules look
for a candidate: an earlier Job in reach with the same normalized company and a
similar title. Then:

- candidate seen within REOPENING_DAYS: the Posting becomes a provisional Job
  with `match_candidate` set, and the next scoring call decides "same Job?".
  Same means it is merged into the candidate before any Digest shows it.
- candidate absent for REOPENING_DAYS or more: a new Job with `reopening_of`.
  Only for candidates the daily radius query covers, since only their last_seen
  is reliable. Remote Jobs are seen only inside the nationwide date window, so
  for them the AI always decides.

Generic jobs are never candidates. Ineligible Postings are not matched at all,
since a duplicate that nobody sees costs nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from .companies import _UMLAUTS, normalize_company
from .eligibility import Decision
from .sources import Location, Posting, safe_url

SIMILAR_TITLE = 0.5  # Jaccard overlap of title tokens

_GENDER = re.compile(
    r"\(\s*(?:[mwdfx]\s*[/|,]\s*){1,3}[mwdfx]\s*\)|\b(?:[mwdfx]/){2}[mwdfx]\b|\(?\ball\s+genders?\b\)?"
    r"|:in(?:nen)?\b|\*in(?:nen)?\b|/-?in(?:nen)?\b|\(in\)",
    re.IGNORECASE,
)
_TITLE_NOISE = {"und", "oder", "fuer", "mit", "im", "in", "als", "der", "die", "das", "for", "and", "the", "of",
                "m", "w", "d", "f", "x", "mwd", "wmd"}


@dataclass(frozen=True)
class Placement:
    """Where a Posting sits relative to home."""
    distance_km: float | None
    nearest: Location | None
    is_target_company: bool


@dataclass(frozen=True)
class MatchRules:
    reopening_days: int
    radius_query_km: int  # the daily radius query sees everything online within this distance


@dataclass(frozen=True)
class Known:
    id: int
    title: frozenset[str]
    last_seen: str
    distance_km: float | None


def title_tokens(title: str) -> frozenset[str]:
    cleaned = _GENDER.sub(" ", title.lower()).translate(_UMLAUTS)
    return frozenset(t for t in re.findall(r"[a-z0-9]+", cleaned) if t not in _TITLE_NOISE)


def title_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class JobIndex:
    """Jobs that can be match candidates, by normalized company."""

    def __init__(self) -> None:
        self._by_company: dict[tuple[str, ...], list[Known]] = {}

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> JobIndex:
        index = cls()
        for row in conn.execute(
            "SELECT id, company, title, last_seen, distance_km FROM jobs WHERE eligibility != 'ineligible' AND is_generic = 0"
        ):
            index.add(row["id"], row["company"], row["title"], row["last_seen"], row["distance_km"])
        return index

    def add(self, job_id: int, company: str, title: str, last_seen: str, distance_km: float | None) -> None:
        key = normalize_company(company)
        if key:
            self._by_company.setdefault(key, []).append(Known(job_id, title_tokens(title), last_seen, distance_km))

    def candidate(self, company: str, title: str) -> Known | None:
        """The most similar earlier Job of the same company, the most recently seen on a tie."""
        tokens = title_tokens(title)
        scored = [(title_similarity(tokens, k.title), k.last_seen, k) for k in self._by_company.get(normalize_company(company), [])]
        scored = [s for s in scored if s[0] >= SIMILAR_TITLE]
        return max(scored, key=lambda s: (s[0], s[1]))[2] if scored else None


def _absent_days(last_seen: str, today: str) -> int:
    return (date.fromisoformat(today) - date.fromisoformat(last_seen)).days


def _covered(distance_km: float | None, rules: MatchRules) -> bool:
    return distance_km is not None and distance_km <= rules.radius_query_km


def _insert_job(conn, posting: Posting, placement: Placement, decision: Decision, today: str,
                reopening_of: int | None, match_candidate: int | None) -> int:
    nearest = placement.nearest
    return conn.execute(
        """INSERT INTO jobs (company, title, location, lat, lon, distance_km, is_target_company,
                             eligibility, ineligible_reason, first_seen, last_seen, reopening_of, match_candidate)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            posting.company, posting.title,
            nearest.label if nearest else posting.location_raw or None,
            nearest.lat if nearest else None, nearest.lon if nearest else None,
            None if placement.distance_km is None else round(placement.distance_km, 1),
            int(placement.is_target_company),
            decision.eligibility, decision.reason, today, today, reopening_of, match_candidate,
        ),
    ).lastrowid


def _insert_posting(conn, job_id: int, posting: Posting, today: str) -> None:
    conn.execute(
        """INSERT INTO postings (job_id, source, source_id, url, title, company, location_raw,
                                 description, is_snippet, raw_json, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job_id, posting.source, posting.source_id, posting.url, posting.title, posting.company,
            posting.location_raw or None, posting.description, int(posting.is_snippet),
            json.dumps(posting.raw, ensure_ascii=False) if posting.raw else None, today, today,
        ),
    )


def add_posting(conn: sqlite3.Connection, job_id: int, posting: Posting, today: str) -> None:
    """Store a Posting as one more Posting of a known Job, without matching. The caller has made sure
    the Posting is not in the database yet."""
    posting.url = safe_url(posting.url)  # third-party links become href in the dashboard and the Digest
    _insert_posting(conn, job_id, posting, today)
    conn.execute("UPDATE jobs SET last_seen = ? WHERE id = ? AND last_seen < ?", (today, job_id, today))


def attach(conn: sqlite3.Connection, posting: Posting, placement: Placement, decision: Decision, today: str,
           index: JobIndex, rules: MatchRules) -> tuple[int, str | None]:
    """Store or refresh a Posting. Returns (job_id, what happened): None for a known Posting,
    'new', 'pending' (waits for the AI's same-Job decision) or 'reopening'."""
    row = conn.execute(
        """SELECT p.id, p.job_id, j.last_seen, j.distance_km FROM postings p LEFT JOIN jobs j ON j.id = p.job_id
           WHERE p.source = ? AND p.source_id = ?""",
        (posting.source, posting.source_id),
    ).fetchone()
    if row:
        conn.execute("UPDATE postings SET last_seen = ? WHERE id = ?", (today, row["id"]))
        if row["job_id"] is None:
            return row["job_id"], None
        if _absent_days(row["last_seen"], today) >= rules.reopening_days and _covered(row["distance_km"], rules):
            # The same ad is back after a real absence: a new hiring round, so a new Job.
            job_id = _insert_job(conn, posting, placement, decision, today, row["job_id"], None)
            conn.execute("UPDATE postings SET job_id = ? WHERE id = ?", (job_id, row["id"]))
            return job_id, "reopening"
        conn.execute("UPDATE jobs SET last_seen = ? WHERE id = ? AND last_seen < ?", (today, row["job_id"], today))
        return row["job_id"], None

    reopening_of = match_candidate = None
    outcome = "new"
    candidate = index.candidate(posting.company, posting.title) if decision.eligibility != "ineligible" else None
    if candidate:
        if _absent_days(candidate.last_seen, today) >= rules.reopening_days and _covered(candidate.distance_km, rules):
            reopening_of, outcome = candidate.id, "reopening"
        else:
            match_candidate, outcome = candidate.id, "pending"

    job_id = _insert_job(conn, posting, placement, decision, today, reopening_of, match_candidate)
    _insert_posting(conn, job_id, posting, today)
    if decision.eligibility != "ineligible":
        index.add(job_id, posting.company, posting.title, today, placement.distance_km)
    return job_id, outcome


def merge(conn: sqlite3.Connection, job_id: int, into: int) -> None:
    """The AI found job_id to be the same opening as `into`: move everything over and drop job_id."""
    conn.execute("UPDATE postings SET job_id = ? WHERE job_id = ?", (into, job_id))
    conn.execute(
        """UPDATE jobs SET last_seen = max(last_seen, (SELECT last_seen FROM jobs WHERE id = ?)) WHERE id = ?""",
        (job_id, into),
    )
    conn.execute("UPDATE jobs SET match_candidate = ? WHERE match_candidate = ?", (into, job_id))
    conn.execute("UPDATE jobs SET reopening_of = ? WHERE reopening_of = ?", (into, job_id))
    conn.execute("DELETE FROM fit_scores WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM feedback_notes WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
