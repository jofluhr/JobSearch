"""The Daily run: collect Postings, decide Eligibility, store everything.

Then the Indeed alert mails, if IMAP is configured, and a lookup of their Jobs at the
Bundesagentur for the full text (see lookup_full_text). Two Bundesagentur queries per Search term:
- a radius query around home without a date limit. It returns every offer that is
  currently online, so last_seen stays accurate for every Job in reach.
- a nationwide query limited to recent offers, keeping only those with home office.
  The API has no home office filter, so this is the only way to find remote Jobs.
"""

from __future__ import annotations

import imaplib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests

from . import db, prompts
from .companies import TargetCompany, match_target_company, normalize_company
from .config import Config, ConfigError
from .eligibility import (HOMEOFFICE_REASON, Rules, decide, distance_km, remote_by_alert_filter,
                          settle_remote)
from .geo import Gazetteer
from .matching import (JobIndex, MatchRules, Placement, add_posting, attach, place_values, title_similarity,
                       title_tokens)
from .scoring import profile_hash
from .sources import Location, Posting, safe_url
from .sources.bundesagentur import BundesagenturClient, SchemaChanged
from .sources.indeed_mail import Mailbox, MailError, parse_alert

log = logging.getLogger(__name__)

LOOKUP_PER_RUN = 50
LOOKUP_SIMILAR_TITLE = 0.8  # stricter than matching.SIMILAR_TITLE, since a hit is attached without the AI
LOOKUP_RADIUS_KM = 30
LOOKUP_DAYS = 7  # a Job is looked up in the Daily runs of its first week, then no more


@dataclass
class Counts:
    postings_seen: int = 0
    new_jobs: int = 0
    eligible: int = 0
    unclear: int = 0
    ineligible: int = 0
    descriptions_fetched: int = 0
    description_failures: int = 0
    remote_dropped: int = 0
    match_pending: int = 0
    reopenings: int = 0
    alert_mails: int = 0
    alert_mails_processed: int = 0
    alert_mails_unread: int = 0  # no Postings found, still in the inbox
    places_unknown: int = 0  # Postings with a place name that could not be located
    lookups: int = 0  # Indeed Jobs looked up at the Bundesagentur for the full text
    lookup_hits: int = 0
    lookup_known: int = 0  # the clear hit was already stored, as a Posting of another Job
    lookup_dropped: int = 0  # lookups that found the same company, but no title and place close enough
    lookup_failures: int = 0
    errors: list[str] = field(default_factory=list)


def remote_window_days(conn: sqlite3.Connection, today: date, max_days: int) -> int:
    """Days since the last successful Daily run, so missed days are caught up."""
    row = conn.execute(
        "SELECT max(started_at) FROM runs WHERE kind = 'daily' AND ok = 1"
    ).fetchone()
    if not row[0]:
        return max_days
    last = date.fromisoformat(row[0][:10])
    return max(1, min(max_days, (today - last).days + 1))


def locate(posting: Posting, gazetteer: Gazetteer | None, counts: Counts) -> None:
    """Coordinates for locations that have only a place name, such as those from alert mails."""
    located = []
    for loc in posting.locations:
        if loc.lat is None and loc.label and gazetteer:
            point = gazetteer.locate(loc.label)
            if point:
                loc = Location(loc.label, *point)
        if loc.lat is None and loc.label:
            counts.places_unknown += 1
        located.append(loc)
    posting.locations = located


def place(posting: Posting, cfg: Config, targets: list[TargetCompany]) -> Placement:
    nearest, best = None, None
    for loc in posting.locations:
        if loc.lat is None or loc.lon is None:
            continue
        d = distance_km(cfg.home_lat, cfg.home_lon, loc.lat, loc.lon)
        if best is None or d < best:
            nearest, best = loc, d
    if best is None:
        best = posting.distance_km
        nearest = posting.locations[0] if posting.locations else None
    return Placement(best, nearest, match_target_company(posting.company, targets) is not None)


def collect(client: BundesagenturClient, cfg: Config, terms: list[str], window: int,
            counts: Counts) -> tuple[dict[str, Posting], dict[str, set[str]]]:
    """Postings by source ID, and the Search terms that found each of them."""
    found: dict[str, Posting] = {}
    found_by: dict[str, set[str]] = {}

    def keep(p: Posting, term: str) -> None:
        found.setdefault(p.source_id, p)
        found_by.setdefault(p.source_id, set()).add(term)

    for term in terms:
        try:
            for p in client.search(term, location=cfg.home_location, radius_km=cfg.radius_target_company_km):
                keep(p, term)
            for p in client.search(term, published_within_days=window):
                if p.homeoffice_possible:
                    keep(p, term)
        except (requests.RequestException, SchemaChanged, ValueError) as e:
            log.error("Bundesagentur search for %r failed: %s", term, e)
            counts.errors.append(f"Suche '{term}': {e}")
    return found, found_by


