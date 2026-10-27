import dataclasses
import sqlite3
import json
from collections import Counter
from datetime import date

import pytest

from jobsearch import db, usage
from jobsearch.cli import cmd_eval
from jobsearch.eligibility import NOT_FULLY_REMOTE_REASON
from jobsearch.config import ConfigError
from jobsearch.evaluation import (STAGES, Found, Period, changes, changes_report, describe, funnel_report, label, periods,
                                  read_found, reason_kind, score_report, score_tables, stage_metrics, stage_report, term_metrics,
                                  trace, usage_metrics, usage_report)
from jobsearch.scoring import AI_NOT_REMOTE_REASON

from .conftest import add_job

BASE = {"model_scoring": "sonnet", "prompt_scoring": "aaa", "profile": "ppp",
        "search_terms": ["Planer", "Projektleiter"], "alert_mail": True, "radius_km": 30}


def add_run(conn, started_at, settings, kind="daily"):
    with conn:
        conn.execute("INSERT INTO runs (kind, started_at, settings_json) VALUES (?, ?, ?)",
                     (kind, started_at, json.dumps(settings) if settings is not None else None))


def test_describe_names_each_changed_setting():
    new = {**BASE, "search_terms": ["Planer", "Disponent"], "prompt_scoring": "bbb", "radius_km": 40,
           "alert_mail": False, "profile": None}
    assert describe(BASE, new) == [
        "Prompt Bewertung: geändert",
        "Profil: fehlt",
        "Suchbegriffe: + Disponent, − Projektleiter",
        "Alert-Mail: an → aus",
        "Radius (km): 30 → 40",
    ]


def test_a_changed_report_prompt_has_a_german_name():
    assert describe(BASE, {**BASE, "prompt_weekly_report": "rrr"}) == ["Prompt Wochenbericht: vorhanden"]


def test_a_setting_added_later_shows_up_as_new():
    assert describe(BASE, {**BASE, "reopening_days": 21}) == ["Reopening nach (Tagen): – → 21"]


def test_only_runs_with_different_settings_are_changes():
    snaps = [("2026-10-15T08:00:00", BASE), ("2026-10-16T08:00:00", BASE),
             ("2026-10-17T08:00:00", {**BASE, "model_scoring": "opus"}), ("2026-10-18T08:00:00", {**BASE, "model_scoring": "opus"})]
    found = changes(snaps)
    assert [(c.day, c.lines) for c in found] == [
        ("2026-10-15", ["Beginn der Aufzeichnung"]),
        ("2026-10-17", ["Modell Bewertung: sonnet → opus"]),
    ]


def test_report_skips_runs_without_snapshot_and_other_kinds(conn):
    add_run(conn, "2026-10-14T08:00:00", None)  # from before the snapshot existed
    add_run(conn, "2026-10-15T08:00:00", BASE)
    add_run(conn, "2026-10-15T09:00:00", None, kind="score")
    add_run(conn, "2026-10-16T08:00:00", {**BASE, "radius_km": 25})
    report = changes_report(conn)
    assert report[1] == "  2026-10-15  Beginn der Aufzeichnung"
    assert report[2] == "  2026-10-16  Radius (km): 30 → 25"
    assert len(report) == 3


def test_report_without_snapshots_says_when_recording_starts(conn):
    assert "Die Aufzeichnung beginnt mit dem nächsten" in changes_report(conn)[0]


def test_report_counts_fit_scores_from_before_prompt_versions(conn):
    with conn:
        job = conn.execute("""INSERT INTO jobs (company, title, eligibility, first_seen, last_seen)
                              VALUES ('Muster GmbH', 'Planer', 'eligible', '2026-10-15', '2026-10-15')""").lastrowid
        conn.execute("""INSERT INTO fit_scores (job_id, tier, reasoning, profile_hash, model, scored_at)
                        VALUES (?, 2, 'Passt.', 'ppp', 'claude-test', '2026-10-15T09:00:00')""", (job,))
    assert "Fit scores ohne Prompt-Version: 1." in changes_report(conn)[-1]


