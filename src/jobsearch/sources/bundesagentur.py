"""Bundesagentur für Arbeit: Jobsuche API v6 for search, public detail page for the description.

The API is public but unofficial, and its detail endpoint is closed (403). The full
description is read from the ng-state JSON embedded in the public job page instead.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator

import requests

from . import Location, Posting

log = logging.getLogger(__name__)

SEARCH_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
DETAIL_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
API_KEY = "jobboerse-jobsuche"  # public key used by the Bundesagentur's own web app
PAGE_SIZE = 100
MAX_PAGES = 30
USER_AGENT = "JobSearch/0.1 (personal job search, low volume)"

_NG_STATE = re.compile(r'<script[^>]*id="ng-state"[^>]*>(.*?)</script>', re.S)


class SchemaChanged(Exception):
    """The API answered in a shape this client does not understand."""


def to_posting(entry: dict) -> Posting:
    refnr = entry.get("referenznummer")
    # A few offers carry only the occupation and no ad title.
    title = entry.get("stellenangebotsTitel") or entry.get("hauptberuf")
    if not refnr or not title:
        raise SchemaChanged(f"search result without reference number or title: {sorted(entry)}")
    locations = []
    for loc in entry.get("stellenlokationen") or []:
        address = loc.get("adresse") or {}
        label = " ".join(p for p in (address.get("plz"), address.get("ort")) if p)
        locations.append(Location(label, loc.get("breite"), loc.get("laenge")))
    return Posting(
        source="bundesagentur",
        source_id=refnr,
        title=title,
        company=entry.get("firma") or "unbekannt",
        url=DETAIL_URL.format(refnr=refnr),
        locations=locations,
        distance_km=entry.get("entfernung"),
        homeoffice_possible=bool(entry.get("homeofficemoeglich")),
        raw=entry,
    )


def parse_description(html: str) -> str | None:
    match = _NG_STATE.search(html)
    if not match:
        return None
    try:
        state = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    detail = state.get("jobdetail") or {}
    return detail.get("stellenangebotsBeschreibung") or None


class BundesagenturClient:
    def __init__(self, session: requests.Session | None = None, delay: float = 0.3):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.delay = delay

    def search(
        self,
        term: str,
        *,
        location: str | None = None,
        radius_km: int | None = None,
        published_within_days: int | None = None,
    ) -> Iterator[Posting]:
        """All job offers (no apprenticeships) for a term, page by page."""
        params: dict[str, str | int] = {"was": term, "angebotsart": 1, "size": PAGE_SIZE}
        if location:
            params["wo"] = location
        if radius_km is not None:
            params["umkreis"] = radius_km
        if published_within_days is not None:
            params["veroeffentlichtseit"] = published_within_days
        delivered = 0
        for page in range(1, MAX_PAGES + 1):
            params["page"] = page
            entries, total = self._page(params)
            for entry in entries:
                yield to_posting(entry)
            delivered += len(entries)
            if not entries or delivered >= total:
                return
            time.sleep(self.delay)
        log.warning("Stopped after %d pages for %r, results may be incomplete", MAX_PAGES, term)

    def lookup(self, title: str, *, location: str | None = None, radius_km: int | None = None) -> list[Posting]:
        """The first page of offers for one title, for a targeted lookup. One request, after the delay."""
        params: dict[str, str | int] = {"was": title, "angebotsart": 1, "size": PAGE_SIZE, "page": 1}
        if location:
            params["wo"] = location
        if radius_km is not None:
            params["umkreis"] = radius_km
        time.sleep(self.delay)
        return [to_posting(entry) for entry in self._page(params)[0]]

    def _page(self, params: dict) -> tuple[list[dict], int]:
        """One page of search results and the total number of results."""
        response = self.session.get(SEARCH_URL, params=params, headers={"X-API-Key": API_KEY}, timeout=30)
        response.raise_for_status()
        data = response.json()
        entries = data.get("ergebnisliste")
        if entries is None:
            if data.get("maxErgebnisse") in (0, "0", None):
                return [], 0
            raise SchemaChanged("search response without 'ergebnisliste'")
        return entries, int(data.get("maxErgebnisse") or 0)

    def description(self, refnr: str) -> str | None:
        time.sleep(self.delay)
        response = self.session.get(DETAIL_URL.format(refnr=refnr), timeout=30)
        response.raise_for_status()
        response.encoding = "utf-8"
        return parse_description(response.text)
