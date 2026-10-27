"""Command line entry point: jobsearch init | status | daily | score | run | install-task | uninstall-task | dashboard | eval | check-injection | market."""

from __future__ import annotations

import argparse
import functools
import json
import logging
import shutil
import sys
import time
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler

from . import ai, config, db, evaluation, geo, guard, scheduler, usage
from .career import ScanCounts, run_career_scan, scan_due
from .companies import load_target_companies
from .config import Config, ConfigError
from .daily import run_daily
from .digest import write_digest
from .eligibility import Rules
from .notify import notify, toast
from .scoring import backlog, run_rescoring, run_scoring
from .search_terms import active_terms, derived_terms, is_current, refresh_derived, switched_off
from .sources.bundesagentur import BundesagenturClient
from .sources.indeed_mail import Mailbox

log = logging.getLogger(__name__)

def _setup_logging(cfg: Config, verbose: bool) -> None:
    log_dir = cfg.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_dir / "jobsearch.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    ]
    if sys.stderr:  # None under pythonw, as started by Task Scheduler
        stream = logging.StreamHandler()
        stream.setLevel(logging.DEBUG if verbose else logging.WARNING)
        handlers.append(stream)
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def cmd_init(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.private_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for example in (cfg.root / "private.example").iterdir():
        target = cfg.private_dir / example.name
        if not target.exists():
            shutil.copy(example, target)
            created.append(f"private/{example.name}")
    env = cfg.root / ".env"
    if not env.exists():
        shutil.copy(cfg.root / ".env.example", env)
        created.append(".env")
    db.connect(cfg.db_path).close()
    print(f"Datenbank bereit: {cfg.db_path}")
    print(guard.enable_commit_check(cfg.root))
    if not cfg.geo_path.exists():
        try:
            n = geo.download(cfg.geo_path)
            print(f"Ortsdaten geladen: {n} Orte (GeoNames, CC BY 4.0)")
        except Exception as e:  # init must still work offline
            print(f"Ortsdaten konnten nicht geladen werden ({e}). Später 'jobsearch init' erneut ausführen.")
    if created:
        print("Angelegt aus den Beispielen, bitte ausfüllen: " + ", ".join(created))
    else:
        print("Private Dateien und .env waren schon vorhanden, nichts überschrieben.")


def cmd_status(cfg: Config) -> None:
    conn = db.connect(cfg.db_path)
    print("Jobs nach Eligibility:")
    for row in conn.execute("SELECT eligibility, count(*) n FROM jobs GROUP BY eligibility ORDER BY eligibility"):
        print(f"  {row['eligibility']:<11} {row['n']}")
    total = conn.execute("SELECT count(*) FROM postings").fetchone()[0]
    missing = conn.execute(
        """SELECT count(*) FROM postings p JOIN jobs j ON j.id = p.job_id
           WHERE p.description IS NULL AND j.eligibility != 'ineligible'"""
    ).fetchone()[0]
    print(f"Postings: {total} (ohne Beschreibung, aber in Reichweite: {missing})")
    tiers = dict(conn.execute(f"SELECT s.tier, count(*) FROM jobs j JOIN fit_scores s ON s.id = {db.NEWEST_SCORE_ID} "
                              "GROUP BY s.tier").fetchall())
    print(f"Fit scores: ★★★ {tiers.get(3, 0)}, ★★ {tiers.get(2, 0)}, ★ {tiers.get(1, 0)}; "
          f"warten auf Bewertung: {backlog(conn)}")
    derived = derived_terms(conn)
    # status is a look around, so a missing file is shown as empty instead of stopping it
    manual = config.read_list_file(cfg.search_terms_path) if cfg.search_terms_path.exists() else []
    active = active_terms(manual, derived)
    by_hand = {t.casefold() for t in manual}
    try:
        stale = bool(derived) and not is_current(conn, cfg.read_profile())
    except ConfigError:
        stale = False
    print(f"Suchbegriffe ({len(active)} aktiv):")
    print("  manuell:        " + (", ".join(t for t in active if t.casefold() in by_hand) or "-"))
    print("  aus dem Profil: " + (", ".join(t for t in active if t.casefold() not in by_hand) or "-")
          + (" (veraltet: Profil geändert, neue Ableitung beim nächsten Lauf)" if stale else ""))
    off = switched_off(manual)
    if off:
        print("  abgeschaltet:   " + ", ".join(off))
    pages = conn.execute(
        """SELECT count(*) n, sum(fail_count > 0) failing, sum(empty_count > 0) empty FROM career_pages"""
    ).fetchone()
    if pages["n"]:
        print(f"Karriereseiten: {pages['n']} geprüft, zuletzt fehlgeschlagen: {pages['failing']}, "
              f"ohne Stellen: {pages['empty']}")
    week = evaluation.Period("7 Tage", (date.today() - timedelta(days=6)).isoformat())
    week_usage = evaluation.usage_metrics(conn, week).total
    if week_usage.calls:
        print(f"KI-Verbrauch (7 Tage): {usage.summary(week_usage)}")
    print("Letzte Läufe:")
    for row in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5"):
        state = {1: "ok", 0: "FEHLER", None: "läuft/abgebrochen"}[row["ok"]]
        print(f"  {row['started_at']}  {row['kind']:<11} {state}")
        if row["error"]:
            print(f"      {row['error']}")


def cmd_score(cfg: Config, conn, limit: int | None = None, warnings: list[str] = ()) -> int:
    """Score, write the Digest, show a toast. Returns 1 if the AI step failed."""
    counts = run_scoring(conn, cfg, limit)
    path, report = write_digest(conn, cfg.digests_dir, date.today(), counts, warnings)
    print(f"Bewertet: {counts.scored} (davon geringe Sicherheit: {counts.low_confidence}, "
          f"aussortiert: {counts.sorted_out}), "
          f"remote bestätigt/abgelehnt: {counts.remote_confirmed}/{counts.remote_rejected}, "
          f"zusammengeführt: {counts.merged}, Sammelanzeigen: {counts.generic}, "
          f"warten noch: {counts.backlog}")
    print(f"Digest: {path}")
    notify(report, counts, path, warnings)
    if counts.error:
        print(f"Fehler bei der Bewertung: {counts.error}", file=sys.stderr)
        return 1
    return 0


def cmd_rescore(cfg: Config, ask=None) -> int:
    """Rescore the ★ Jobs from an older scoring prompt, all in one go. Returns 1 if a batch failed."""
    counts = run_rescoring(db.connect(cfg.db_path), cfg, ask=ask or ai.ask)
    if not counts.rescored and not counts.error:
        print("Keine ★-Jobs mit älterem Bewertungs-Prompt, nichts neu zu bewerten.")
        return 0
    print(f"Neubewertung: bewertet {counts.rescored}, davon aussortiert {counts.sorted_out}, "
          f"Tier verändert {counts.tier_changed}, übrig {counts.left}")
    if counts.moved_up:
        print("Aufgestiegen (vielleicht jetzt einen Blick wert):")
        for job in counts.moved_up:
            print(f"  ★ → {evaluation.STARS[job['tier']]}: {job['title']} – {job['company']}")
    if counts.error:
        print(f"Neubewertung abgebrochen: {counts.error}. Schon bewertete Jobs bleiben gespeichert, übrig "
              f"{counts.left}; erneut aufrufen, um fortzusetzen.", file=sys.stderr)
        return 1
    return 0


def career_summary(counts: ScanCounts) -> str:
    line = (f"Karriereseiten: {counts.pages} geprüft, {counts.unchanged} unverändert, {counts.extracted} ausgelesen, "
            f"{counts.feeds} per Feed, {counts.new_jobs} neue Jobs")
    if counts.ungrounded:
        line += f", verworfen (nicht im Seitentext): {counts.ungrounded}"
    return line


def career_scan(cfg: Config, conn, rules: Rules, targets) -> list[str]:
    """Run the Career scan, print a summary, return warnings for the Digest."""
    gazetteer = geo.Gazetteer.load(cfg.geo_path, (cfg.home_lat, cfg.home_lon))
    try:
        counts = run_career_scan(conn, cfg, targets, rules, gazetteer=gazetteer)
    except Exception as e:  # the Daily run's own Jobs must still be scored and the Digest written
        log.exception("Career scan failed")
        return [f"Karriereseiten-Scan abgebrochen: {e}"]
    print(career_summary(counts))
    warnings = list(counts.warnings)
    if counts.error:
        warnings.append(f"Karriereseiten nicht ausgelesen: {counts.error}")
    for warning in warnings:
        print(warning, file=sys.stderr)
    return warnings


def terms_for_run(cfg: Config, conn, ask=ai.ask) -> tuple[list[str], list[str]]:
    """The Search terms for this run, after deriving new ones if the Profile changed, and warnings."""
    warnings = []
    manual = config.read_list_file(cfg.search_terms_path)
    derived = derived_terms(conn)
    try:
        profile = cfg.read_profile()
    except ConfigError as e:  # collecting works without a Profile, scoring reports it
        log.info("No Search terms derived: %s", e)
    else:
        # A rare call where judgment matters more than quota, so the scoring model is reused.
        result = refresh_derived(conn, profile, model=cfg.model_scoring, ask=ask) if profile.strip() else None
        if result:
            derived = result.terms
            if result.changed:
                print("Neue Suchbegriffe aus dem Profil: " + ", ".join(result.terms))
            if result.error:
                warnings.append(f"Suchbegriffe aus dem Profil nicht abgeleitet: {result.error}")
    terms = active_terms(manual, derived)
    if not terms:
        raise ConfigError(f"{cfg.search_terms_path} enthält keine Suchbegriffe, und aus dem Profil wurden keine "
                          f"abgeleitet. {' '.join(warnings)}".strip())
    return terms, warnings


def cmd_daily(cfg: Config, score: bool, career: str = "auto", *, client=None, ask=ai.ask) -> int:
    """career: 'auto' scans career pages when due, 'force' always, 'off' never."""
    cfg.require_home()
    rules = Rules(cfg.radius_km, cfg.radius_target_company_km,
                  tuple(config.read_list_file(cfg.exclusions_path)))
    targets = load_target_companies(cfg.companies_path)
    conn = db.connect(cfg.db_path)
    terms, warnings = terms_for_run(cfg, conn, ask)
    for warning in warnings:
        print(warning, file=sys.stderr)
    mailbox = Mailbox(cfg.imap_host, cfg.imap_user, cfg.imap_password) if cfg.imap_configured else None
    counts = run_daily(conn, cfg, client or BundesagenturClient(), targets, rules, terms, mailbox=mailbox)
    print(json.dumps({k: v for k, v in counts.__dict__.items() if k != "errors"}, ensure_ascii=False))
    if counts.lookups:
        print(f"Volltext gesucht: {counts.lookups}, gefunden: {counts.lookup_hits}, "
              f"schon bekannt: {counts.lookup_known}, verworfen: {counts.lookup_dropped}, "
              f"fehlgeschlagen: {counts.lookup_failures}")
    for error in counts.errors:
        print(f"Fehler: {error}", file=sys.stderr)
    status = 1 if counts.errors else 0
    warnings += [f"Fehler beim Sammeln: {e}" for e in counts.errors]
    if counts.places_unknown and not cfg.geo_path.exists():
        warnings.append("Ortsdaten fehlen, Jobs aus Alert-Mails haben keine Entfernung. 'jobsearch init' ausführen.")
    if counts.alert_mails_unread:
        warnings.append(f"{counts.alert_mails_unread} Alert-Mail(s) ohne erkannte Jobs, sie liegen noch im Posteingang.")
    if career == "force" or (career == "auto" and scan_due(conn, date.today(), cfg.career_scan_interval_days)):
        warnings.extend(career_scan(cfg, conn, rules, targets))
    if score:
        status = max(status, cmd_score(cfg, conn, warnings=warnings))
        weekly_report(cfg, conn, ask)
    return status


def weekly_report(cfg: Config, conn, ask=ai.ask) -> None:
    """The Weekly report when due, after scoring so the week's scores are in. A failure is stored with
    the report and tried again at the next run; it never changes the run's outcome."""
    from . import report

    if not report.report_due(conn, date.today(), cfg.report_interval_days):
        return
    try:
        result = report.make_report(conn, cfg, date.today(), ask=ask)
        error = result.error
    except Exception as e:  # the collected Jobs and the Digest matter more than the report
        log.exception("Weekly report failed")
        error = str(e)
    if error:
        print(f"Wochenbericht fehlgeschlagen, der nächste Lauf versucht es erneut: {error}", file=sys.stderr)
        return
    print(f"Wochenbericht: {result.path}")
    toast("JobSearch", f"Wochenbericht: {result.counts.recommendations} Empfehlungen",
          on_click=result.path.resolve().as_uri())


def cmd_run(cfg: Config) -> int:
    """The scheduled run. There is no console under pythonw, so problems end up in the log and a toast."""
    status = 0

    def daily() -> int:
        nonlocal status
        status = cmd_daily(cfg, score=True)
        return status

    conn = db.connect(cfg.db_path)
    try:
        result = scheduler.run_scheduled(conn, cfg, daily=daily,
                                         network_ok=scheduler.network_ok, toast=toast, sleep=time.sleep)
    except Exception as e:
        log.exception("Scheduled run failed")
        toast("JobSearch", f"Lauf fehlgeschlagen: {e}. Details in data/logs/jobsearch.log")
        return 1
    finally:
        conn.close()
    log.info("Scheduled run: %s", result)
    return status


def cmd_report(cfg: Config, dry_run: bool = False, ask=ai.ask) -> int:
    """The Weekly report now. --dry-run prints what the AI would get, without calling it."""
    from . import report

    conn = db.connect(cfg.db_path)
    if dry_run:
        print(report.build_input(conn, cfg, date.today()).to_prompt_json())
        return 0
    print("Wochenbericht: 1 Aufruf an Claude ...")
    result = report.make_report(conn, cfg, date.today(), ask=ask)
    if result.error:
        print(f"Wochenbericht fehlgeschlagen: {result.error}", file=sys.stderr)
        return 1
    print(result.path.read_text(encoding="utf-8"))
    print(f"Gespeichert: {result.path}")
    return 0


def cmd_install_task(cfg: Config) -> int:
    try:
        scheduler.install_task(cfg.root)
    except scheduler.TaskError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Aufgabe '{scheduler.TASK_NAME}' angelegt: läuft 5 Minuten nach jeder Anmeldung, "
          "der Daily run höchstens einmal am Tag.")
    return 0


