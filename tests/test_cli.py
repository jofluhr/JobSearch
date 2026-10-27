import dataclasses
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from jobsearch.career import ScanCounts
from jobsearch import ai, db, usage
from jobsearch.cli import career_summary, cmd_daily, cmd_status
from jobsearch.config import ConfigError
from tests.conftest import TERMS, TermsAi, add_job

EXAMPLES = Path(__file__).parent.parent / "private.example"


class RecordingClient:
    """A Bundesagentur client that finds nothing and remembers the Search terms it was asked for."""

    def __init__(self):
        self.terms = []

    def search(self, term, **kwargs):
        self.terms.append(term)
        return iter([])

    def description(self, refnr):
        raise AssertionError("no Postings, so no descriptions")


@pytest.fixture
def cfg(cfg, tmp_path):
    shutil.copytree(EXAMPLES, cfg.private_dir)
    (cfg.private_dir / "search_terms.txt").write_text("# Manuell\nProjektleiter\n-Bauleiter\n", encoding="utf-8")
    profile = tmp_path / "elsewhere" / "profil.md"
    profile.parent.mkdir()
    profile.write_text("Projektplanung und Anlagenbau in Musterstadt.", encoding="utf-8")
    return dataclasses.replace(cfg, profile_path=profile)


def test_daily_run_searches_manual_and_derived_terms(cfg):
    client = RecordingClient()
    assert cmd_daily(cfg, score=False, career="off", client=client, ask=TermsAi()) == 0
    expected = ["Projektleiter"] + [t for t in TERMS if t != "Bauleiter"]
    assert list(dict.fromkeys(client.terms)) == expected


def test_failed_derivation_is_a_warning_and_the_manual_terms_still_run(cfg, capsys):
    client = RecordingClient()
    assert cmd_daily(cfg, score=False, career="off", client=client, ask=TermsAi(fail="Claude-Kontingent erschöpft")) == 0
    assert set(client.terms) == {"Projektleiter"}
    assert "Suchbegriffe aus dem Profil nicht abgeleitet: Claude-Kontingent erschöpft" in capsys.readouterr().err


def test_without_profile_only_the_manual_terms_run(cfg):
    client = RecordingClient()
    fake = TermsAi()
    cmd_daily(dataclasses.replace(cfg, profile_path=None), score=False, career="off", client=client, ask=fake)
    assert set(client.terms) == {"Projektleiter"} and fake.prompts == []


def test_failed_first_derivation_without_manual_terms_names_the_ai_error(cfg):
    (cfg.private_dir / "search_terms.txt").write_text("# leer\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Claude-Kontingent erschöpft"):
        cmd_daily(cfg, score=False, career="off", client=RecordingClient(), ask=TermsAi(fail="Claude-Kontingent erschöpft"))


def test_career_summary_names_dropped_postings_only_when_there_are_some():
    counts = ScanCounts(pages=3, unchanged=1, extracted=2, new_jobs=1)
    assert "verworfen" not in career_summary(counts)
    counts.ungrounded = 2
    assert career_summary(counts).endswith(", verworfen (nicht im Seitentext): 2")


def test_status_shows_the_ai_usage_of_the_last_seven_days(cfg, capsys):
    record = db.usage_recorder(db.connect(cfg.db_path))
    today = date.today()
    for day, cost in ((today, 0.04), (today - timedelta(days=6), 0.02), (today - timedelta(days=7), 5.0)):
        record(usage.CallUsage("scoring", "claude-test", 1, "ok", f"{day.isoformat()}T08:00:00", input_tokens=2,
                            output_tokens=400, cache_read_tokens=0, cache_creation_tokens=9000, cost_usd=cost))
    cmd_status(cfg)
    assert "KI-Verbrauch (7 Tage): 2 Aufrufe, API-Gegenwert 0.060 $" in capsys.readouterr().out