def test_migration_keeps_existing_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = db.connect(path, run_migrations=False)
    for version, sql in db._migrations():
        if version <= 5:
            conn.executescript(sql)
    conn.execute("PRAGMA user_version = 5")
    conn.execute("INSERT INTO runs (kind, started_at) VALUES ('daily', '2026-10-14T08:00:00')")
    conn.commit()
    conn.close()
    conn = db.connect(path)
    row = conn.execute("SELECT kind, settings_json FROM runs").fetchone()
    assert (row["kind"], row["settings_json"]) == ("daily", None)
    assert "prompt_hash" in {r["name"] for r in conn.execute("PRAGMA table_info(fit_scores)")}
    conn.close()


def score(conn, job_id, tier, low=0):
    with conn:
        conn.execute("""INSERT INTO fit_scores (job_id, tier, reasoning, low_confidence, profile_hash, model, scored_at)
                        VALUES (?, ?, 'Passt.', ?, 'ppp', 'claude-test', '2026-10-16T09:00:00')""", (job_id, tier, low))


def found_by(conn, job_id, *terms):
    posting = conn.execute("SELECT id FROM postings WHERE job_id = ?", (job_id,)).fetchone()[0]
    with conn:
        conn.executemany("INSERT INTO posting_terms (posting_id, term) VALUES (?, ?)", [(posting, t) for t in terms])


def test_periods_split_into_before_and_after():
    assert periods(None, None, None) == [Period("gesamt")]
    assert periods("2026-10-01", None, "2026-10-10") == [Period("vor 2026-10-10", "2026-10-01", "2026-10-10"),
                                                         Period("ab 2026-10-10", "2026-10-10", None)]
    with pytest.raises(ValueError):
        periods("2026-10-10", None, "2026-10-01")


@pytest.mark.parametrize("reason, kind", [
    ("Entfernung 55 km", "Entfernung"),
    ("Ausschlussliste: Praktikum", "Ausschlussliste"),
    (NOT_FULLY_REMOTE_REASON, "nicht voll remote (Regel)"),
    (AI_NOT_REMOTE_REASON, "nicht voll remote (KI)"),
    (None, "ohne Grund"),
])
def test_reason_kinds(reason, kind):
    assert reason_kind(reason) == kind


def test_stage_metrics_count_each_stage_within_the_period(conn):
    a = add_job(conn, "Planer", first_seen="2026-10-15")
    b = add_job(conn, "Disponent", first_seen="2026-10-15")
    add_job(conn, "Unscored", first_seen="2026-10-15")
    add_job(conn, "Weit weg", eligibility="ineligible", reason="Entfernung 80 km", first_seen="2026-10-15")
    add_job(conn, "Praktikum", eligibility="ineligible", reason="Ausschlussliste: Praktikum", first_seen="2026-10-15")
    add_job(conn, "Später", first_seen="2026-10-20", source="indeed")
    score(conn, a, 3)
    score(conn, b, 1, low=1)
    with conn:
        conn.execute("UPDATE jobs SET is_generic = 1 WHERE id = ?", (b,))
    add_run(conn, "2026-10-15T08:00:00", BASE)
    with conn:
        conn.execute("UPDATE runs SET ok = 1")
    m = stage_metrics(conn, Period("x", "2026-10-15", "2026-10-16"))
    assert m.daily_runs == 1
    assert m.postings == {"bundesagentur": 5}
    assert m.jobs == 5 and m.eligibility == {"eligible": 3, "ineligible": 2}
    assert m.reasons == {"Entfernung": 1, "Ausschlussliste": 1}
    assert m.tiers == {3: 1, 1: 1} and m.scored == 2
    assert (m.low_confidence, m.generic, m.unscored) == (1, 1, 1)


