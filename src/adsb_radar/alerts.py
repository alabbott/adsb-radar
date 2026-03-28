#!/usr/bin/env python3
"""
alerts.py — plane-alert-db integration for ADS-B receiver.

Loads the plane-alert-db CSV (sdr-enthusiast/plane-alert-db on GitHub)
and provides fast ICAO hex lookups for registration, operator, type, tags.

Usage:
    db = AlertDB.load()              # search default locations, silent if not found
    db = AlertDB.load('auto')        # search + download from GitHub if not found
    db = AlertDB.load('/path/to/plane-alert-db.csv')

    entry = db.lookup('A12345')      # AlertEntry or None
    len(db)                          # number of entries loaded

Download the DB manually:
    adsb-alerts --download
    # or: python3 -m adsb_radar.alerts --download

The CSV (~200 KB) is saved next to alerts.py and found automatically on the
next run.
"""

import argparse
import csv
import os
import tempfile
from urllib.error import URLError
from urllib.request import urlretrieve

GITHUB_URL = (
    "https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db"
    "/refs/heads/main/plane-alert-db.csv"
)
DEFAULT_FILENAME = "plane-alert-db.csv"

# Directories searched in order when no explicit path is given
_SEARCH_DIRS = [
    os.path.dirname(os.path.abspath(__file__)),  # same dir as scripts
    os.path.expanduser("~/.local/share/adsb"),
    os.path.expanduser("~/"),
]

# Short labels for the Category field
CATEGORY_ABBREV = {
    "Military": "MIL",
    "Government": "GOV",
    "Law Enforcement": "LEA",
    "Medical": "MED",
    "News": "NEWS",
    "Historic": "HIST",
    "Interesting": "INT",
    "UAV": "UAV",
    "Balloons": "BALL",
    "Blimps/Airships": "BLMP",
}

# Category → scope marker colour band override
# These aircraft skip the altitude colour and get a fixed highlight pair
CATEGORY_HIGHLIGHT = {
    "Military": "mil",
    "Government": "gov",
    "Law Enforcement": "lea",
}


class AlertEntry:
    """Single plane-alert-db record."""

    __slots__ = ("icao", "reg", "operator", "type_desc", "tags", "category", "military", "notes")

    def __init__(self, icao, reg, operator, type_desc, tags, category, military, notes):
        self.icao = icao  # uppercase 6-char hex
        self.reg = reg  # tail / registration
        self.operator = operator  # airline or owner name
        self.type_desc = type_desc  # human-readable aircraft type
        self.tags = tags  # list[str], up to 3 non-empty tags
        self.category = category  # e.g. "Military"
        self.military = military  # bool
        self.notes = notes  # free-text notes

    @property
    def tag_str(self):
        """Short tag summary for display (e.g. 'MIL GOV')."""
        parts = []
        if self.military:
            parts.append("MIL")
        abbr = CATEGORY_ABBREV.get(self.category)
        if abbr and abbr not in parts:
            parts.append(abbr)
        for t in self.tags:
            s = t[:5].upper()
            if s and s not in parts:
                parts.append(s)
        return " ".join(parts[:3])

    @property
    def highlight(self):
        """Return highlight key ('mil', 'gov', 'lea') or None."""
        return CATEGORY_HIGHLIGHT.get(self.category)

    @property
    def scope_label(self):
        """
        Best short label for the radar scope.
        Prefer registration (tail number) over ICAO hex — it's more readable
        and especially useful for military/gov aircraft with no callsign.
        """
        return self.reg or self.icao

    def summary(self):
        """One-line human description."""
        parts = [self.reg or self.icao]
        if self.operator:
            parts.append(self.operator)
        if self.type_desc:
            parts.append(self.type_desc)
        if self.tag_str:
            parts.append(f"[{self.tag_str}]")
        if self.notes:
            parts.append(f"— {self.notes[:60]}")
        return "  ".join(parts)


