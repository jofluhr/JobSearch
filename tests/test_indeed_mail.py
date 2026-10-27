import email
from datetime import date
from email.message import EmailMessage
from email.policy import default as default_policy

import pytest

from jobsearch import daily
from jobsearch.eligibility import Rules
from jobsearch.sources import Location, Posting
from jobsearch.sources.indeed_mail import Mailbox, MailError, is_account_mail, is_indeed, parse_alert

from .conftest import FIXTURES


def mail_bytes(sender, subject="Neue Jobs für dich"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "kandidat@example.org", subject
    msg.set_content("Text")
    msg.add_alternative("<html><body><p>Jobs</p></body></html>", subtype="html")
    return msg.as_bytes()


class FakeImap:
    """Just enough of imaplib.IMAP4_SSL: a mailbox of uid -> raw mail."""

    def __init__(self, mails, move=True, fail_login=False, folders=("INBOX", "INBOX/verarbeitet")):
        self.mails = dict(mails)
        self.capabilities = ("IMAP4REV1", "MOVE") if move else ("IMAP4REV1",)
        self.fail_login = fail_login
        self.folders = {}
        self.deleted = set()
        self.folder_names = folders

    def __call__(self, host, port):  # used as the imap_factory
        return self

    def login(self, user, password):
        if self.fail_login:
            import imaplib
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, box):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(self.mails)]
        uid = args[0].encode() if isinstance(args[0], str) else args[0]
        if command == "FETCH":
            return "OK", [(b"1 (BODY[] {n}", self.mails[uid]), b")"]
        if command in ("MOVE", "COPY"):
            self.folders.setdefault(args[1].strip('"'), []).append(uid)
            if command == "MOVE":
                del self.mails[uid]
            return "OK", [None]
        if command == "STORE":
            self.deleted.add(uid)
            return "OK", [None]
        raise AssertionError(command)

    def expunge(self):
        for uid in self.deleted:
            del self.mails[uid]
        self.deleted.clear()
        return "OK", [None]

    def list(self):
        return "OK", [f'(\\HasNoChildren) "/" {name}'.encode() for name in self.folder_names]

    def logout(self):
        pass


INDEED = mail_bytes("Indeed <alert@indeed.com>")
OTHER = mail_bytes("Newsletter <news@not-indeed.example>")


@pytest.mark.parametrize(
    "sender, expected",
    [
        ("Indeed <alert@indeed.com>", True),
        ("Indeed <jobalerts-noreply@mail.indeed.de>", True),
        ("Indeed Fan <x@indeed.com.example>", False),
        ("Newsletter <news@not-indeed.example>", False),
        ("", False),
    ],
)
def test_is_indeed(sender, expected):
    assert is_indeed(sender) is expected


@pytest.mark.parametrize(
    "subject, account",
    [
        ("Melden Sie sich bei Indeed mit dem folgenden Code an: 123456", True),
        ("Bitte bestätigen Sie Ihre Job-E-Mail für „Projektingenieur“ Jobs in Musterstadt", True),
        ("Ihre Telefonnummer wurde geändert", True),
        ("15 neue Jobs für Projektingenieur in Musterstadt", False),
        ("Projektingenieur (m/w/d) bei Muster GmbH und weitere Jobs", False),
        ("8 neue Jobs für Account Manager Konto-Service in Musterstadt", False),
    ],
)
def test_account_mails_are_recognized(subject, account):
    assert is_account_mail(subject) is account


def test_only_indeed_alert_mails_are_read():
    account = mail_bytes("Indeed <alert@indeed.com>", subject="Ihre Telefonnummer wurde geändert")
    imap = FakeImap({b"1": INDEED, b"2": OTHER, b"3": account})
    with Mailbox("imap.example.org", "u", "p", imap_factory=imap) as box:
        mails = box.indeed_mails()
    assert [m.uid for m in mails] == ["1"]
    assert mails[0].subject == "Neue Jobs für dich"


@pytest.mark.parametrize("move", [True, False])
def test_move_with_and_without_move_support(move):
    imap = FakeImap({b"1": INDEED}, move=move)
    with Mailbox("imap.example.org", "u", "p", imap_factory=imap) as box:
        box.move("1", "verarbeitet")
    assert imap.folders == {"verarbeitet": [b"1"]} and imap.mails == {}


def test_login_failure_is_explained():
    with pytest.raises(MailError, match="IMAP_USER und IMAP_PASSWORD"):
        with Mailbox("imap.example.org", "u", "p", imap_factory=FakeImap({}, fail_login=True)):
            pass


def alert():
    return email.message_from_bytes((FIXTURES / "indeed_alert.eml").read_bytes(), policy=default_policy)


def test_parser_finds_every_job_block():
    postings = parse_alert(alert())
    assert [p.title for p in postings] == [
        "Projektingenieur (m/w/d)",
        "Entwicklungsingenieur - Prüfstände (w/m/d)",
        "Projektleiter Montage (m/w/d)",
        "Softwareentwickler Python (m/w/d)",
    ]
    assert all(p.source == "indeed" and p.is_snippet for p in postings)


def test_organic_result_uses_job_key_and_neutral_url():
    p = parse_alert(alert())[0]
    assert (p.source_id, p.url) == ("0123456789abcdef", "https://de.indeed.com/viewjob?jk=0123456789abcdef")
    assert (p.company, p.location_raw) == ("Muster Industrie GmbH", "Musterstadt")
    assert p.description == "Sie planen und betreuen Projekte von der Idee bis zur Inbetriebnahme."
    assert p.raw["sponsored"] is False