def test_term_metrics_show_unique_finds_tiers_positives_and_terms_without_finds(conn):
    a = add_job(conn, "Planer")
    b = add_job(conn, "Projektplaner")
    add_job(conn, "Ohne Begriff")
    found_by(conn, a, "Planer", "Projekt")
    found_by(conn, b, "Projekt")
    score(conn, a, 3)
    with conn:
        conn.execute("UPDATE jobs SET status = 'interesting' WHERE id = ?", (a,))
    add_run(conn, "2026-10-15T08:00:00", {**BASE, "search_terms": ["Planer", "Projekt", "Leerlauf"]})
    terms = {t.term: t for t in term_metrics(conn, Period("gesamt"))}
    assert (terms["Projekt"].jobs, terms["Projekt"].only_here) == (2, 1)
    assert (terms["Planer"].jobs, terms["Planer"].only_here, terms["Planer"].positive) == (1, 0, 1)
    assert terms["Planer"].tiers == {3: 1}
    assert terms["Leerlauf"].jobs == 0
    assert [t.term for t in term_metrics(conn, Period("gesamt"))][0] == "Projekt"  # most Jobs first


def test_stage_report_has_one_column_per_period(conn):
    add_job(conn, "Alt", first_seen="2026-10-10")
    add_job(conn, "Neu", first_seen="2026-10-20")
    report = stage_report(conn, periods(None, None, "2026-10-15"))
    assert report[1].split() == ["vor", "2026-10-15", "ab", "2026-10-15"]
    assert next(line for line in report if line.startswith("Neue Jobs")).split() == ["Neue", "Jobs", "1", "1"]


def test_eval_command_rejects_a_split_outside_the_period(cfg, capsys):
    assert cmd_eval(cfg, False, since=date(2026, 10, 15), split=date(2026, 10, 1)) == 2
    assert "liegt nicht im Zeitraum" in capsys.readouterr().err
    assert cmd_eval(cfg, False, until=date(2026, 10, 15)) == 0


def scored_job(conn, title, tier, status="new", prompt_hash=None, scored_at="2026-10-16T09:00:00",
               first_seen="2026-10-15"):
    job = add_job(conn, title, first_seen=first_seen)
    with conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job))
        conn.execute("""INSERT INTO fit_scores (job_id, tier, reasoning, profile_hash, prompt_hash, model, scored_at)
                        VALUES (?, ?, 'Passt.', 'ppp', ?, 'claude-test', ?)""", (job, tier, prompt_hash, scored_at))
    return job


@pytest.mark.parametrize("status, expected", [
    ("interesting", "positive"), ("applied", "positive"), ("rejected", "positive"),
    ("not_interesting", "negative"), ("new", None),
])
def test_job_status_as_label(status, expected):
    assert label(status) == expected


def test_score_table_counts_tier_against_label(conn):
    scored_job(conn, "A", 3, "applied")
    scored_job(conn, "B", 3, "not_interesting")
    scored_job(conn, "C", 1, "interesting")
    scored_job(conn, "D", 1)
    assert score_tables(conn, Period("gesamt")) == {
        "alle": Counter({(3, "positive"): 1, (3, "negative"): 1, (1, "positive"): 1, (1, None): 1})}
    report = score_report(conn, [Period("gesamt")])
    assert "Treffsicherheit ★★★: 50 % (1 von 2)" in report
    assert "Übersehen in ★: 1 von 2 positiven" in report
    assert "Labels: 3 von 4 bewerteten Jobs" in report
    assert any(line.startswith("Wenige Labels") for line in report)


def test_scores_group_by_prompt_version_and_month(conn):
    scored_job(conn, "Alt", 2)
    scored_job(conn, "Neu", 2, prompt_hash="abcdef0123456789", scored_at="2026-11-02T09:00:00")
    assert set(score_tables(conn, Period("gesamt"), "prompt")) == {"Prompt vor der Aufzeichnung", "Prompt abcdef01"}
    assert set(score_tables(conn, Period("gesamt"), "month")) == {"2026-10", "2026-11"}