def record_terms(conn: sqlite3.Connection, found_by: dict[str, set[str]]) -> None:
    conn.executemany(
        """INSERT OR IGNORE INTO posting_terms (posting_id, term)
           SELECT id, ? FROM postings WHERE source = 'bundesagentur' AND source_id = ?""",
        [(term, source_id) for source_id, terms in found_by.items() for term in terms],
    )


def fetch_missing_descriptions(conn: sqlite3.Connection, client: BundesagenturClient, counts: Counts) -> None:
    """Full text for Postings of Jobs in reach. Ineligible Jobs are skipped to save requests."""
    rows = conn.execute(
        """SELECT p.id, p.source_id FROM postings p JOIN jobs j ON j.id = p.job_id
           WHERE p.source = 'bundesagentur' AND p.description IS NULL
             AND j.eligibility IN ('eligible', 'unclear')"""
    ).fetchall()
    for row in rows:
        try:
            text = client.description(row["source_id"])
        except requests.RequestException as e:
            log.warning("Description for %s failed: %s", row["source_id"], e)
            counts.description_failures += 1
            continue
        if text is None:
            # External offers (partner portals) have no description on the page.
            counts.description_failures += 1
            continue
        with conn:
            conn.execute("UPDATE postings SET description = ? WHERE id = ?", (text, row["id"]))
        counts.descriptions_fetched += 1


def lookup_kind(job: sqlite3.Row) -> str:
    """'remote' for a home office Job, also once it is kept on the alert's remote filter, 'unknown place'
    for a Job without coordinates, else 'place'."""
    if (job["ineligible_reason"] or "").startswith(HOMEOFFICE_REASON):
        return "remote"
    if job["lat"] is None or job["lon"] is None:
        return "unknown place"
    return "place"


def _hit_distance(job: sqlite3.Row, hit: Posting) -> float | None:
    """Distance from the Job's place to the hit's nearest place, None when one of them is unknown."""
    if job["lat"] is None or job["lon"] is None:
        return None
    return min((distance_km(job["lat"], job["lon"], loc.lat, loc.lon)
                for loc in hit.locations if loc.lat is not None and loc.lon is not None), default=None)


def clear_hit(job: sqlite3.Row, hits: list[Posting]) -> tuple[Posting | None, bool]:
    """The best hit of the same company with a very similar title and a fitting place, and whether hits
    of the same company were dropped without a clear one. A remote Job needs a hit with home office, a Job
    with a place a hit near it. A hit or Job without coordinates passes the place check and ranks as near."""
    key, tokens, kind = normalize_company(job["company"]), title_tokens(job["title"]), lookup_kind(job)
    clear, dropped = [], False
    for hit in hits:
        if normalize_company(hit.company) != key:
            continue
        similarity = title_similarity(tokens, title_tokens(hit.title))
        distance = _hit_distance(job, hit)
        if kind == "remote":
            place_fits = hit.homeoffice_possible
        else:
            place_fits = distance is None or distance <= LOOKUP_RADIUS_KM
        if similarity < LOOKUP_SIMILAR_TITLE or not place_fits:
            dropped = True
            continue
        clear.append((similarity, -(distance or 0.0), hit))
    best = max(clear, key=lambda c: (c[0], c[1]))[2] if clear else None
    return best, dropped and best is None


def _search_hits(client: BundesagenturClient, job: sqlite3.Row) -> list[Posting]:
    kind = lookup_kind(job)
    if kind == "remote":
        return client.lookup(job["title"])  # nationwide, the hit must offer home office
    if kind == "unknown place":
        return client.lookup(job["title"], location=job["location"])  # a region or nothing usable
    return client.lookup(job["title"], location=job["location"], radius_km=LOOKUP_RADIUS_KM)


def _relocate(conn: sqlite3.Connection, job: sqlite3.Row, hit: Posting, cfg: Config,
              targets: list[TargetCompany], rules: Rules) -> None:
    """A Job whose place was unknown takes the hit's place, and its Eligibility is decided again."""
    placement = place(hit, cfg, targets)
    decision = decide(rules, job["title"], placement.distance_km, placement.is_target_company,
                      hit.homeoffice_possible)
    conn.execute(
        """UPDATE jobs SET location = ?, lat = ?, lon = ?, distance_km = ?, is_target_company = ?,
                           eligibility = ?, ineligible_reason = ? WHERE id = ?""",
        (*place_values(hit, placement), decision.eligibility, decision.reason, job["id"]),
    )


