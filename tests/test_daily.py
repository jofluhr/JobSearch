import json
from datetime import date

import pytest

from jobsearch import daily, prompts
from jobsearch.companies import TargetCompany, normalize_company
from jobsearch.daily import remote_window_days, run_daily
from jobsearch.eligibility import INDEED_REMOTE_HINT, Rules
from jobsearch.sources import Location, Posting
from jobsearch.sources.indeed_mail import Mailbox

from .test_indeed_mail import INDEED, FakeImap

RULES = Rules(30, 70, ("Werkstudent",))
TARGETS = [TargetCompany("Example Tech AG", (normalize_company("Example Tech"),), "Kassel", "", "", "")]
KASSEL = Location("34117 Kassel", 51.315, 9.49)
GOETTINGEN = Location("37073 Göttingen", 51.5413, 9.9158)
HAMBURG = Location("20095 Hamburg", 53.5503, 10.0006)


def posting(n, title, company="Muster Industrie GmbH", loc=KASSEL, homeoffice=False):
    return Posting("bundesagentur", f"ID-{n}", title, company, url=f"https://x/{n}",
                   locations=[loc], homeoffice_possible=homeoffice)


class FakeClient:
    def __init__(self, radius, nationwide, descriptions=None):
        self.radius, self.nationwide = radius, nationwide
        self.descriptions = descriptions or {}
        self.windows = []
        self.description_calls = []

    def search(self, term, *, location=None, radius_km=None, published_within_days=None):
        if location:
            return iter(self.radius)
        self.windows.append(published_within_days)
        return iter(self.nationwide)

    def description(self, refnr):
        self.description_calls.append(refnr)
        return self.descriptions.get(refnr, f"Beschreibung {refnr}")