def test_score_report_without_scores_says_so(conn):
    assert score_report(conn, [Period("gesamt")]) == ["Im Zeitraum gibt es keine bewerteten Jobs."]


def write_found(path, *rows):
    lines = ["company,title,url,found_on"] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_found_by_url_reports_the_tier_and_status(conn):
    job = scored_job(conn, "Planer (m/w/d)", 2, "interesting")
    url = conn.execute("SELECT url FROM postings WHERE job_id = ?", (job,)).fetchone()[0]
    t = trace(conn, Found("Anders geschrieben", "Anderer Titel", url + "/"), None)
    assert (t.stage, t.detail) == ("tier2", "als 'Planer (m/w/d)', Status interessant")


def test_found_by_company_and_similar_title(conn):
    add_job(conn, "Data Analyst Vertrieb (m/w/d)", company="Example Tech Services AG")
    t = trace(conn, Found("Example Tech", "Data Analyst Vertrieb (w/m/d)"), None)
    assert t.stage == "waiting"


def test_found_but_ineligible_names_the_reason(conn):
    add_job(conn, "Werkstudent Planung", eligibility="ineligible", reason="Ausschlussliste: Werkstudent")
    t = trace(conn, Found("Muster GmbH", "Werkstudent Planung"), None)
    assert (t.stage, t.detail) == ("ineligible", "Ausschlussliste")


@pytest.mark.parametrize("found, stage", [
    (Found("Muster GmbH", "Vertriebsleiter"), "not_collected"),  # company known, this opening not
    (Found("Beispiel AG", "Planer"), "unknown_company"),
    (Found("Beispiel AG", "Planer", found_on="2026-10-01"), "before"),
    (Found("Beispiel AG", "Planer", found_on="2026-10-20"), "unknown_company"),
])
def test_not_collected_tells_known_from_unknown_companies_and_old_finds(conn, found, stage):
    add_job(conn, "Planer")
    assert trace(conn, found, "2026-10-15").stage == stage


def test_funnel_report_counts_stages_and_points_to_the_lever(conn, tmp_path):
    scored_job(conn, "Planer", 1)
    path = write_found(tmp_path / "found.csv", ("Muster GmbH", "Planer", "", ""), ("Beispiel AG", "Planer", "", ""))
    report = funnel_report(conn, path)
    assert report[0] == "Fremdfunde: 2 (aus found.csv)"
    assert any("Firma nie gesehen" in line and "→ Sources, Zielunternehmen" in line for line in report)
    assert any(line.split()[:2] == ["★", "1"] and "→ Prompt" in line for line in report)
    assert "  Beispiel AG – Planer: nicht gesammelt, Firma nie gesehen" in report


def test_funnel_with_an_empty_or_missing_file(conn, tmp_path):
    assert "Noch keine Fremdfunde" in funnel_report(conn, write_found(tmp_path / "found.csv"))[0]
    with pytest.raises(ConfigError):
        funnel_report(conn, tmp_path / "missing.csv")


def test_a_found_file_saved_with_bom_keeps_its_company_column(tmp_path):
    path = write_found(tmp_path / "found.csv", ("Muster GmbH", "Planer", "", ""))
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8-sig")
    assert read_found(path) == [Found("Muster GmbH", "Planer", "", "")]


# --- AI usage ------------------------------------------------------------------------------------

def add_call(conn, started_at, purpose="scoring", cost=0.04, attempt=1, model="claude-sonnet-test",
             outcome="ok", tokens=(2, 400, 1000, 9000), run_id=None):
    inp, out, read, write = tokens
    db.usage_recorder(conn, run_id)(usage.CallUsage(purpose, model, attempt, outcome, started_at, input_tokens=inp,
                                                  output_tokens=out, cache_read_tokens=read,
                                                  cache_creation_tokens=write, cost_usd=cost))