def test_sponsored_result_gets_a_stable_hash_id():
    first = parse_alert(alert())[1]
    assert first.source_id.startswith("ad-") and first.source_id == parse_alert(alert())[1].source_id
    assert first.url.startswith("https://de.indeed.com/pagead/clk")
    assert (first.company, first.location_raw) == ("Example Tech AG", "Beispielstadt am See")
    # salary and markers are not part of the snippet, the salary is kept as a note
    assert first.description == ("Aufbau und Betrieb von Prüfständen. Erfahrung mit Messtechnik wünschenswert…"
                                 "\nGehalt laut Indeed: 45.000 € – 60.000 € pro Jahr")


def test_country_only_place_means_remote():
    # Such ads come in through the alert's remote filter
    p = parse_alert(alert())[2]
    assert p.locations == [] and p.raw["place"] == "Deutschland" and p.homeoffice_possible is True


def test_remote_place_sets_home_office():
    p = parse_alert(alert())[3]
    assert p.locations == [] and p.homeoffice_possible is True


def test_mail_without_plain_text_part_yields_nothing():
    msg = EmailMessage()
    msg.set_content("<p>nur HTML</p>", subtype="html")
    assert parse_alert(msg) == []


# The Daily run with alert mails

RULES = Rules(30, 70, ())


class FakeClient:
    def search(self, term, **kwargs):
        return iter([])

    def lookup(self, title, **kwargs):
        return []

    def description(self, refnr):
        return None


def run(conn, cfg, imap):
    mailbox = Mailbox("imap.example.org", "u", "p", imap_factory=imap)
    return daily.run_daily(conn, cfg, FakeClient(), [], RULES, ["Ingenieur"], today=date(2026, 10, 16), mailbox=mailbox)


def test_mail_is_moved_only_after_its_postings_are_stored(conn, cfg, monkeypatch):
    posting = Posting("indeed", "jk-1", "Projektingenieur", "Muster GmbH", url="https://x/jk-1",
                      locations=[Location("34117 Kassel", 51.315, 9.49)], description="Kurzer Text", is_snippet=True)
    monkeypatch.setattr(daily, "parse_alert", lambda message: [posting])
    imap = FakeImap({b"1": INDEED})
    counts = run(conn, cfg, imap)
    assert (counts.alert_mails, counts.alert_mails_processed, counts.new_jobs) == (1, 1, 1)
    assert imap.folders == {"INBOX/verarbeitet": [b"1"]}
    row = conn.execute("SELECT source, is_snippet FROM postings").fetchone()
    assert tuple(row) == ("indeed", 1)


def test_mail_without_postings_stays_in_the_inbox(conn, cfg):
    imap = FakeImap({b"1": INDEED})
    counts = run(conn, cfg, imap)
    assert (counts.alert_mails, counts.alert_mails_unread) == (1, 1)
    assert imap.folders == {} and list(imap.mails) == [b"1"]


@pytest.mark.parametrize(
    "folders, wanted, expected",
    [
        (("INBOX", "verarbeitet"), "verarbeitet", "verarbeitet"),
        (("INBOX", "INBOX/verarbeitet"), "verarbeitet", "INBOX/verarbeitet"),
        (("INBOX", "INBOX/Archiv"), "INBOX/Archiv", "INBOX/Archiv"),
    ],
)
def test_folder_is_resolved(folders, wanted, expected):
    with Mailbox("imap.example.org", "u", "p", imap_factory=FakeImap({}, folders=folders)) as box:
        assert box.folder(wanted) == expected


@pytest.mark.parametrize("folders", [("INBOX",), ("INBOX/verarbeitet", "Archiv/verarbeitet")])
def test_missing_or_ambiguous_folder_is_an_error(folders):
    with Mailbox("imap.example.org", "u", "p", imap_factory=FakeImap({}, folders=folders)) as box:
        with pytest.raises(MailError, match="nicht eindeutig gefunden"):
            box.folder("verarbeitet")


def test_missing_folder_stops_before_anything_is_stored(conn, cfg):
    imap = FakeImap({b"1": (FIXTURES / "indeed_alert.eml").read_bytes()}, folders=("INBOX",))
    counts = run(conn, cfg, imap)
    assert "nicht eindeutig gefunden" in counts.errors[0]
    assert conn.execute("SELECT count(*) FROM postings").fetchone()[0] == 0
    assert list(imap.mails) == [b"1"]


def test_real_parser_through_the_daily_run(conn, cfg):
    imap = FakeImap({b"1": (FIXTURES / "indeed_alert.eml").read_bytes()})
    counts = run(conn, cfg, imap)
    assert (counts.alert_mails_processed, counts.new_jobs) == (1, 4)
    assert imap.folders == {"INBOX/verarbeitet": [b"1"]}
    counts = run(conn, cfg, FakeImap({b"2": (FIXTURES / "indeed_alert.eml").read_bytes()}))
    assert counts.new_jobs == 0  # the same jobs in a second mail are recognized


def test_mailbox_failure_does_not_stop_the_run(conn, cfg):
    counts = run(conn, cfg, FakeImap({}, fail_login=True))
    assert counts.errors and counts.errors[0].startswith("Alert-Mails:")
    assert conn.execute("SELECT ok FROM runs").fetchone()[0] == 0