def cmd_uninstall_task() -> int:
    try:
        scheduler.uninstall_task()
    except scheduler.TaskError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Aufgabe '{scheduler.TASK_NAME}' entfernt.")
    return 0


def cmd_career_scan(cfg: Config) -> int:
    """Only the Career scan. Its new Jobs are scored by the next daily or score run."""
    cfg.require_home()
    rules = Rules(cfg.radius_km, cfg.radius_target_company_km, tuple(config.read_list_file(cfg.exclusions_path)))
    warnings = career_scan(cfg, db.connect(cfg.db_path), rules, load_target_companies(cfg.companies_path))
    return 1 if warnings else 0


def recording_ask(conn):
    """ai.ask that stores its AI usage without a run, for the commands that start none."""
    return functools.partial(ai.ask, usage_recorder=db.usage_recorder(conn))


def cmd_career_setup(cfg: Config, recheck: bool) -> int:
    from .career_setup import run_setup

    conn = db.connect(cfg.db_path)
    problems = {}
    if recheck:
        problems = {r["company"]: r["last_error"] or "die Seite zeigt keine Stellen"
                    for r in conn.execute("SELECT * FROM career_pages WHERE fail_count > 0 OR empty_count > 0")}
        print(f"Prüfe {len(problems)} Karriereseiten neu, die fehlschlugen oder keine Stellen zeigten.")
    print("Suche Karriereseiten mit Websuche, das dauert ein paar Minuten ...")
    result = run_setup(cfg.companies_path, model=cfg.model_scoring, ask=recording_ask(conn), recheck=problems)
    print(f"Karriereseiten eingetragen: {result.filled}")
    if result.not_found:
        print("Nicht gefunden: " + ", ".join(result.not_found))
    for note in result.notes:
        print(f"Hinweis: {note}")
    if result.error:
        print(f"Abgebrochen: {result.error}. Erneut starten macht beim Rest weiter.", file=sys.stderr)
        return 1
    print(f"Bitte die Einträge in {cfg.companies_path} prüfen (Sicherung: companies.csv.bak).")
    return 0