def lookup_full_text(conn: sqlite3.Connection, client: BundesagenturClient, cfg: Config,
                     targets: list[TargetCompany], rules: Rules, today: date, counts: Counts) -> None:
    """Snippet-only Indeed Jobs, still unscored: look for the same opening at the Bundesagentur and attach a
    clear hit as one more Posting, so its description is fetched and the Job is scored on the full text.
    A Job with an unknown place takes the hit's place. Target companies are left to the Career scan.
    Only Jobs first seen within LOOKUP_DAYS, oldest first, at most LOOKUP_PER_RUN per run: a Job without
    a hit is tried again for a few days, and Jobs that are never scored do not crowd out new ones."""
    rows = conn.execute(
        """SELECT j.id, j.title, j.company, j.location, j.lat, j.lon, j.ineligible_reason FROM jobs j
           WHERE j.eligibility IN ('eligible', 'unclear') AND j.is_target_company = 0
             AND EXISTS (SELECT 1 FROM postings p WHERE p.job_id = j.id AND p.source = 'indeed')
             AND NOT EXISTS (SELECT 1 FROM postings p WHERE p.job_id = j.id AND p.is_snippet = 0)
             AND NOT EXISTS (SELECT 1 FROM fit_scores s WHERE s.job_id = j.id)
             AND j.first_seen > ?
           ORDER BY j.first_seen, j.id""",
        ((today - timedelta(days=LOOKUP_DAYS)).isoformat(),),
    ).fetchall()
    for job in [r for r in rows if normalize_company(r["company"])][:LOOKUP_PER_RUN]:
        counts.lookups += 1
        try:
            hits = _search_hits(client, job)
        except (requests.RequestException, SchemaChanged, ValueError) as e:
            log.warning("Full-text lookup for job %s failed: %s", job["id"], e)
            counts.lookup_failures += 1
            continue
        hit, dropped = clear_hit(job, hits)
        counts.lookup_dropped += dropped
        if hit is None:
            continue
        if conn.execute("SELECT 1 FROM postings WHERE source = ? AND source_id = ?",
                        (hit.source, hit.source_id)).fetchone():
            counts.lookup_known += 1
            continue
        hit.raw = {**(hit.raw or {}), "found_by": "lookup"}
        with conn:
            add_posting(conn, job["id"], hit, today.isoformat())
            if lookup_kind(job) == "unknown place":
                _relocate(conn, job, hit, cfg, targets, rules)
        counts.lookup_hits += 1


def settle_remote_jobs(conn: sqlite3.Connection, counts: Counts) -> None:
    """Home office Jobs outside the radius: keep only those whose full text suggests fully remote.
    An alert mail Job without a full text is kept on the alert's remote filter, its snippet rarely tells,
    and is decided again once a full text arrives."""
    rows = conn.execute(
        """SELECT j.id,
                  (SELECT p.description FROM postings p WHERE p.job_id = j.id AND p.is_snippet = 0
                   AND p.description IS NOT NULL ORDER BY length(p.description) DESC LIMIT 1) AS description,
                  EXISTS (SELECT 1 FROM postings p WHERE p.job_id = j.id AND p.source = 'indeed') AS from_alert
           FROM jobs j WHERE j.eligibility = 'unclear'
             AND (j.ineligible_reason = ? AND (description IS NOT NULL OR from_alert)
                  OR j.ineligible_reason = ? AND description IS NOT NULL)""",
        (HOMEOFFICE_REASON, remote_by_alert_filter().reason),
    ).fetchall()
    with conn:
        for row in rows:
            decision = settle_remote(row["description"]) if row["description"] else remote_by_alert_filter()
            conn.execute("UPDATE jobs SET eligibility = ?, ineligible_reason = ? WHERE id = ?",
                         (decision.eligibility, decision.reason, row["id"]))
            if decision.eligibility == "ineligible":
                counts.remote_dropped += 1


def store_postings(conn: sqlite3.Connection, postings, cfg: Config, targets: list[TargetCompany], rules: Rules,
                   index: JobIndex, today: date, counts: Counts) -> None:
    match_rules = MatchRules(cfg.reopening_days, cfg.radius_target_company_km)
    for posting in postings:
        posting.url = safe_url(posting.url)  # third-party links become href in the dashboard and the Digest
        placement = place(posting, cfg, targets)
        decision = decide(rules, posting.title, placement.distance_km,
                          placement.is_target_company, posting.homeoffice_possible)
        _, outcome = attach(conn, posting, placement, decision, today.isoformat(), index, match_rules)
        if outcome:
            counts.new_jobs += 1
            counts.match_pending += outcome == "pending"
            counts.reopenings += outcome == "reopening"
            setattr(counts, decision.eligibility, getattr(counts, decision.eligibility) + 1)