class AlertDB:
    """In-memory plane-alert-db, keyed by uppercase ICAO hex."""

    def __init__(self, entries=None):
        self._db: dict[str, AlertEntry] = {}
        if entries:
            for e in entries:
                self._db[e.icao] = e

    # ── Public API ────────────────────────────────────────────────────────

    def lookup(self, icao_hex: str):
        """Return AlertEntry for this ICAO hex, or None."""
        return self._db.get(icao_hex.upper().strip())

    def __len__(self):
        return len(self._db)

    def __bool__(self):
        return bool(self._db)

    # ── Loaders ───────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path=None, verbose=True):
        """
        Load the alert DB.

        path=None   — search default locations; return empty DB silently if not found
        path='auto' — search then download from GitHub if not found
        path=PATH   — load that specific file (error if missing)
        """
        if path and path not in ("auto",):
            return cls._load_file(path, verbose=True)

        # Search default locations
        for d in _SEARCH_DIRS:
            candidate = os.path.join(d, DEFAULT_FILENAME)
            if os.path.exists(candidate):
                return cls._load_file(candidate, verbose=verbose)

        # Not found
        if path == "auto":
            return cls._download_and_load(verbose=verbose)

        # Silently absent
        if verbose:
            print("  plane-alert-db: not found (run with --alert-db auto to download)", flush=True)
        return cls()

    @classmethod
    def _download_and_load(cls, verbose=True):
        dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_FILENAME)
        if verbose:
            print(f"  Downloading plane-alert-db (~200 KB) → {dest} …", flush=True)
        try:
            # Download to a temp file first; rename on success so a failed or
            # partial download never leaves a corrupt file at the final path.
            tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".tmp")
            os.close(tmp_fd)
            urlretrieve(GITHUB_URL, tmp_path)
            os.replace(tmp_path, dest)
            return cls._load_file(dest, verbose=verbose)
        except (URLError, OSError) as e:
            if verbose:
                print(f"  Download failed: {e}", flush=True)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return cls()

    @classmethod
    def _load_file(cls, path, verbose=True):
        entries = []
        try:
            with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
                reader = csv.DictReader(f)
                for raw in reader:
                    # Strip leading $ and # from keys
                    # Header is: $ICAO,$Registration,...,$Tag 1,$#Tag 2,$#Tag 3,...
                    row = {k.lstrip("$#").strip(): v for k, v in raw.items()}

                    icao = row.get("ICAO", row.get("ICAO24", row.get("icao24", ""))).strip().upper()
                    if not icao or len(icao) not in (4, 5, 6):
                        continue
                    icao = icao.zfill(6)  # normalise to 6 chars

                    tags = [row.get(f"Tag {i}", "").strip() for i in (1, 2, 3)]
                    tags = [t for t in tags if t]

                    mil_raw = row.get("Military", "").strip().lower()
                    military = mil_raw in ("1", "true", "yes", "y", "mil")

                    entries.append(
                        AlertEntry(
                            icao=icao,
                            reg=row.get("Registration", "").strip(),
                            operator=row.get("Operator", "").strip(),
                            type_desc=row.get("Type", row.get("ICAO Type Code", "")).strip(),
                            tags=tags,
                            category=row.get("Category", "").strip(),
                            military=military,
                            notes=row.get("Notes", "").strip(),
                        )
                    )

            if verbose:
                print(
                    f"  plane-alert-db: {len(entries):,} entries loaded"
                    f" from {os.path.basename(path)}",
                    flush=True,
                )
        except OSError as e:
            if verbose:
                print(f"  plane-alert-db: could not read {path}: {e}", flush=True)

        return cls(entries)


# ── CLI download helper ────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description="plane-alert-db loader / downloader")
    ap.add_argument(
        "--download",
        action="store_true",
        help="Download plane-alert-db.csv from GitHub into the package directory",
    )
    ap.add_argument("--test", metavar="ICAO", help="Lookup a specific ICAO hex after loading")
    args = ap.parse_args()

    if args.download:
        db = AlertDB.load("auto", verbose=True)
    else:
        db = AlertDB.load(verbose=True)

    print(f"Loaded {len(db):,} entries.")

    if args.test:
        entry = db.lookup(args.test)
        if entry:
            print(f"Found: {entry.summary()}")
            print(f"  reg={entry.reg!r}  operator={entry.operator!r}")
            print(f"  type={entry.type_desc!r}  category={entry.category!r}")
            print(f"  military={entry.military}  tags={entry.tags}")
            print(f"  highlight={entry.highlight}  scope_label={entry.scope_label!r}")
        else:
            print(f"Not in database: {args.test.upper()}")


if __name__ == "__main__":
    main()
