"""Eligibility: a rule-based decision whether a Job is in reach. No AI involved."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

HOMEOFFICE_REASON = "Homeoffice möglich, ob vollständig remote ist offen"
NOT_FULLY_REMOTE_REASON = "Homeoffice möglich, laut Beschreibung nicht vollständig remote"
UNKNOWN_PLACE_REASON = "Ort unbekannt"
# The hint for a remote alert mail Job without a full text: its snippet rarely says "fully remote",
# but it came in through the alert's remote filter, so it counts as remote until the AI says otherwise.
INDEED_REMOTE_HINT = "Remote-Filter des Indeed-Alerts"

# Phrases that suggest fully remote work. A home office flag alone usually means hybrid.
_REMOTE_HINTS = re.compile(
    r"100\s*%\s*(remote|home\s*-?office|mobil)"
    r"|(vollständig|komplett|ausschließlich|rein|full|fully)\s*(-|\s)?\s*(remote|im\s+home\s*-?office|mobil)"
    r"|ortsunabhängig|remote[\s-]first|remote\s+aus\s+ganz\s+deutschland"
    r"|deutschlandweit\s+(remote|im\s+home\s*-?office)|bundesweit\s+(remote|im\s+home\s*-?office)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rules:
    radius_km: int
    radius_target_company_km: int
    exclusions: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    eligibility: str  # eligible | ineligible | unclear
    reason: str | None = None


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def obvious_mismatch(title: str, exclusions: tuple[str, ...]) -> str | None:
    """The first exclusion pattern found in the title, ignoring case."""
    lowered = title.lower()
    for pattern in exclusions:
        if pattern.lower() in lowered:
            return pattern
    return None


def decide(
    rules: Rules,
    title: str,
    distance: float | None,
    is_target_company: bool,
    homeoffice_possible: bool,
) -> Decision:
    hit = obvious_mismatch(title, rules.exclusions)
    if hit:
        return Decision("ineligible", f"Ausschlussliste: {hit}")
    if distance is not None and distance <= rules.radius_km:
        return Decision("eligible")
    if distance is not None and is_target_company and distance <= rules.radius_target_company_km:
        return Decision("eligible")
    if homeoffice_possible:
        # Hybrid outside the radius is out, fully remote is in. Only the description tells which.
        return Decision("unclear", HOMEOFFICE_REASON)
    if distance is None:
        return Decision("unclear", UNKNOWN_PLACE_REASON)
    return Decision("ineligible", f"Entfernung {distance:.0f} km")


def remote_hint(description: str) -> str | None:
    """The first phrase in a description that suggests fully remote work."""
    match = _REMOTE_HINTS.search(description)
    return match.group(0) if match else None


def remote_by_alert_filter() -> Decision:
    """A remote alert mail Job without a full text, kept for the AI like one with a remote phrase."""
    return Decision("unclear", f"{HOMEOFFICE_REASON} (Hinweis: '{INDEED_REMOTE_HINT}')")


def settle_remote(description: str) -> Decision:
    """Second look at a home office Job outside the radius, once its description is known.
    With a remote phrase it stays unclear for the AI to confirm, otherwise it is out."""
    hint = remote_hint(description)
    if hint:
        return Decision("unclear", f"{HOMEOFFICE_REASON} (Hinweis: '{hint}')")
    return Decision("ineligible", NOT_FULLY_REMOTE_REASON)