def test_usage_is_summed_per_purpose_within_the_period(conn):
    add_call(conn, "2026-10-14T08:00:00", cost=1.0)  # before the period
    add_call(conn, "2026-10-15T08:00:00", cost=0.04)
    add_call(conn, "2026-10-15T08:05:00", purpose="career_extract", cost=0.01, model="claude-haiku-test")
    add_call(conn, "2026-10-16T08:00:00", cost=0.06, attempt=2, outcome="ok")
    m = usage_metrics(conn, Period("x", "2026-10-15"))
    scoring = m.by_purpose["scoring"]
    assert (scoring.calls, scoring["output_tokens"], scoring["cache_creation_tokens"]) == (2, 800, 18000)
    assert scoring.cost_usd == pytest.approx(0.10)
    assert m.by_purpose["career_extract"].calls == 1
    assert set(m.by_day) == {"2026-10-15", "2026-10-16"}
    assert set(m.by_model) == {"claude-sonnet-test", "claude-haiku-test"}
    assert m.total.cost_usd == pytest.approx(0.11)


def add_tier(conn, tier, scored_at):
    job = add_job(conn, f"Planer {tier} {scored_at}")
    with conn:
        conn.execute("""INSERT INTO fit_scores (job_id, tier, reasoning, profile_hash, model, scored_at)
                        VALUES (?, ?, 'Passt.', 'ppp', 'claude-test', ?)""", (job, tier, scored_at))


def test_api_equivalent_per_job_and_page_counts_only_what_happened_since_recording_began(conn):
    add_tier(conn, 3, "2026-10-14T08:00:00")  # scored before the first recorded call: free, so left out
    add_call(conn, "2026-10-15T08:00:00", cost=0.30)
    add_call(conn, "2026-10-15T08:01:00", purpose="search_terms", cost=0.10)
    add_call(conn, "2026-10-15T09:00:00", purpose="career_extract", cost=0.08)
    for tier in (3, 2, 1, 1, 1, 1):
        add_tier(conn, tier, "2026-10-15T08:02:00")
    add_run(conn, "2026-10-13T09:00:00", None, kind="career_scan")  # before recording
    add_run(conn, "2026-10-15T09:00:00", None, kind="career_scan")
    with conn:
        conn.execute("UPDATE runs SET counts_json = ? WHERE kind = 'career_scan'", (json.dumps({"extracted": 4}),))
    m = usage_metrics(conn, Period("gesamt"))
    assert m.recording_since == "2026-10-15"
    assert (m.scored, m.good, m.pages) == (6, 2, 4)
    assert m.per_scored == pytest.approx(0.05)  # scoring calls only
    assert m.per_good == pytest.approx(0.24)  # every purpose: what a good match costs
    assert m.per_page == pytest.approx(0.02)


def test_api_equivalent_per_job_is_none_without_jobs(conn):
    add_call(conn, "2026-10-15T08:00:00")
    m = usage_metrics(conn, Period("gesamt"))
    assert (m.per_scored, m.per_good, m.per_page) == (None, None, None)


def test_retry_share_and_fixed_overhead_per_purpose(conn):
    add_call(conn, "2026-10-15T08:00:00", outcome="invalid", tokens=(2, 400, 0, 9000))
    add_call(conn, "2026-10-15T08:01:00", attempt=2, tokens=(2, 400, 8000, 1000))
    add_call(conn, "2026-10-16T08:00:00", tokens=(2, 400, 6000, 0))
    add_call(conn, "2026-10-16T08:05:00", outcome="timeout", tokens=(None, None, None, None))  # no envelope
    m = usage_metrics(conn, Period("gesamt"))
    assert m.retries["scoring"] == 1
    assert m.cache_per_call["scoring"] == pytest.approx(8000)  # cache tokens per call that reported any