def jobs(conn):
    return {r["title"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}


def test_daily_run_stores_and_classifies(conn, cfg):
    client = FakeClient(
        radius=[
            posting(1, "Projektingenieur"),
            posting(2, "Werkstudent Datenanalyse"),
            posting(3, "Entwicklungsingenieur", company="Example Tech AG", loc=GOETTINGEN),
            posting(4, "Konstrukteur", loc=GOETTINGEN),
        ],
        nationwide=[
            posting(5, "Softwareentwickler remote", loc=HAMBURG, homeoffice=True),
            posting(6, "Softwareentwickler vor Ort", loc=HAMBURG, homeoffice=False),
            posting(7, "Softwareentwickler hybrid", loc=HAMBURG, homeoffice=True),
        ],
        descriptions={"ID-5": "Sie arbeiten 100% remote.", "ID-7": "2 Tage Homeoffice pro Woche."},
    )
    counts = run_daily(conn, cfg, client, TARGETS, RULES, ["Ingenieur"], today=date(2026, 10, 15))

    stored = jobs(conn)
    assert stored["Projektingenieur"]["eligibility"] == "eligible"
    assert stored["Werkstudent Datenanalyse"]["eligibility"] == "ineligible"
    assert stored["Entwicklungsingenieur"]["eligibility"] == "eligible"  # Target company, about 40 km
    assert stored["Entwicklungsingenieur"]["is_target_company"] == 1
    assert stored["Konstrukteur"]["eligibility"] == "ineligible"
    assert stored["Softwareentwickler remote"]["eligibility"] == "unclear"  # the AI confirms later
    assert stored["Softwareentwickler hybrid"]["eligibility"] == "ineligible"
    assert "Softwareentwickler vor Ort" not in stored  # nationwide without home office is dropped
    assert counts.new_jobs == 6
    assert counts.remote_dropped == 1
    # Descriptions only for Jobs in reach
    assert sorted(client.description_calls) == ["ID-1", "ID-3", "ID-5", "ID-7"]


def test_second_run_same_day_adds_nothing(conn, cfg):
    client = FakeClient([posting(1, "Projektingenieur")], [])
    run_daily(conn, cfg, client, TARGETS, RULES, ["Ingenieur"], today=date(2026, 10, 15))
    counts = run_daily(conn, cfg, client, TARGETS, RULES, ["Ingenieur"], today=date(2026, 10, 15))
    assert counts.new_jobs == 0
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert client.description_calls == ["ID-1"]


def test_seen_again_updates_last_seen(conn, cfg):
    client = FakeClient([posting(1, "Projektingenieur")], [])
    run_daily(conn, cfg, client, TARGETS, RULES, ["Ingenieur"], today=date(2026, 10, 15))
    run_daily(conn, cfg, client, TARGETS, RULES, ["Ingenieur"], today=date(2026, 10, 18))
    job = jobs(conn)["Projektingenieur"]
    assert (job["first_seen"], job["last_seen"]) == ("2026-10-15", "2026-10-18")


def test_failed_description_is_retried_next_run(conn, cfg):
    client = FakeClient([posting(1, "Projektingenieur")], [], descriptions={"ID-1": None})
    counts = run_daily(conn, cfg, client, TARGETS, RULES, ["Ingenieur"], today=date(2026, 10, 15))
    assert counts.description_failures == 1
    client.descriptions = {}
    counts = run_daily(conn, cfg, client, TARGETS, RULES, ["Ingenieur"], today=date(2026, 10, 16))
    assert counts.descriptions_fetched == 1


def test_remote_window_catches_up_missed_days(conn, cfg):
    assert remote_window_days(conn, date(2026, 10, 15), 14) == 14  # first run ever
    conn.execute("INSERT INTO runs (kind, started_at, ok) VALUES ('daily', '2026-10-11T08:00:00', 1)")
    conn.execute("INSERT INTO runs (kind, started_at, ok) VALUES ('daily', '2026-10-14T08:00:00', 0)")
    assert remote_window_days(conn, date(2026, 10, 15), 14) == 5  # failed run does not count
    assert remote_window_days(conn, date(2026, 10, 11), 14) == 1
    assert remote_window_days(conn, date(2026, 12, 1), 14) == 14


class TermClient(FakeClient):
    """Returns different radius results per Search term."""

    def __init__(self, by_term):
        super().__init__(radius=[], nationwide=[])
        self.by_term = by_term

    def search(self, term, *, location=None, radius_km=None, published_within_days=None):
        if location:
            return iter(self.by_term.get(term, []))
        return iter([])


def test_search_terms_are_recorded_per_posting(conn, cfg):
    shared = posting(1, "Projektingenieur")
    client = TermClient({"Ingenieur": [shared, posting(2, "Entwicklungsingenieur")], "Projekt": [shared]})
    run_daily(conn, cfg, client, TARGETS, RULES, ["Ingenieur", "Projekt"], today=date(2026, 10, 15))
    run_daily(conn, cfg, client, TARGETS, RULES, ["Ingenieur", "Projekt"], today=date(2026, 10, 16))  # no duplicates
    rows = conn.execute(
        """SELECT p.source_id, t.term FROM posting_terms t JOIN postings p ON p.id = t.posting_id
           ORDER BY p.source_id, t.term"""
    ).fetchall()
    assert [tuple(r) for r in rows] == [("ID-1", "Ingenieur"), ("ID-1", "Projekt"), ("ID-2", "Ingenieur")]


def test_daily_run_stores_what_was_in_effect(conn, cfg):
    run_daily(conn, cfg, FakeClient([], []), TARGETS, RULES, ["Planer", "anlagenbau"], today=date(2026, 10, 15))
    settings = json.loads(conn.execute("SELECT settings_json FROM runs WHERE kind = 'daily'").fetchone()[0])
    assert settings["search_terms"] == ["anlagenbau", "Planer"]
    assert settings["prompt_scoring"] == prompts.version("scoring.md")
    assert settings["prompt_weekly_report"] == prompts.version("weekly_report.md")
    assert settings["profile"] is None  # no Profile configured, the run still works
    assert settings["radius_km"] == 30 and settings["alert_mail"] is False
    assert "Werkstudent" not in json.dumps(settings)  # the exclusion list is kept as a hash only


def test_a_posting_with_a_script_link_is_kept_without_the_link(conn, cfg):
    bad = posting(1, "Landschaftsgärtner")
    bad.url = "javascript:alert(document.cookie)"
    run_daily(conn, cfg, FakeClient(radius=[bad], nationwide=[]), TARGETS, RULES, ["Gärtner"],
              today=date(2026, 10, 15))
    assert [tuple(r) for r in conn.execute("SELECT title, url FROM postings")] == [("Landschaftsgärtner", None)]


# Full-text lookup for Indeed Jobs

class LookupClient(FakeClient):
    """Finds nothing by Search term. A lookup returns the BA Postings given for the looked-up title."""

    def __init__(self, hits=None, descriptions=None):
        super().__init__(radius=[], nationwide=[], descriptions=descriptions)
        self.hits = hits or {}
        self.lookups = []

    def lookup(self, title, *, location=None, radius_km=None):
        self.lookups.append((title, location))
        return list(self.hits.get(title, []))


def indeed(n, title, company="Muster Industrie GmbH", loc=KASSEL):
    return Posting("indeed", f"jk-{n}", title, company, url=f"https://indeed.example/viewjob?jk={n}",
                   locations=[loc] if loc else [], homeoffice_possible=loc is None,
                   description="Kurzer Ausschnitt.", is_snippet=True)


def run_with_mail(conn, cfg, client, monkeypatch, postings, today=date(2026, 10, 16)):
    monkeypatch.setattr(daily, "parse_alert", lambda message: postings)
    mailbox = Mailbox("imap.example.org", "u", "p", imap_factory=FakeImap({b"1": INDEED}))
    return run_daily(conn, cfg, client, TARGETS, RULES, [], today=today, mailbox=mailbox)


def postings_of(conn, title):
    return [tuple(r) for r in conn.execute(
        """SELECT p.source, p.source_id, p.description FROM postings p JOIN jobs j ON j.id = p.job_id
           WHERE j.title = ? ORDER BY p.source, p.source_id""", (title,))]


def test_lookup_attaches_a_clear_hit_with_its_full_text(conn, cfg, monkeypatch):
    client = LookupClient({"Musterplaner (m/w/d)": [posting(1, "Musterplaner (m/w/d)", company="Muster Industrie")]},
                          descriptions={"ID-1": "Volltext der Anzeige."})
    run_with_mail(conn, cfg, client, monkeypatch, [indeed(1, "Musterplaner (m/w/d)")])
    assert postings_of(conn, "Musterplaner (m/w/d)") == [
        ("bundesagentur", "ID-1", "Volltext der Anzeige."), ("indeed", "jk-1", "Kurzer Ausschnitt.")]
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    raw = json.loads(conn.execute("SELECT raw_json FROM postings WHERE source = 'bundesagentur'").fetchone()[0])
    assert raw["found_by"] == "lookup"
    assert conn.execute("SELECT count(*) FROM posting_terms").fetchone()[0] == 0


@pytest.mark.parametrize("hit", [
    posting(1, "Musterplaner Elektrotechnik (m/w/d)"),  # similar, but not close enough
    posting(1, "Musterplaner (m/w/d)", company="Beispiel Anlagenbau GmbH"),
    posting(1, "Musterplaner (m/w/d)", loc=GOETTINGEN),  # about 40 km away
])
def test_lookup_drops_a_hit_that_is_not_clear(conn, cfg, monkeypatch, hit):
    client = LookupClient({"Musterplaner (m/w/d)": [hit]})
    counts = run_with_mail(conn, cfg, client, monkeypatch, [indeed(1, "Musterplaner (m/w/d)")])
    assert postings_of(conn, "Musterplaner (m/w/d)") == [("indeed", "jk-1", "Kurzer Ausschnitt.")]
    assert conn.execute("SELECT count(*) FROM postings WHERE source = 'bundesagentur'").fetchone()[0] == 0
    assert (counts.lookups, counts.lookup_hits) == (1, 0)
    assert counts.lookup_dropped == (hit.company == "Muster Industrie GmbH")
    assert client.description_calls == []


def test_lookup_takes_the_most_similar_then_the_nearest_hit(conn, cfg, monkeypatch):
    near = Location("34119 Kassel", 51.318, 9.47)
    client = LookupClient({"Musterplaner (m/w/d)": [
        posting(1, "Musterplaner (m/w/d)", loc=Location("34233 Fuldatal", 51.37, 9.53)),
        posting(2, "Musterplaner (m/w/d)", loc=near),
    ]})
    run_with_mail(conn, cfg, client, monkeypatch, [indeed(1, "Musterplaner (m/w/d)")])
    assert [p[1] for p in postings_of(conn, "Musterplaner (m/w/d)")] == ["ID-2", "jk-1"]


def test_lookup_skips_a_hit_already_in_the_database(conn, cfg, monkeypatch):
    known = posting(1, "Musterplaner (m/w/d)")
    run_daily(conn, cfg, FakeClient([known], []), TARGETS, RULES, ["Ingenieur"], today=date(2026, 10, 15))
    client = LookupClient({"Musterplaner (m/w/d)": [known]})
    counts = run_with_mail(conn, cfg, client, monkeypatch, [indeed(1, "Musterplaner (m/w/d)")])
    assert counts.lookup_known == 1 and counts.lookup_hits == 0
    owners = conn.execute("SELECT source, job_id FROM postings ORDER BY source").fetchall()
    assert owners[0]["job_id"] != owners[1]["job_id"]  # the BA Posting stays with its own Job


@pytest.mark.parametrize("homeoffice, attached", [(True, True), (False, False)])
def test_a_remote_indeed_job_only_takes_a_home_office_hit(conn, cfg, monkeypatch, homeoffice, attached):
    hit = posting(1, "Softwareentwickler (m/w/d)", loc=HAMBURG, homeoffice=homeoffice)
    client = LookupClient({"Softwareentwickler (m/w/d)": [hit]},
                          descriptions={"ID-1": "Sie arbeiten 100% remote."})
    run_with_mail(conn, cfg, client, monkeypatch, [indeed(1, "Softwareentwickler (m/w/d)", loc=None)])
    assert client.lookups == [("Softwareentwickler (m/w/d)", None)]  # nationwide
    sources = [p[0] for p in postings_of(conn, "Softwareentwickler (m/w/d)")]
    assert ("bundesagentur" in sources) is attached
    # The full text settles the remote question. Without it, the alert's remote filter is the hint.
    job = jobs(conn)["Softwareentwickler (m/w/d)"]
    assert job["eligibility"] == "unclear"
    assert job["ineligible_reason"].endswith("'100% remote')" if attached else f"'{INDEED_REMOTE_HINT}')")


def test_a_remote_indeed_job_with_a_full_text_without_remote_phrase_is_dropped(conn, cfg, monkeypatch):
    hit = posting(1, "Softwareentwickler (m/w/d)", loc=HAMBURG, homeoffice=True)
    client = LookupClient({"Softwareentwickler (m/w/d)": [hit]}, descriptions={"ID-1": "2 Tage Homeoffice."})
    counts = run_with_mail(conn, cfg, client, monkeypatch, [indeed(1, "Softwareentwickler (m/w/d)", loc=None)])
    assert jobs(conn)["Softwareentwickler (m/w/d)"]["eligibility"] == "ineligible"
    assert counts.remote_dropped == 1


def test_a_hit_gives_a_job_with_an_unknown_place_its_place(conn, cfg, monkeypatch):
    region = Location("Musterland")  # a region, not a place the gazetteer knows
    client = LookupClient({"Musterplaner (m/w/d)": [posting(1, "Musterplaner (m/w/d)")]})
    run_with_mail(conn, cfg, client, monkeypatch, [indeed(1, "Musterplaner (m/w/d)", loc=region)])
    assert client.lookups == [("Musterplaner (m/w/d)", "Musterland")]
    job = jobs(conn)["Musterplaner (m/w/d)"]
    assert (job["eligibility"], job["ineligible_reason"], job["location"]) == ("eligible", None, "34117 Kassel")
    assert job["distance_km"] < 5


def test_a_job_with_an_unknown_place_stays_unclear_without_a_hit(conn, cfg, monkeypatch):
    client = LookupClient()
    run_with_mail(conn, cfg, client, monkeypatch,
                  [indeed(1, "Musterplaner (m/w/d)", loc=Location("Musterland"))])
    job = jobs(conn)["Musterplaner (m/w/d)"]
    assert (job["eligibility"], job["ineligible_reason"]) == ("unclear", "Ort unbekannt")


def test_lookups_per_run_are_capped_oldest_first(conn, cfg, monkeypatch):
    monkeypatch.setattr(daily, "LOOKUP_PER_RUN", 2)
    client = LookupClient()
    run_with_mail(conn, cfg, client, monkeypatch, [indeed(1, "Werkstoffprüfer")], today=date(2026, 10, 14))
    counts = run_with_mail(conn, cfg, client, monkeypatch,
                           [indeed(2, "Elektroingenieur"), indeed(3, "Projektleiter")])
    assert counts.lookups == 2
    assert [title for title, _ in client.lookups] == ["Werkstoffprüfer", "Werkstoffprüfer", "Elektroingenieur"]


def test_an_indeed_job_with_a_full_text_is_not_looked_up_again(conn, cfg, monkeypatch):
    client = LookupClient({"Musterplaner (m/w/d)": [posting(1, "Musterplaner (m/w/d)")]})
    run_with_mail(conn, cfg, client, monkeypatch, [indeed(1, "Musterplaner (m/w/d)")])
    counts = run_with_mail(conn, cfg, client, monkeypatch, [], today=date(2026, 10, 17))
    assert counts.lookups == 0
