import dataclasses
import json
import logging
from datetime import date

import pytest

from jobsearch import ai, db, digest, notify, prompts, usage
from jobsearch.eligibility import HOMEOFFICE_REASON, INDEED_REMOTE_HINT
from jobsearch.scoring import AI_NOT_REMOTE_REASON, run_scoring

from .conftest import FakeAi, add_job, ai_calls

TODAY = date.today().isoformat()


@pytest.fixture
def cfg(cfg, tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("# Profil\nBeispielkenntnisse: Projektplanung, Datenanalyse.", encoding="utf-8")
    return dataclasses.replace(cfg, profile_path=profile)


def test_scores_newest_first_up_to_the_cap(conn, cfg):
    old = add_job(conn, "Alt", first_seen="2026-10-01")
    new = add_job(conn, "Neu", first_seen="2026-10-15")
    add_job(conn, "Raus", eligibility="ineligible", reason="Entfernung 80 km")
    fake = FakeAi()
    counts = run_scoring(conn, dataclasses.replace(cfg, max_scored_per_run=1), ask=fake)
    assert counts.scored == 1 and counts.backlog == 1
    assert conn.execute("SELECT job_id FROM fit_scores").fetchone()[0] == new
    counts = run_scoring(conn, cfg, ask=fake)
    assert counts.scored == 1 and counts.backlog == 0
    assert {r[0] for r in conn.execute("SELECT job_id FROM fit_scores")} == {old, new}
    assert run_scoring(conn, cfg, ask=fake).scored == 0  # already scored Jobs are left alone
    assert len(fake.prompts) == 2


def test_prompt_contains_profile_notes_and_jobs(conn, cfg):
    job = add_job(conn, "Projektingenieur")
    with conn:
        conn.execute("INSERT INTO feedback_notes (job_id, status, note, created_at) VALUES (?, 'not_interesting', "
                     "'Zu viel Reisetätigkeit', '2026-10-14T10:00:00')", (job,))
    fake = FakeAi()
    run_scoring(conn, cfg, ask=fake)
    prompt = fake.prompts[0]
    assert "Beispielkenntnisse" in prompt
    assert "Zu viel Reisetätigkeit" in prompt
    assert '"title": "Projektingenieur"' in prompt


def test_stored_score(conn, cfg):
    job = add_job(conn, "Projektingenieur")
    run_scoring(conn, cfg, ask=FakeAi(tier=3, remote="hybrid"))
    score = dict(conn.execute("SELECT * FROM fit_scores").fetchone())
    assert (score["job_id"], score["tier"], score["model"], score["low_confidence"]) == (job, 3, "claude-test", 0)
    assert json.loads(score["missing_skills"]) == ["Tabulix"]
    assert len(score["profile_hash"]) == 16
    assert conn.execute("SELECT remote_type FROM jobs").fetchone()[0] == "hybrid"


def test_missing_description_or_snippet_is_low_confidence(conn, cfg):
    add_job(conn, "Ohne Text", description=None)
    add_job(conn, "Nur Snippet", snippet=True)
    counts = run_scoring(conn, cfg, ask=FakeAi())
    assert counts.low_confidence == 2


def test_remote_candidates_are_settled_by_the_ai(conn, cfg):
    reason = f"{HOMEOFFICE_REASON} (Hinweis: '100% remote')"
    full = add_job(conn, "Remote A", eligibility="unclear", reason=reason)
    run_scoring(conn, cfg, ask=FakeAi(remote="full"))
    hybrid = add_job(conn, "Remote B", eligibility="unclear", reason=reason)
    add_job(conn, "Ohne Beschreibung", eligibility="unclear", reason=HOMEOFFICE_REASON, description=None)
    counts = run_scoring(conn, cfg, ask=FakeAi(remote="hybrid"))
    assert counts.scored == 1 and counts.remote_rejected == 1
    rows = {r["id"]: (r["eligibility"], r["ineligible_reason"]) for r in conn.execute("SELECT * FROM jobs")}
    assert rows[full] == ("eligible", None)
    assert rows[hybrid] == ("ineligible", AI_NOT_REMOTE_REASON)


@pytest.mark.parametrize("remote, expected", [("unknown", "eligible"), ("full", "eligible"), ("hybrid", "ineligible")])
def test_a_remote_alert_job_stays_remote_unless_the_ai_says_otherwise(conn, cfg, remote, expected):
    reason = f"{HOMEOFFICE_REASON} (Hinweis: '{INDEED_REMOTE_HINT}')"
    job = add_job(conn, "Remote C", eligibility="unclear", reason=reason, snippet=True, source="indeed")
    run_scoring(conn, cfg, ask=FakeAi(remote=remote))
    assert conn.execute("SELECT eligibility FROM jobs WHERE id = ?", (job,)).fetchone()[0] == expected


def test_missing_profile_is_recorded_like_an_ai_failure(conn, cfg, tmp_path):
    add_job(conn, "Sachbearbeiter")
    counts = run_scoring(conn, dataclasses.replace(cfg, profile_path=tmp_path / "weg.md"), ask=FakeAi())
    assert "Profil nicht gefunden" in counts.error and counts.backlog == 1
    assert conn.execute("SELECT ok FROM runs").fetchone()[0] == 0


def test_ai_failure_keeps_jobs_unscored_and_is_recorded(conn, cfg):
    add_job(conn, "Sachbearbeiter")
    counts = run_scoring(conn, cfg, ask=FakeAi(fail="Claude-Kontingent erschöpft"))
    assert counts.error == "Claude-Kontingent erschöpft" and counts.backlog == 1
    assert conn.execute("SELECT count(*) FROM fit_scores").fetchone()[0] == 0
    run = conn.execute("SELECT kind, ok, error FROM runs").fetchone()
    assert tuple(run) == ("score", 0, "Claude-Kontingent erschöpft")


def test_wrong_job_ids_are_rejected():
    from jobsearch.scoring import check_answer
    with pytest.raises(ai.InvalidAnswer, match="fehlende Jobs"):
        check_answer({1, 2})({"scores": [{"job_id": 1}]})
    with pytest.raises(ai.InvalidAnswer, match="mehrfach"):
        check_answer({1})({"scores": [{"job_id": 1}, {"job_id": 1}]})


def test_digest_and_toast(conn, cfg, tmp_path):
    add_job(conn, "Projektingenieur", first_seen=TODAY)
    add_job(conn, "Wartet", first_seen="2026-01-01")
    counts = run_scoring(conn, dataclasses.replace(cfg, max_scored_per_run=1), ask=FakeAi(tier=3))
    path, report = digest.write_digest(conn, tmp_path / "digests", date.today(), counts)
    text = path.read_text(encoding="utf-8")
    assert "## ★★★ Sehr passend (1)" in text
    assert "[Projektingenieur](https://x/" in text
    assert "**Fehlt:** Tabulix" in text
    assert "Warten auf Bewertung: 1." in text
    assert notify.message(report, counts) == ("JobSearch", "Neu: 1× ★★★\n1 Jobs warten auf Bewertung")


def test_no_toast_when_nothing_new(conn, cfg, tmp_path):
    add_job(conn, "Projektingenieur", first_seen=TODAY)
    counts = run_scoring(conn, cfg, ask=FakeAi(tier=1))
    _, report = digest.write_digest(conn, tmp_path, date.today(), counts)
    assert notify.message(report, counts) is None


def test_collection_warnings_get_a_toast_even_without_new_jobs(conn, cfg, tmp_path):
    counts = run_scoring(conn, cfg, ask=FakeAi())
    _, report = digest.write_digest(conn, tmp_path, date.today(), counts)
    assert notify.message(report, counts, ["Fehler beim Sammeln: Suche 'x'"]) == (
        "JobSearch", "1 Warnung(en), Details im Digest")


def test_each_fit_score_stores_the_prompt_version(conn, cfg):
    add_job(conn, "Sachbearbeiter")
    run_scoring(conn, cfg, ask=FakeAi())
    assert conn.execute("SELECT prompt_hash FROM fit_scores").fetchone()[0] == prompts.version("scoring.md")


def test_a_job_flagged_for_injection_is_scored_counted_and_logged(conn, cfg, caplog):
    flagged = add_job(conn, "Gärtnermeister", first_seen=TODAY)
    add_job(conn, "Konditor", first_seen=TODAY)
    with caplog.at_level(logging.WARNING, logger="jobsearch.scoring"):
        counts = run_scoring(conn, cfg, ask=FakeAi(suspected={flagged}))
    assert counts.injection_suspected == 1 and counts.scored == 2
    assert any(f"Job {flagged}" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_the_digest_warns_once_when_scoring_flagged_something(conn, cfg, tmp_path):
    flagged = add_job(conn, "Gärtnermeister", first_seen=TODAY)
    counts = run_scoring(conn, cfg, ask=FakeAi(suspected={flagged}))
    text = digest.write_digest(conn, tmp_path / "digests", date.today(), counts)[0].read_text(encoding="utf-8")
    assert text.count("Anweisungen an die KI") == 1
    clean = run_scoring(conn, cfg, ask=FakeAi())
    assert "Anweisungen an die KI" not in digest.render(digest.DayReport(date.today(), {1: [], 2: [], 3: []}, [], 0),
                                                         clean)


def test_ai_usage_is_recorded_with_the_score_run(conn, cfg):
    add_job(conn, "Projektleiter")
    run_scoring(conn, cfg, ask=FakeAi())
    run_id = conn.execute("SELECT id FROM runs WHERE kind = 'score'").fetchone()[0]
    assert ai_calls(conn) == [("scoring", run_id, "ok")]


def test_digest_names_the_ai_usage_of_the_day(conn, tmp_path):
    record = db.usage_recorder(conn)
    for started_at, cost in (("2026-10-14T08:00:00", 9.0), ("2026-10-15T08:00:00", 0.04),
                             ("2026-10-15T09:00:00", 0.02)):
        record(usage.CallUsage("scoring", "claude-test", 1, "ok", started_at, input_tokens=2, output_tokens=400,
                            cache_read_tokens=1000, cache_creation_tokens=9000, cost_usd=cost))
    text = digest.write_digest(conn, tmp_path, date(2026, 10, 15))[0].read_text(encoding="utf-8")
    assert ("KI-Verbrauch heute: 2 Aufrufe, API-Gegenwert 0.060 $ (Tokens: Eingabe 4, Ausgabe 800, Cache gelesen 2.000, "
            "Cache geschrieben 18.000)") in text


def test_digest_without_ai_calls_has_no_usage_line(conn, tmp_path):
    assert "KI-Verbrauch" not in digest.write_digest(conn, tmp_path, date(2026, 10, 15))[0].read_text(encoding="utf-8")


def _answer(tier, sorted_out):
    return {"scores": [{"job_id": 1, "tier": tier, "sorted_out": sorted_out}]}


def test_sorted_out_is_only_allowed_with_tier_1():
    from jobsearch.scoring import check_answer
    for tier in (2, 3):
        with pytest.raises(ai.InvalidAnswer, match="aussortiert"):
            check_answer({1})(_answer(tier, True))
    check_answer({1})(_answer(1, True))
    check_answer({1})(_answer(3, False))


def test_a_sorted_out_job_is_stored_with_its_score_and_counted(conn, cfg):
    off = add_job(conn, "Koch")
    kept = add_job(conn, "Sachbearbeiter")
    counts = run_scoring(conn, cfg, ask=FakeAi(tier=1, sorted_out={off}))
    assert counts.scored == 2 and counts.sorted_out == 1
    rows = dict(conn.execute("SELECT job_id, sorted_out FROM fit_scores").fetchall())
    assert rows == {off: 1, kept: 0}
    run = conn.execute("SELECT counts_json FROM runs WHERE kind = 'score'").fetchone()[0]
    assert json.loads(run)["sorted_out"] == 1


def test_the_migration_keeps_old_fit_scores_not_sorted_out(tmp_path):
    path = tmp_path / "old.db"
    old = db.connect(path, run_migrations=False)
    for version, sql in db._migrations():
        if version <= 9:
            old.executescript(sql)
    old.execute("PRAGMA user_version = 9")
    old.execute("INSERT INTO jobs (company, title, eligibility, first_seen, last_seen) "
                "VALUES ('Muster GmbH', 'Alt', 'eligible', '2026-10-01', '2026-10-01')")
    old.execute("INSERT INTO fit_scores (job_id, tier, reasoning, profile_hash, model, scored_at) "
                "VALUES (1, 1, 'Alt.', 'p', 'm', '2026-10-01T08:00:00')")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT sorted_out FROM fit_scores").fetchone()[0] == 0
    conn.close()


def test_the_digest_names_the_sorted_out_jobs_of_the_day(conn, cfg, tmp_path):
    off = add_job(conn, "Koch", first_seen=TODAY)
    add_job(conn, "Sachbearbeiter", first_seen=TODAY)
    counts = run_scoring(conn, cfg, ask=FakeAi(tier=1, sorted_out={off}))
    text = digest.write_digest(conn, tmp_path, date.today(), counts)[0].read_text(encoding="utf-8")
    assert "Von der KI aussortiert: 1" in text
    assert "[Sachbearbeiter]" in text and "[Koch]" not in text
    assert "2 neu bewertete Jobs" in text
    other_day = digest.write_digest(conn, tmp_path, date(2026, 1, 1))[0].read_text(encoding="utf-8")
    assert "aussortiert" not in other_day