def test_usage_report_puts_the_periods_side_by_side_and_names_its_limits(conn):
    add_call(conn, "2026-10-15T08:00:00", cost=0.30)
    add_call(conn, "2026-10-17T08:00:00", purpose="career_extract", cost=0.02, model="claude-haiku-test")
    add_tier(conn, 3, "2026-10-17T08:02:00")
    lines = usage_report(conn, periods(None, None, "2026-10-16"))
    text = "\n".join(lines)
    assert "vor 2026-10-16" in text and "ab 2026-10-16" in text
    assert "API-Gegenwert (Listenpreis)" in text
    assert "kein echter Preis" in text and "Kontingent" in text
    assert "gezählt ab 2026-10-15" in text
    assert "Karriereseiten auslesen" in text and "claude-haiku-test" in text


def test_usage_report_without_recorded_calls(conn):
    assert "Noch keine KI-Aufrufe aufgezeichnet" in "\n".join(usage_report(conn, periods(None, None, None)))


def test_eval_usage_command(cfg, capsys):
    add_call(db.connect(cfg.db_path), "2026-10-15T08:00:00")
    assert cmd_eval(cfg, changes=False, usage=True) == 0
    assert "API-Gegenwert" in capsys.readouterr().out


def test_scores_earlier_on_the_day_recording_began_are_left_out(conn):
    add_tier(conn, 3, "2026-10-15T07:00:00")  # the morning run, before the first recorded call
    add_run(conn, "2026-10-15T07:00:00", None, kind="career_scan")
    with conn:
        conn.execute("UPDATE runs SET counts_json = ? WHERE kind = 'career_scan'", (json.dumps({"extracted": 4}),))
    add_call(conn, "2026-10-15T11:00:00", cost=0.04)
    add_tier(conn, 2, "2026-10-15T11:00:05")
    m = usage_metrics(conn, Period("gesamt"))
    assert (m.scored, m.good, m.pages) == (1, 1, 0)
    assert m.per_scored == pytest.approx(0.04)


def test_a_row_with_only_one_cache_count_still_counts_for_the_overhead(conn):
    add_call(conn, "2026-10-15T08:00:00", tokens=(2, 400, 6000, None))
    assert usage_metrics(conn, Period("gesamt")).cache_per_call["scoring"] == pytest.approx(6000)


def test_ai_calls_has_a_column_for_every_call_usage_field(conn):
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(ai_calls)")}
    assert columns == {f.name for f in dataclasses.fields(usage.CallUsage)} | {"id", "run_id"}


def test_migration_007_adds_ai_calls_to_an_existing_database(tmp_path):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    for version, sql in db._migrations():
        if version <= 6:
            old.executescript(sql)
    old.execute("PRAGMA user_version = 6")
    old.execute("INSERT INTO runs (kind, started_at) VALUES ('daily', '2026-10-14T08:00:00')")
    old.commit()
    old.close()
    conn = db.connect(path)
    add_call(conn, "2026-10-15T08:00:00", run_id=1)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert conn.execute("SELECT run_id FROM ai_calls").fetchone()[0] == 1


def rescored_job(conn, title="Koch", status="new", first_seen="2026-10-15"):
    """A Job scored ★★★ under an old prompt and later rescored to a Sorted out ★."""
    job = scored_job(conn, title, 3, status, prompt_hash="aaaaaaaa00000000", first_seen=first_seen)
    with conn:
        conn.execute("""INSERT INTO fit_scores (job_id, tier, reasoning, sorted_out, profile_hash, prompt_hash, model,
                                                scored_at)
                        VALUES (?, 1, 'Anderes Feld.', 1, 'ppp', 'bbbbbbbb00000000', 'claude-test',
                                '2026-10-20T09:00:00')""", (job,))
    return job


def test_a_rescored_job_counts_once_with_its_newest_score(conn):
    job = rescored_job(conn)
    found_by(conn, job, "Planer")
    m = stage_metrics(conn, Period("gesamt"))
    assert (m.scored, m.tiers[1], m.tiers[3], m.sorted_out) == (1, 1, 0, 1)
    [t] = term_metrics(conn, Period("gesamt"))
    assert (t.jobs, t.only_here, dict(t.tiers)) == (1, 1, {1: 1})
    assert score_tables(conn, Period("gesamt")) == {"alle": Counter({(1, None): 1})}
    by_prompt = score_tables(conn, Period("gesamt"), "prompt")
    assert by_prompt == {"Prompt aaaaaaaa": Counter({(3, None): 1}), "Prompt bbbbbbbb": Counter({(1, None): 1})}


