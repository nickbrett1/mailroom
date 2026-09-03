"""PS+ Essential monthly lineup feed — scrape the authoritative source for new months.

The historical `essentials_lineup` is seeded from the curated `ESSENTIALS_MONTHS`
constant (essentials.py). This module keeps it CURRENT: it scrapes the same
authoritative source (the Fandom NA monthly list the seed came from) and merges
any new months/rows into `essentials_lineup`, so future PS+ Essential claims can
be dated as soon as the month's lineup is announced.

Source: https://playstation.fandom.com — "List of PlayStation Plus monthly games
(North America)" split per calendar year of the first-Tuesday `available_from`
(e.g. Jan 2026's lineup lives on the 2026 page even though announced in Dec
2025). We fetch the current calendar year + the next (to catch a December
announcement that lands in January), and merge only rows not already present.

Design notes:
- Read/parse only; the only write is INSERT OR IGNORE into `essentials_lineup`
  (mailroom remains the single writer).
- Merged rows carry no igdb_id (the wiki doesn't expose one); the enricher's
  normalized_title + platform fallback dates them. New rows are still dated
  correctly the moment they're claimed.
- Idempotent: re-scraping inserts nothing already present.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from datetime import UTC
from typing import Any

import httpx

from mailroom.verticals.game_catalog.parsers.psn import normalize_title

_FANDOM_API = "https://playstation.fandom.com/api.php"
# Wikipedia/Wikimedia policy requires a descriptive user-agent (their client is
# Fandom, which enforces similar policy); include contact info.
_USER_AGENT = (
    "mailroom/0.1 (PlayStation game catalog upkeep; "
    "scrapes PS+ Essential monthly list for own-catalog enrichment)"
)
_MONTH_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}


def _iso(date_str: str | None) -> str | None:
    """Normalize a wiki date to ISO (YYYY-MM-DD).

    Tolerates {{dts|2024-1-2}} (non-padded), {{dts|2021-01-05}}, and the plain
    'January 6, 2026' form used by the current-year pages."""
    if not date_str:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.fullmatch(r"(\w+) (\d{1,2}), (\d{4})", date_str)
    if m and m.group(1) in _MONTH_NUM:
        return f"{m.group(3)}-{_MONTH_NUM[m.group(1)]:02d}-{int(m.group(2)):02d}"
    return None


def _date_values(block: str) -> list[str]:
    """All dates in a wiki row block, in order (added, removed), any format."""
    vals = []
    vals += re.findall(r"\{\{dts\|([\d-]+)\}\}", block)
    vals += re.findall(
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December) (\d{1,2}), (\d{4})",
        block,
    )
    out = []
    for v in vals:
        if isinstance(v, tuple):
            v = f"{v[2]}-{_MONTH_NUM[v[0]]:02d}-{int(v[1]):02d}"
        iso = _iso(v)
        if iso:
            out.append(iso)
    return out


def parse_yearly_wikitext(wikitext: str) -> list[dict[str, Any]]:
    """Parse a year page's wikitext into monthly lineup records.

    Returns [{title, ps4, ps5, added, removed}] where added/removed are ISO or
    None, and `added` is carried across the rowspan'd month group.
    """
    records: list[dict[str, Any]] = []
    cur_added: str | None = None
    cur_removed: str | None = None
    for block in re.split(r"\n\|-", wikitext):
        lines = [l for l in block.split("\n") if l.strip()]
        numseen = 0
        title = None
        for line in lines:
            s = line.strip()
            if re.fullmatch(r"\|?\s*\d+\s*", s):
                numseen += 1
                continue
            if numseen >= 2 and not s.startswith("!"):
                m = re.search(r"''\[\[([^\]|]+)(?:\|([^\]]+))?\]\]''", s)
                title = (m.group(2) or m.group(1)).strip() if m else s.lstrip("|").strip()
                break
        if title is None:
            continue
        dates = _date_values(block)
        if dates:
            cur_added = dates[0]
            if len(dates) > 1:
                cur_removed = dates[1]
        records.append({
            "title": title,
            "ps4": "{{yes|PS4}}" in block,
            "ps5": "{{yes|PS5}}" in block,
            "added": cur_added,
            "removed": cur_removed,
        })
    # Header artifacts carry no added date and are never a real month group.
    return [r for r in records if r["title"] and r["title"].lower() != "game"]


def _page_wikitext(year: int, client: httpx.Client | None = None) -> str:
    """Fetch one year's monthly-list page wikitext ('' if the page doesn't exist)."""
    owns = client is None
    c = client or httpx.Client(headers={"User-Agent": _USER_AGENT}, timeout=30)
    try:
        name = (
            f"List_of_PlayStation_Plus_monthly_games_(North_America,_{year})"
            if year <= 2022
            else f"List_of_PlayStation_Essential_monthly_games_(North_America,_{year})"
        )
        r = c.get(
            _FANDOM_API,
            params={"action": "parse", "page": name, "format": "json", "prop": "wikitext"},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("parse", {}).get("wikitext", {}).get("*", "")
    finally:
        if owns:
            c.close()


def fetch_current_lineup_rows(
    now: _dt.datetime | None = None, client: httpx.Client | None = None
) -> list[dict[str, Any]]:
    """Scrape the current + next calendar year's pages into lineup records.

    New monthly lineups are announced late in the prior month for the NEXT
    month, so a Dec run must also check the following year's page."""
    now = now or _dt.datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for year in (now.year, now.year + 1):
        text = _page_wikitext(year, client=client)
        if not text:
            continue
        for r in parse_yearly_wikitext(text):
            r["year"] = year
            rows.append(r)
    return rows


def merge_new_lineup_rows(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> int:
    """Insert scraped lineup rows that aren't already present.

    One row per OFFERED platform (a PS4+PS5 monthly yields two rows, so the
    enricher can date whichever copy the user claimed). Keyed by
    (normalized_title, platform, available_from) to match the table's UNIQUE
    constraint. Idempotent — returns the number of NEW rows inserted.
    """
    inserted = 0
    for r in records:
        if not r.get("added"):
            continue  # skip rows whose month isn't parsed yet (incomplete wiki)
        title = r["title"]
        added = r["added"]
        removed = r.get("removed")
        month = added[:7]
        normalized = normalize_title(title)
        platforms = []
        if r.get("ps5"):
            platforms.append("playstation 5")
        if r.get("ps4"):
            platforms.append("playstation 4")
        for platform in platforms:
            cur = conn.execute(
                """INSERT OR IGNORE INTO essentials_lineup
                   (month, title, normalized_title, platform, available_from,
                    available_to, igdb_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, 'playstation-fandom-NA')""",
                (month, title, normalized, platform, added, removed),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted
