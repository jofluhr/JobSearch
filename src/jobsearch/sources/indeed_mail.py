"""Indeed Postings from job alert mails, read over IMAP (ADR 0002).

Only mails from Indeed senders in the inbox are read. A mail is moved to the
processed folder only after its Postings are stored. A mail that yields no
Postings stays in the inbox and is reported, so a changed mail format shows up
instead of silently losing Jobs.

The parser reads the plain-text part, which lists each job as one block:

    Title
    Company - Place
    [salary line] [markers such as "Schnellbewerbung"]
    Snippet
    Age ("Vor 3 Tagen", "Gerade geschaltet")
    Tracking link

Organic results link to /rc/clk with the job key `jk`, which becomes the source ID
and a neutral job URL. Sponsored results link to /pagead/clk without a key, so
their source ID is a hash of title, company and place: the same ad in two mails
is still recognized. The personalized tracking links themselves are not kept for
organic results, since the mail asks not to pass them on.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr
from urllib.parse import parse_qs, urlparse

from . import Location, Posting

log = logging.getLogger(__name__)

IMAP_PORT = 993
_INDEED_DOMAIN = re.compile(r"(^|\.)indeed\.(com|de)$", re.IGNORECASE)
# Indeed also sends account mails: login codes, alert confirmations, profile changes. They are
# left alone in the inbox and not reported, since they never contain Jobs.
_ACCOUNT_MAIL = re.compile(
    # Whole phrases only: single words like "Account" or "Konto" also appear in job titles.
    r"mit dem folgenden code|bitte bestätigen sie|telefonnummer wurde|passwort wurde|e-mail-adresse wurde"
    r"|verification code|verify your|confirm your|password (was|has been)",
    re.IGNORECASE,
)

_JOB_LINK = re.compile(r"^https://[a-z.]*indeed\.[a-z]+/(rc|pagead)/clk", re.IGNORECASE)
_AGE = re.compile(r"^(vor \d+\+? (tag|tagen|stunde|stunden)|gerade geschaltet|heute|"
                  r"\d+\+? (day|days|hour|hours) ago|just posted|today)$", re.IGNORECASE)
_SALARY = re.compile(r"^(bis zu |ab |up to |from )?[\d.,]+\s*€.*(pro|per|/)\s*(jahr|monat|woche|tag|stunde|year|month|week|day|hour)$",
                     re.IGNORECASE)
_MARKERS = {"schnellbewerbung", "aktiver arbeitgeber", "easily apply", "responsive employer", "neu", "new"}
_NOWHERE = {"deutschland", "germany", "schweiz", "switzerland", "österreich", "austria"}
_LIST_ROW = re.compile(r'^\((?P<flags>[^)]*)\) (?:"(?P<delim>[^"]*)"|NIL) (?P<name>.+)$')
_REMOTE_PLACE = re.compile(r"^(remote|homeoffice|home office|home-office|von zu ?hause)\b", re.IGNORECASE)


class MailError(Exception):
    """The mailbox could not be read. The message is German and meant for the user."""


@dataclass
class AlertMail:
    uid: str
    sender: str
    subject: str
    message: EmailMessage


def is_indeed(sender: str) -> bool:
    address = parseaddr(sender)[1]
    return bool(address) and bool(_INDEED_DOMAIN.search(address.rpartition("@")[2]))


def is_account_mail(subject: str) -> bool:
    return bool(_ACCOUNT_MAIL.search(subject))


def parse_alert(message: EmailMessage) -> list[Posting]:
    """Postings from one alert mail, read from its plain-text part."""
    body = message.get_body(("plain",))
    if body is None:
        return []
    text = body.get_content().replace("\r\n", "\n")
    postings = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
        if len(lines) >= 3 and _JOB_LINK.match(lines[-1]):
            postings.append(_posting(lines))
    return postings


def _posting(lines: list[str]) -> Posting:
    title, company_line, *middle, link = lines
    company, _, place = company_line.rpartition(" - ")
    if not company:
        company, place = company_line, ""
    salary = next((line for line in middle if _SALARY.match(line)), None)
    snippet = " ".join(line for line in middle
                       if not (_AGE.match(line) or _SALARY.match(line) or line.lower() in _MARKERS))
    if salary:
        snippet += f"\nGehalt laut Indeed: {salary}"
    parsed = urlparse(link)
    jk = parse_qs(parsed.query).get("jk", [None])[0]
    if jk:
        source_id, url = jk, f"{parsed.scheme}://{parsed.netloc}/viewjob?jk={jk}"
    else:
        digest = hashlib.sha1(f"{title}|{company}|{place}".lower().encode("utf-8")).hexdigest()[:16]
        source_id, url = f"ad-{digest}", link
    # A country instead of a place: the ad came in through the alert's remote filter.
    remote = bool(_REMOTE_PLACE.match(place)) or place.lower() in _NOWHERE
    known_place = place and not remote
    return Posting(
        "indeed", source_id, title, company, url=url,
        locations=[Location(place)] if known_place else [],
        homeoffice_possible=remote,
        description=snippet or None, is_snippet=True,
        raw={"place": place, "salary": salary, "sponsored": not jk},
    )


class Mailbox:
    """The inbox over IMAP SSL. Use as a context manager."""

    def __init__(self, host: str, user: str, password: str, imap_factory=imaplib.IMAP4_SSL):
        self.host, self.user, self.password = host, user, password
        self.imap_factory = imap_factory
        self.imap = None

    def __enter__(self) -> Mailbox:
        try:
            self.imap = self.imap_factory(self.host, IMAP_PORT)
            self.imap.login(self.user, self.password)
            self._ok(self.imap.select("INBOX"), "Posteingang öffnen")
        except imaplib.IMAP4.error as e:
            raise MailError(f"Anmeldung bei {self.host} fehlgeschlagen. Stimmen IMAP_USER und IMAP_PASSWORD, "
                            f"und ist IMAP im Postfach erlaubt? ({e})") from None
        except OSError as e:
            raise MailError(f"Keine Verbindung zu {self.host}: {e}") from None
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.imap.logout()
        except Exception:  # logout after a broken connection is not worth an error
            pass

    @staticmethod
    def _ok(response, action: str):
        status, data = response
        if status != "OK":
            raise MailError(f"IMAP: {action} fehlgeschlagen ({data!r})")
        return data

    def indeed_mails(self) -> list[AlertMail]:
        """Alert mails from Indeed in the inbox, without account mails. BODY.PEEK leaves them unread."""
        uids = self._ok(self.imap.uid("SEARCH", None, '(FROM "indeed")'), "Suche")[0].split()
        mails = []
        for uid in uids:
            data = self._ok(self.imap.uid("FETCH", uid, "(BODY.PEEK[])"), "Abruf")
            raw = next((part[1] for part in data if isinstance(part, tuple)), None)
            if raw is None:
                continue
            message = email.message_from_bytes(raw, policy=default_policy)
            sender = str(message.get("From", ""))
            subject = str(message.get("Subject", ""))
            if is_account_mail(subject):
                log.debug("Skipping Indeed account mail: %s", subject)
            elif is_indeed(sender):
                mails.append(AlertMail(uid.decode(), sender, str(message.get("Subject", "")), message))
        return mails

    def folder(self, name: str) -> str:
        """The server's full name for a folder: an exact match, or a folder whose last path part
        is `name`, such as "INBOX/verarbeitet" for "verarbeitet"."""
        found = []
        for row in self._ok(self.imap.list(), "Ordnerliste"):
            match = _LIST_ROW.match(row.decode("utf-8", errors="replace") if isinstance(row, bytes) else str(row))
            if match:
                found.append((match["name"].strip('"'), match["delim"]))
        if any(full == name for full, _ in found):
            return name
        nested = [full for full, delim in found if delim and full.split(delim)[-1] == name]
        if len(nested) == 1:
            return nested[0]
        raise MailError(f"Ordner '{name}' nicht eindeutig gefunden (vorhanden: {', '.join(f for f, _ in found)}). "
                        "Ordner im Postfach anlegen oder IMAP_PROCESSED_FOLDER anpassen.")

    def move(self, uid: str, folder: str) -> None:
        """Move a mail, with COPY + delete as fallback for servers without MOVE."""
        quoted = '"' + folder.replace('"', '') + '"'
        if b"MOVE" in self.imap.capabilities or "MOVE" in self.imap.capabilities:
            self._ok(self.imap.uid("MOVE", uid, quoted), f"Verschieben nach {folder}")
            return
        self._ok(self.imap.uid("COPY", uid, quoted), f"Kopieren nach {folder}")
        self._ok(self.imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)"), "Löschen markieren")
        self._ok(self.imap.expunge(), "Löschen")