class DailyAi:
    """Answers the Search terms and the Weekly report; fails the report when asked to."""

    def __init__(self, report_fail=None):
        self.terms, self.report_fail = TermsAi(), report_fail
        self.purposes = []

    def __call__(self, prompt, *, purpose, model, schema, system, check=None, usage_recorder=None):
        self.purposes.append(purpose)
        if purpose == "search_terms":
            return self.terms(prompt, purpose=purpose, model=model, schema=schema, system=system, check=check)
        if self.report_fail:
            raise ai.AiError(self.report_fail)
        return ai.Answer({"summary": "Ruhige Woche.", "summary_evidence": ["stage.jobs.all"],
                          "recommendations": [], "new_levers": []}, "claude-test")


@pytest.fixture
def toasts(monkeypatch):
    shown = []
    monkeypatch.setattr("jobsearch.cli.toast", lambda title, body, on_click=None: shown.append((body, on_click)))
    return shown


def test_daily_run_makes_the_weekly_report_when_due_and_toasts_it(cfg, toasts):
    fake = DailyAi()
    assert cmd_daily(cfg, score=True, career="off", client=RecordingClient(), ask=fake) == 0
    assert fake.purposes.count("weekly_report") == 1
    [(body, on_click)] = [t for t in toasts if "Wochenbericht" in t[0]]
    assert "0 Empfehlungen" in body and on_click.endswith(".md")
    fake = DailyAi()
    cmd_daily(cfg, score=True, career="off", client=RecordingClient(), ask=fake)
    assert "weekly_report" not in fake.purposes  # not due again the same week


def test_a_failed_weekly_report_leaves_the_run_and_is_tried_again(cfg, toasts, capsys):
    assert cmd_daily(cfg, score=True, career="off", client=RecordingClient(),
                     ask=DailyAi(report_fail="Claude-Kontingent erschöpft")) == 0
    assert "Wochenbericht fehlgeschlagen" in capsys.readouterr().err
    assert (cfg.digests_dir / f"{date.today().isoformat()}.md").exists()
    fake = DailyAi()
    cmd_daily(cfg, score=True, career="off", client=RecordingClient(), ask=fake)
    assert fake.purposes.count("weekly_report") == 1


def test_no_weekly_report_without_scoring(cfg, toasts):
    fake = DailyAi()
    cmd_daily(cfg, score=False, career="off", client=RecordingClient(), ask=fake)
    assert "weekly_report" not in fake.purposes


def test_any_weekly_report_error_leaves_the_run_alone(cfg, toasts, capsys, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("Datei gesperrt")
    monkeypatch.setattr("jobsearch.report.make_report", broken)
    assert cmd_daily(cfg, score=True, career="off", client=RecordingClient(), ask=DailyAi()) == 0
    assert "Wochenbericht fehlgeschlagen" in capsys.readouterr().err


def test_status_counts_each_job_once_by_its_newest_score(cfg, capsys):
    conn = db.connect(cfg.db_path)
    job = add_job(conn, "Planer")
    with conn:
        for tier in (3, 1):
            conn.execute("""INSERT INTO fit_scores (job_id, tier, reasoning, profile_hash, model, scored_at)
                            VALUES (?, ?, 'Passt.', 'ppp', 'claude-test', '2026-10-16T09:00:00')""", (job, tier))
    cmd_status(cfg)
    assert "Fit scores: ★★★ 0, ★★ 0, ★ 1;" in capsys.readouterr().out


def test_daily_run_prints_the_full_text_lookup_in_german(cfg, capsys, monkeypatch):
    from jobsearch import cli
    from jobsearch.daily import Counts

    monkeypatch.setattr(cli, "run_daily", lambda *args, **kwargs: Counts(
        lookups=12, lookup_hits=4, lookup_known=1, lookup_dropped=2, lookup_failures=1))
    cmd_daily(cfg, score=False, career="off", client=RecordingClient(), ask=TermsAi())
    assert ("Volltext gesucht: 12, gefunden: 4, schon bekannt: 1, verworfen: 2, fehlgeschlagen: 1"
            in capsys.readouterr().out)


def test_daily_run_without_lookups_prints_no_lookup_line(cfg, capsys):
    cmd_daily(cfg, score=False, career="off", client=RecordingClient(), ask=TermsAi())
    assert "Volltext gesucht" not in capsys.readouterr().out