def cmd_check_injection(cfg: Config) -> int:
    """Two AI calls with synthetic injected ads and a career page; prints pass or fail per expectation."""
    from . import injection

    print("Injection-Check: 2 Aufrufe an Claude (Bewertung und Extraktion), gespeichert wird nur der KI-Verbrauch.")
    try:
        report = injection.run(cfg, cfg.root / "tests" / "fixtures" / "injection",
                               ask=recording_ask(db.connect(cfg.db_path)))
    except ai.AiError as e:
        print(f"Injection-Check abgebrochen: {e}", file=sys.stderr)
        return 1
    print("Modelle: " + ", ".join(report.models))
    for line in report.info:
        print(f"  {line}")
    for check in report.checks:
        detail = f" ({check.detail})" if check.detail else ""
        print(f"{'OK    ' if check.ok else 'FEHLER'} {check.name}{detail}")
    passed = sum(c.ok for c in report.checks)
    print(f"{passed} von {len(report.checks)} Erwartungen erfüllt.")
    return 0 if report.ok else 1


def cmd_eval(cfg: Config, changes: bool, since: date | None = None, until: date | None = None,
             split: date | None = None, scores: bool = False, by: str | None = None, funnel: bool = False,
             usage: bool = False) -> int:
    conn = db.connect(cfg.db_path)
    if changes:
        print("\n".join(evaluation.changes_report(conn)))
        return 0
    if funnel:
        print("\n".join(evaluation.funnel_report(conn, cfg.found_elsewhere_path)))
        return 0
    try:
        # --until names the last day, a Period ends the day after
        chosen = evaluation.periods(since and since.isoformat(), until and (until + timedelta(days=1)).isoformat(),
                                    split and split.isoformat())
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if usage:
        print("\n".join(evaluation.usage_report(conn, chosen)))
    elif scores or by:
        print("\n".join(evaluation.score_report(conn, chosen, by)))
    else:
        print("\n".join(evaluation.stage_report(conn, chosen)))
    return 0


