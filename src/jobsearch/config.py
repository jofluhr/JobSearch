"""Settings from .env and the private/ files.

Everything personal lives outside git: .env holds credentials and paths,
private/ holds the company list, the exclusion list and the Search terms.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """A setting is missing or invalid. The message says how to fix it."""


def project_root() -> Path:
    override = os.environ.get("JOBSEARCH_HOME")
    if override:
        return Path(override)
    # src/jobsearch/config.py -> repository root
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    root: Path
    profile_path: Path | None
    home_location: str | None
    home_lat: float | None
    home_lon: float | None
    radius_km: int
    radius_target_company_km: int
    remote_max_window_days: int
    reopening_days: int
    model_scoring: str
    max_scored_per_run: int
    feedback_notes_in_prompt: int
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""
    imap_processed_folder: str = "verarbeitet"
    model_extraction: str = "haiku"
    career_scan_interval_days: int = 7
    report_interval_days: int = 7
    tracing_endpoint: str = ""  # empty: tracing off

    def require_home(self) -> None:
        """The home location has no default, since it is personal. Fail clearly when it is not set."""
        missing = [name for name, value in (("HOME_LOCATION", self.home_location), ("HOME_LAT", self.home_lat),
                                            ("HOME_LON", self.home_lon)) if value is None]
        if missing:
            raise ConfigError(f"{', '.join(missing)} fehlt in .env. Trage deinen Wohnort und seine Koordinaten ein.")

    def read_profile(self) -> str:
        """The Profile text. The file belongs to another project and is only ever read."""
        if self.profile_path is None:
            raise ConfigError("PROFILE_PATH fehlt in .env. Ohne Profil kann nichts bewertet werden.")
        if not self.profile_path.is_file():
            raise ConfigError(f"Profil nicht gefunden: {self.profile_path}")
        return self.profile_path.read_text(encoding="utf-8")

    @property
    def imap_configured(self) -> bool:
        """The alert mail Source is optional: without IMAP settings the Daily run skips it."""
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def private_dir(self) -> Path:
        return self.root / "private"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobsearch.sqlite"

    @property
    def digests_dir(self) -> Path:
        return self.data_dir / "digests"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def geo_path(self) -> Path:
        return self.data_dir / "geo" / "places.tsv"

    @property
    def companies_path(self) -> Path:
        return self.private_dir / "companies.csv"

    @property
    def exclusions_path(self) -> Path:
        return self.private_dir / "exclusions.txt"

    @property
    def search_terms_path(self) -> Path:
        return self.private_dir / "search_terms.txt"

    @property
    def found_elsewhere_path(self) -> Path:
        return self.private_dir / "found_elsewhere.csv"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} in .env must be a whole number, got {raw!r}") from None


def _float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} in .env must be a number, got {raw!r}") from None


def load(root: Path | None = None) -> Config:
    root = root or project_root()
    load_dotenv(root / ".env")
    profile = os.environ.get("PROFILE_PATH", "").strip()
    return Config(
        root=root,
        profile_path=Path(profile) if profile else None,
        home_location=os.environ.get("HOME_LOCATION", "").strip() or None,
        home_lat=_float("HOME_LAT"),
        home_lon=_float("HOME_LON"),
        radius_km=_int("RADIUS_KM", 30),
        radius_target_company_km=_int("RADIUS_TARGET_COMPANY_KM", 70),
        remote_max_window_days=_int("REMOTE_MAX_WINDOW_DAYS", 14),
        reopening_days=_int("REOPENING_DAYS", 21),
        model_scoring=os.environ.get("MODEL_SCORING", "").strip() or "sonnet",
        max_scored_per_run=_int("MAX_SCORED_PER_RUN", 40),
        feedback_notes_in_prompt=_int("FEEDBACK_NOTES_IN_PROMPT", 20),
        imap_host=os.environ.get("IMAP_HOST", "").strip(),
        imap_user=os.environ.get("IMAP_USER", "").strip(),
        imap_password=os.environ.get("IMAP_PASSWORD", "").strip(),
        imap_processed_folder=os.environ.get("IMAP_PROCESSED_FOLDER", "").strip() or "verarbeitet",
        model_extraction=os.environ.get("MODEL_EXTRACTION", "").strip() or "haiku",
        career_scan_interval_days=_int("CAREER_SCAN_INTERVAL_DAYS", 7),
        report_interval_days=_int("REPORT_INTERVAL_DAYS", 7),
        tracing_endpoint=os.environ.get("TRACING_ENDPOINT", "").strip(),
    )


def read_list_file(path: Path) -> list[str]:
    """Non-empty lines of a private list file, without # comments."""
    if not path.exists():
        raise ConfigError(f"{path} is missing. Run 'jobsearch init' and fill it in.")
    lines = path.read_text(encoding="utf-8").splitlines()
    return [s for s in (line.strip() for line in lines) if s and not s.startswith("#")]