def test_the_stage_report_shows_the_sorted_out_share_of_tier_1(conn):
    rescored_job(conn)
    scored_job(conn, "Sachbearbeiter", 1)
    assert any(line.split() == ["davon", "aussortiert", "1", "(50", "%)"] for line in stage_report(conn, [Period("gesamt")]))


def test_a_found_elsewhere_opening_that_was_sorted_out_names_that_stage(conn, tmp_path):
    job = rescored_job(conn, "Planer (m/w/d)")
    url = conn.execute("SELECT url FROM postings WHERE job_id = ?", (job,)).fetchone()[0]
    assert trace(conn, Found("Muster GmbH", "Planer", url), None).stage == "sorted_out"
    report = funnel_report(conn, write_found(tmp_path / "found.csv", ("Muster GmbH", "Planer", url, "")))
    assert any("aussortiert" in line and "1  → Prompt, Profil, Feedback notes" in line for line in report)
    assert len(report) == 1 + len(STAGES) + 3


def test_api_equivalent_per_scored_job_leaves_out_rescored_rows(conn):
    add_call(conn, "2026-10-15T08:00:00", cost=0.30)
    add_call(conn, "2026-10-16T08:00:00", purpose="rescoring", cost=0.50)
    for tier in (1, 1, 2):
        add_tier(conn, tier, "2026-10-15T08:02:00")
    with conn:  # a later rescore of the first Job, paid by the rescoring call
        conn.execute("""INSERT INTO fit_scores (job_id, tier, reasoning, profile_hash, model, scored_at)
                        SELECT job_id, 3, 'Neu.', 'ppp', 'claude-test', '2026-10-16T08:01:00' FROM fit_scores
                        ORDER BY id LIMIT 1""")
    m = usage_metrics(conn, Period("gesamt"))
    assert (m.scored, m.good) == (3, 1)
    assert m.per_scored == pytest.approx(0.10)


def add_full_text(conn, job_id, source_id, found_by_lookup, description="Volltext."):
    raw = json.dumps({"found_by": "lookup"}) if found_by_lookup else None
    with conn:
        conn.execute(
            """INSERT INTO postings (job_id, source, source_id, title, company, description, is_snippet, raw_json,
                                     first_seen, last_seen)
               VALUES (?, 'bundesagentur', ?, 'Planer', 'Muster GmbH', ?, 0, ?, '2026-10-15', '2026-10-15')""",
            (job_id, source_id, description, raw))


def test_stage_metrics_tell_indeed_full_texts_from_the_lookup_and_from_elsewhere(conn):
    by_lookup = add_job(conn, "Planer A", snippet=True, source="indeed")
    elsewhere = add_job(conn, "Planer B", snippet=True, source="indeed")
    no_text = add_job(conn, "Planer C", snippet=True, source="indeed")
    add_job(conn, "Planer D", snippet=True, source="indeed")
    add_job(conn, "Nur Bundesagentur")
    add_full_text(conn, by_lookup, "R-1", True)
    add_full_text(conn, elsewhere, "R-2", False)
    add_full_text(conn, no_text, "R-3", True, description=None)  # a partner portal offer has no text
    m = stage_metrics(conn, Period("gesamt"))
    assert (m.indeed_jobs, m.indeed_full_text_lookup, m.indeed_full_text_other) == (4, 1, 1)
    report = stage_report(conn, [Period("gesamt")])
    assert next(line for line in report if "per Lookup" in line).split()[-3:] == ["1", "(25", "%)"]
    assert next(line for line in report if "anderweitig" in line).split()[-3:] == ["1", "(25", "%)"]