def cmd_market(cfg: Config, regroup: bool = False, since: date | None = None, until: date | None = None,
               today: date | None = None, ask=None) -> int:
    """The Market report. Groups new skill texts first (a few AI calls); if that fails, the report
    is printed anyway with what is grouped so far. Returns 1 if the AI step failed."""
    from . import market

    try:
        period = market.period_for(since, until)
    except ValueError as e:  # checked before any AI call
        print(e, file=sys.stderr)
        return 2
    conn = db.connect(cfg.db_path)
    result = market.group_skills(conn, model=cfg.model_extraction, ask=ask or recording_ask(conn), regroup=regroup)
    today = today or date.today()
    print("\n".join(market.company_report(market.active_companies(conn, today))))
    print()
    print(market.grouping_summary(result))
    print("\n".join(market.skill_report(market.skill_table(conn, period, today), len(market.ungrouped_texts(conn)),
                                        period)))
    if result.error:
        print(f"Skill-Gruppen nicht aktualisiert: {result.error}", file=sys.stderr)
        return 1
    return 0


def cmd_dashboard(cfg: Config, port: int, open_browser: bool) -> int:
    import socket
    import threading
    import webbrowser

    from .dashboard import create_app

    with socket.socket() as probe:  # before the browser opens on whatever else holds the port
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(f"Port {port} ist schon belegt, läuft das Dashboard schon? Sonst --port wählen.", file=sys.stderr)
            return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard: {url} (beenden mit Strg+C)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    create_app(cfg).run(host="127.0.0.1", port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobsearch")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log auch auf der Konsole ausgeben")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Datenbank und private Dateien anlegen")
    sub.add_parser("status", help="Überblick über Datenbank und letzte Läufe")
    daily = sub.add_parser("daily", help="Daily run: Postings sammeln, Eligibility prüfen, bewerten")
    daily.add_argument("--no-score", action="store_true", help="Nur sammeln, nicht bewerten")
    daily.add_argument("--career", choices=["auto", "force", "off"], default="auto",
                       help="Karriereseiten: wenn fällig (Standard), immer oder nie")
    score = sub.add_parser("score", help="Unbewertete Jobs bewerten, Digest schreiben")
    score.add_argument("--limit", type=int, help="Höchstens so viele Jobs (statt MAX_SCORED_PER_RUN)")
    sub.add_parser("rescore", help="★-Jobs mit älterem Bewertungs-Prompt neu bewerten "
                                   "(ein KI-Aufruf je MAX_SCORED_PER_RUN Jobs)")
    sub.add_parser("run", help="Geplanter Lauf: Daily run, falls heute noch keiner erfolgreich war")
    sub.add_parser("install-task", help="Aufgabe in der Aufgabenplanung anlegen (bei Anmeldung)")
    sub.add_parser("uninstall-task", help="Aufgabe aus der Aufgabenplanung entfernen")
    setup = sub.add_parser("career-setup", help="Karriereseiten der Zielunternehmen per Websuche finden")
    setup.add_argument("--recheck", action="store_true",
                       help="Auch Seiten neu suchen, die fehlschlugen oder keine Stellen zeigten")
    sub.add_parser("career-scan", help="Nur die Karriereseiten prüfen (ohne Bewertung)")
    dashboard = sub.add_parser("dashboard", help="Dashboard im Browser öffnen")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    market = sub.add_parser("market", help="Marktbericht: aktive Firmen und Skill-Gruppen (bis zu 3 KI-Aufrufe)")
    market.add_argument("--regroup", action="store_true", help="Alle Skill-Gruppen verwerfen und neu bilden")
    market.add_argument("--since", type=date.fromisoformat, help="Skill-Tabelle: erster Tag (JJJJ-MM-TT)")
    market.add_argument("--until", type=date.fromisoformat, help="Skill-Tabelle: letzter Tag (JJJJ-MM-TT)")
    sub.add_parser("check-injection", help="Prompts gegen eingeschleuste Anweisungen prüfen (2 KI-Aufrufe)")
    report = sub.add_parser("report", help="Wochenbericht mit Empfehlungen jetzt erstellen (1 KI-Aufruf)")
    report.add_argument("--dry-run", action="store_true", help="Nur die Eingabe für die KI zeigen, ohne Aufruf")
    evaluate = sub.add_parser("eval", help="Auswertung der gespeicherten Läufe (ohne KI)")
    evaluate.add_argument("--changes", action="store_true", help="Wann sich die Einstellungen geändert haben")
    evaluate.add_argument("--since", type=date.fromisoformat, help="Erster Tag (JJJJ-MM-TT)")
    evaluate.add_argument("--until", type=date.fromisoformat, help="Letzter Tag (JJJJ-MM-TT)")
    evaluate.add_argument("--split", type=date.fromisoformat,
                          help="Vorher und nachher vergleichen, z. B. am Tag einer Änderung (JJJJ-MM-TT)")
    evaluate.add_argument("--scores", action="store_true", help="Fit score gegen Job status")
    evaluate.add_argument("--by", choices=evaluation.GROUPS, help="Fit scores gruppieren (schließt --scores ein)")
    evaluate.add_argument("--funnel", action="store_true",
                          help="Wo Stellen aus private/found_elsewhere.csv verloren gingen")
    evaluate.add_argument("--usage", action="store_true",
                          help="KI-Verbrauch: Tokens und API-Gegenwert je Tag, Zweck und Modell. Der API-Gegenwert "
                               "ist der Listenpreis über die API, kein echter Preis (die Aufrufe laufen über das "
                               "Abo), und kein Anteil am Kontingent (dessen Grenze ist nicht in Tokens bekannt, "
                               "und die interaktive Nutzung fehlt)")
    args = parser.parse_args(argv)

    try:
        cfg = config.load()
        _setup_logging(cfg, args.verbose)
        if args.command == "init":
            cmd_init(cfg)
        elif args.command == "status":
            cmd_status(cfg)
        elif args.command in ("daily", "score", "rescore", "career-scan", "report"):
            # The same lock as the scheduled run, so a manual run never overlaps it.
            with scheduler.RunLock(cfg.data_dir / "run.lock") as held:
                if not held:
                    print("Ein anderer Lauf ist gerade aktiv, bitte später erneut versuchen.", file=sys.stderr)
                    return 1
                if args.command == "daily":
                    return cmd_daily(cfg, score=not args.no_score, career=args.career)
                if args.command == "career-scan":
                    return cmd_career_scan(cfg)
                if args.command == "report":
                    return cmd_report(cfg, args.dry_run)
                if args.command == "rescore":
                    return cmd_rescore(cfg)
                return cmd_score(cfg, db.connect(cfg.db_path), args.limit)
        elif args.command == "run":
            return cmd_run(cfg)
        elif args.command == "install-task":
            return cmd_install_task(cfg)
        elif args.command == "uninstall-task":
            return cmd_uninstall_task()
        elif args.command == "career-setup":
            return cmd_career_setup(cfg, args.recheck)
        elif args.command == "dashboard":
            return cmd_dashboard(cfg, args.port, not args.no_browser)
        elif args.command == "eval":
            return cmd_eval(cfg, args.changes, args.since, args.until, args.split, args.scores, args.by, args.funnel,
                            args.usage)
        elif args.command == "market":
            return cmd_market(cfg, args.regroup, args.since, args.until)
        elif args.command == "check-injection":
            return cmd_check_injection(cfg)
    except ConfigError as e:
        print(f"Konfiguration: {e}", file=sys.stderr)
        if args.command == "run":
            toast("JobSearch", f"Konfiguration: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
