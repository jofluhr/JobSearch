"""The dashboard: a local Flask app on the same SQLite file. Lists Jobs, shows details, sets Job status."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from typing import Callable

from flask import Flask, abort, g, redirect, render_template, request, url_for

from . import db
from .config import Config
from .digest import REMOTE_LABELS

log = logging.getLogger(__name__)

LATEST_SCORE = f"LEFT JOIN fit_scores f ON f.id = {db.NEWEST_SCORE_ID}"
# The list's order; the previous and next links on the detail page follow it as well.
JOB_ORDER = "f.tier IS NULL, f.tier DESC, j.first_seen DESC, j.id DESC"
# Requests closer together than this belong to one visit, so filtering keeps the "neu" markers.
VISIT_GAP = timedelta(hours=1)
# A dashboard left open and used all day still starts a new visit after this long.
VISIT_MAX = timedelta(hours=8)
LOCAL_HOSTS = ("127.0.0.1", "localhost")

STATUS_LABELS = {"new": "neu", "interesting": "interessant", "not_interesting": "nicht interessant",
                 "applied": "beworben", "rejected": "abgelehnt"}
ELIGIBILITY_LABELS = {"reach": "in Reichweite oder unklar", "eligible": "in Reichweite", "unclear": "unklar",
                      "ineligible": "außerhalb", "all": "alle"}
SOURCE_LABELS = {"bundesagentur": "Bundesagentur", "indeed": "Indeed", "career_page": "Karriereseite"}
TIERS = {"3": "★★★", "2": "★★", "1": "★"}
TIER_LABELS = {**TIERS, "none": "unbewertet", "sorted_out": "aussortiert"}
# Sorted out Jobs are hidden until the candidate sets a Job status.
HIDDEN_SORTED_OUT = "(f.sorted_out = 1 AND j.status = 'new')"


def job_filter(args) -> tuple[str, list, dict]:
    """WHERE clause and parameters for the list, from the query string. Unknown values are ignored."""
    where, params = [], []
    chosen = {
        "tier": args.get("tier", ""),
        "status": args.get("status", ""),
        "eligibility": args.get("eligibility", "reach"),
        "source": args.get("source", ""),
        "term": args.get("term", ""),
        "since": args.get("since", ""),
    }
    if chosen["tier"] == "sorted_out":
        where.append(HIDDEN_SORTED_OUT)
    else:
        where.append(f"NOT coalesce({HIDDEN_SORTED_OUT}, 0)")
    if chosen["tier"] == "none":
        where.append("f.tier IS NULL")
    elif chosen["tier"] in TIERS:
        where.append("f.tier = ?")
        params.append(int(chosen["tier"]))
    if chosen["status"] in STATUS_LABELS:
        where.append("j.status = ?")
        params.append(chosen["status"])
    if chosen["eligibility"] == "reach":
        where.append("j.eligibility != 'ineligible'")
    elif chosen["eligibility"] in ("eligible", "unclear", "ineligible"):
        where.append("j.eligibility = ?")
        params.append(chosen["eligibility"])
    if chosen["source"] in SOURCE_LABELS:
        where.append("EXISTS (SELECT 1 FROM postings p WHERE p.job_id = j.id AND p.source = ?)")
        params.append(chosen["source"])
    if chosen["term"]:
        where.append("""EXISTS (SELECT 1 FROM postings p JOIN posting_terms t ON t.posting_id = p.id
                                WHERE p.job_id = j.id AND t.term = ?)""")
        params.append(chosen["term"])
    try:
        where.append("j.first_seen >= ?")
        params.append(date.fromisoformat(chosen["since"]).isoformat())
    except ValueError:
        where.pop()
        chosen["since"] = ""
    return " AND ".join(where) or "1", params, chosen


def filter_args(chosen: dict) -> dict[str, str]:
    """The chosen filters for links that carry them, without empty values and the default Eligibility."""
    return {k: v for k, v in chosen.items() if v and not (k == "eligibility" and v == "reach")}


def neighbours(conn: sqlite3.Connection, args, job_id: int) -> tuple[int | None, int | None]:
    """Previous and next Job with Job status new in the filtered list's order. The status filter
    does not apply, and the current Job keeps its place even after it was judged."""
    where, params, _ = job_filter({k: v for k, v in args.items() if k != "status"})
    ids = [r[0] for r in conn.execute(
        f"""SELECT j.id FROM jobs j {LATEST_SCORE} WHERE {where} AND (j.status = 'new' OR j.id = ?)
            ORDER BY {JOB_ORDER}""",
        [*params, job_id],
    )]
    if job_id not in ids:
        return None, None
    at = ids.index(job_id)
    return (ids[at - 1] if at > 0 else None), (ids[at + 1] if at + 1 < len(ids) else None)


def visit_baseline(conn: sqlite3.Connection, now: datetime) -> int:
    """Highest Job id seen at the previous visit. Jobs above it are new to the candidate.
    Every page view keeps the current visit going."""
    meta = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'visit_%'").fetchall())
    baseline = int(meta.get("visit_baseline_id", 0))
    last, started = meta.get("visit_at"), meta.get("visit_started_at")
    if (last is None or started is None or now - datetime.fromisoformat(last) > VISIT_GAP
            or now - datetime.fromisoformat(started) > VISIT_MAX):
        baseline = int(meta.get("visit_seen_id", 0))
        started = now.isoformat(timespec="seconds")
    seen = conn.execute("SELECT coalesce(max(id), 0) FROM jobs").fetchone()[0]
    try:
        with conn:
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                [("visit_baseline_id", str(baseline)), ("visit_seen_id", str(seen)),
                 ("visit_at", now.isoformat(timespec="seconds")), ("visit_started_at", started)],
            )
    except sqlite3.OperationalError as e:  # a Daily run holds the write lock; the page still works
        log.warning("Could not record the visit: %s", e)
    return baseline


def term_options(conn: sqlite3.Connection) -> dict[str, str]:
    """Search terms that found something, with the number of Jobs each one found."""
    rows = conn.execute(
        """SELECT t.term, count(DISTINCT p.job_id) n FROM posting_terms t JOIN postings p ON p.id = t.posting_id
           GROUP BY t.term ORDER BY t.term"""
    ).fetchall()
    return {r["term"]: f"{r['term']} ({r['n']})" for r in rows}


def terms_by_job(conn: sqlite3.Connection) -> dict[int, str]:
    found: dict[int, set[str]] = {}
    for row in conn.execute("SELECT p.job_id, t.term FROM posting_terms t JOIN postings p ON p.id = t.posting_id"):
        found.setdefault(row["job_id"], set()).add(row["term"])
    return {job_id: ", ".join(sorted(terms)) for job_id, terms in found.items()}


def reopening_chain(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """All Jobs linked by reopening_of, oldest first: the same opening advertised again."""
    rows = conn.execute(
        """WITH RECURSIVE root(id, up) AS (
               SELECT id, reopening_of FROM jobs WHERE id = ?
               UNION SELECT j.id, j.reopening_of FROM jobs j JOIN root r ON j.id = r.up),
           chain(id) AS (
               SELECT id FROM root WHERE up IS NULL
               UNION SELECT j.id FROM jobs j JOIN chain c ON j.reopening_of = c.id)
           SELECT j.id, j.first_seen, j.last_seen, j.status FROM jobs j JOIN chain USING (id)
           ORDER BY j.first_seen, j.id""",
        (job_id,),
    ).fetchall()
    return rows


def create_app(cfg: Config, now: Callable[[], datetime] = datetime.now) -> Flask:
    app = Flask(__name__)
    # Any web page could send a form to 127.0.0.1, so status changes need this per-start token.
    token = secrets.token_urlsafe(16)
    db.connect(cfg.db_path).close()  # migrate once, not per request

    def conn() -> sqlite3.Connection:
        if "conn" not in g:
            g.conn = db.connect(cfg.db_path, run_migrations=False)
        return g.conn

    @app.before_request
    def local_only() -> None:
        # Against DNS rebinding: a foreign page reaching 127.0.0.1 still sends its own host name.
        if request.host.rsplit(":", 1)[0] not in LOCAL_HOSTS:
            abort(400)

    @app.errorhandler(400)
    def bad_request(_e):
        return "<p>Ungültige Anfrage. Seite neu laden und erneut versuchen.</p>", 400

    @app.errorhandler(404)
    def not_found(_e):
        return '<p>Seite nicht gefunden. <a href="/">Zur Jobliste</a></p>', 404

    @app.teardown_appcontext
    def close(_exc) -> None:
        connection = g.pop("conn", None)
        if connection is not None:
            connection.close()

    @app.context_processor
    def labels() -> dict:
        return {"REMOTE_LABELS": REMOTE_LABELS, "STATUS_LABELS": STATUS_LABELS, "ELIGIBILITY_LABELS": ELIGIBILITY_LABELS,
                "SOURCE_LABELS": SOURCE_LABELS, "TIER_LABELS": TIER_LABELS}

    @app.get("/")
    def jobs():
        where, params, chosen = job_filter(request.args)
        rows = conn().execute(
            f"""SELECT j.*, f.tier, f.low_confidence, f.sorted_out FROM jobs j {LATEST_SCORE} WHERE {where}
                ORDER BY {JOB_ORDER}""",
            params,
        ).fetchall()
        return render_template("jobs.html", jobs=rows, chosen=chosen, nav_args=filter_args(chosen),
                               baseline=visit_baseline(conn(), now()),
                               terms=term_options(conn()), job_terms=terms_by_job(conn()),
                               token=token, here=request.full_path.rstrip("?"))

    @app.get("/job/<int:job_id>")
    def job_detail(job_id: int):
        job = conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            abort(404)
        postings = conn().execute(
            "SELECT * FROM postings WHERE job_id = ? ORDER BY first_seen, id", (job_id,)).fetchall()
        description = max((p["description"] or "" for p in postings), key=len, default="")
        row = conn().execute(
            "SELECT * FROM fit_scores WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        score = None
        if row:
            score = dict(row)
            score["matching"] = json.loads(row["matching_skills"] or "[]")
            score["missing"] = json.loads(row["missing_skills"] or "[]")
        terms = [r[0] for r in conn().execute(
            """SELECT DISTINCT t.term FROM posting_terms t JOIN postings p ON p.id = t.posting_id
               WHERE p.job_id = ? ORDER BY t.term""", (job_id,))]
        notes = conn().execute(
            "SELECT * FROM feedback_notes WHERE job_id = ? ORDER BY created_at DESC, id DESC", (job_id,)).fetchall()
        visit_baseline(conn(), now())
        nav_args = filter_args(job_filter(request.args)[2])
        prev_id, next_id = neighbours(conn(), request.args, job_id)
        onward = (url_for("job_detail", job_id=next_id, **nav_args) if next_id
                  else url_for("jobs", **nav_args))
        return render_template("job.html", job=job, postings=postings, description=description, score=score,
                               chain=reopening_chain(conn(), job_id), notes=notes, token=token, terms=terms,
                               nav_args=nav_args, prev_id=prev_id, next_id=next_id, onward=onward,
                               here=url_for("job_detail", job_id=job_id, **nav_args))

    def check_token() -> None:
        if not hmac.compare_digest(request.form.get("token", "").encode(), token.encode()):
            abort(400)

    @app.get("/bericht")
    @app.get("/bericht/<int:report_id>")
    def report_page(report_id: int | None = None, notice: str | None = None, code: int = 200):
        from . import report

        entries = report.list_reports(conn())
        ids = [e.id for e in entries]
        if report_id is None:
            report_id = ids[0] if ids else None
        elif report_id not in ids:
            abort(404)
        shown = report.load_report(conn(), report_id) if report_id else None
        files = {lever: report.applied_file(cfg, lever).name for lever in report.APPLIED}
        return render_template("report.html", report=shown, entries=entries, token=token, notice=notice,
                               LEVER_LABELS=report.LEVER_LABELS, APPLIED=report.APPLIED, files=files), code

    @app.get("/markt")
    def market_page():
        """The Market report from the database as it is; grouping new texts is left to `jobsearch market`."""
        from . import market

        today = now().date()
        return render_template("market.html", companies=market.active_companies(conn(), today),
                               skills=market.skill_table(conn(), market.period_for(None, None), today),
                               ungrouped=len(market.ungrouped_texts(conn())),
                               MIN_JOBS=market.MIN_JOBS, WINDOW_DAYS=market.WINDOW_DAYS)

    @app.get("/markt/gruppe")
    def market_group():
        from . import market

        name = request.args.get("name", "")
        texts = market.group_texts(conn(), name)
        if not texts:
            abort(404)
        return render_template("market_group.html", group=name, texts=texts)

    @app.post("/empfehlung/<int:rec_id>")
    def decide(rec_id: int):
        from . import report, scheduler

        check_token()
        row = conn().execute("SELECT report_id FROM recommendations WHERE id = ?", (rec_id,)).fetchone()
        if row is None:
            abort(404)
        # The same lock as the runs: a run reads these files, so they are not written under it.
        with scheduler.RunLock(cfg.data_dir / "run.lock") as held:
            if not held:
                return report_page(row["report_id"], "Gerade läuft ein Lauf. Bitte später noch einmal versuchen.", 409)
            try:
                report.decide(conn(), cfg, rec_id, request.form.get("decision", ""), now())
            except report.DecisionError as e:
                return report_page(row["report_id"], str(e), 409)
        return redirect(url_for("report_page", report_id=row["report_id"]), code=303)

    @app.post("/job/<int:job_id>/status")
    def set_status(job_id: int):
        check_token()
        status = request.form.get("status", "")
        if status not in STATUS_LABELS:
            abort(400)
        note = request.form.get("note", "").strip()
        stamp = now()
        with conn() as c:
            changed = c.execute(
                """UPDATE jobs SET
                       status_changed_at = CASE WHEN status != :status THEN :stamp ELSE status_changed_at END,
                       status = :status,
                       applied_on = CASE WHEN :status = 'applied' THEN coalesce(applied_on, :day) ELSE applied_on END
                   WHERE id = :id""",
                {"status": status, "stamp": stamp.isoformat(timespec="seconds"), "day": stamp.date().isoformat(),
                 "id": job_id},
            ).rowcount
            if not changed:
                abort(404)
            if note:
                c.execute("INSERT INTO feedback_notes (job_id, status, note, created_at) VALUES (?, ?, ?, ?)",
                          (job_id, status, note, stamp.isoformat(timespec="seconds")))
        back = request.form.get("next", "")
        if not back.startswith("/") or back.startswith("//") or "\\" in back:  # only pages of this app
            back = url_for("job_detail", job_id=job_id)
        return redirect(back, code=303)

    return app