def collect_mail(conn: sqlite3.Connection, mailbox: Mailbox, cfg: Config, targets: list[TargetCompany], rules: Rules,
                 index: JobIndex, today: date, counts: Counts) -> None:
    """Store the Postings of each alert mail, then move the mail. Mails without Postings stay put."""
    gazetteer = Gazetteer.load(cfg.geo_path, (cfg.home_lat, cfg.home_lon))
    if gazetteer is None:
        log.warning("No place data at %s, alert mail Postings stay without distance", cfg.geo_path)
    try:
        with mailbox as box:
            # Before any Posting is stored, so a missing folder never leaves half-processed mails.
            processed = box.folder(cfg.imap_processed_folder)
            for mail in box.indeed_mails():
                counts.alert_mails += 1
                postings = parse_alert(mail.message)
                if not postings:
                    log.warning("Alert mail without Postings stays in the inbox: %s", mail.subject)
                    counts.alert_mails_unread += 1
                    continue
                counts.postings_seen += len(postings)
                for posting in postings:
                    locate(posting, gazetteer, counts)
                with conn:
                    store_postings(conn, postings, cfg, targets, rules, index, today, counts)
                box.move(mail.uid, processed)
                counts.alert_mails_processed += 1
    except (MailError, imaplib.IMAP4.error, OSError) as e:
        log.error("Alert mails failed: %s", e)
        counts.errors.append(f"Alert-Mails: {e}")


def settings_snapshot(cfg: Config, targets: list[TargetCompany], rules: Rules, terms: list[str],
                      alert_mail: bool) -> dict:
    """What was in effect for this run, so the eval can split results at a change.
    Lists that are personal or long are kept as hashes, which is enough to see that they changed."""
    try:
        profile = profile_hash(cfg.read_profile())
    except ConfigError:
        profile = None
    return {
        "model_scoring": cfg.model_scoring,
        "model_extraction": cfg.model_extraction,
        "prompt_scoring": prompts.version("scoring.md"),
        "prompt_career_extract": prompts.version("career_extract.md"),
        "prompt_search_terms": prompts.version("search_terms.md"),
        "prompt_weekly_report": prompts.version("weekly_report.md"),
        "profile": profile,
        "search_terms": sorted(terms, key=str.casefold),
        "exclusions": profile_hash("\n".join(sorted(rules.exclusions))),
        "target_companies": profile_hash("\n".join(sorted(t.name for t in targets))),
        "alert_mail": alert_mail,
        "radius_km": rules.radius_km,
        "radius_target_company_km": rules.radius_target_company_km,
        "remote_max_window_days": cfg.remote_max_window_days,
        "reopening_days": cfg.reopening_days,
        "max_scored_per_run": cfg.max_scored_per_run,
        "feedback_notes_in_prompt": cfg.feedback_notes_in_prompt,
    }


def run_daily(
    conn: sqlite3.Connection,
    cfg: Config,
    client: BundesagenturClient,
    targets: list[TargetCompany],
    rules: Rules,
    terms: list[str],
    today: date | None = None,
    mailbox: Mailbox | None = None,
) -> Counts:
    today = today or date.today()
    counts = Counts()
    run_id = db.start_run(conn, "daily", settings_snapshot(cfg, targets, rules, terms, mailbox is not None))
    try:
        window = remote_window_days(conn, today, cfg.remote_max_window_days)
        log.info("Daily run: %d Search terms, remote window %d days", len(terms), window)
        found, found_by = collect(client, cfg, terms, window, counts)
        counts.postings_seen = len(found)
        index = JobIndex.load(conn)
        with conn:
            store_postings(conn, found.values(), cfg, targets, rules, index, today, counts)
            record_terms(conn, found_by)
        if mailbox is not None:
            collect_mail(conn, mailbox, cfg, targets, rules, index, today, counts)
            lookup_full_text(conn, client, cfg, targets, rules, today, counts)
        fetch_missing_descriptions(conn, client, counts)
        settle_remote_jobs(conn, counts)
    except Exception as e:
        db.finish_run(conn, run_id, False, json.dumps(counts.__dict__, ensure_ascii=False), repr(e))
        raise
    ok = not counts.errors
    db.finish_run(conn, run_id, ok, json.dumps(counts.__dict__, ensure_ascii=False),
                  "; ".join(counts.errors) or None)
    return counts
