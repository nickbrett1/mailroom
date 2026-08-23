"""Canonical-game grouping for the catalog (memos/catalog-games-model).

A single logical game — e.g. Alien Isolation, which you own both as the base
game and "The Collection" — currently shows up as two entries because the two
editions are separate IGDB entries / owned rows. The catalog front-end should
show ONE card per game, with the multiple purchases/editions as its editions.

This module turns owned_games (the per-edition/per-purchase ownership records)
into `games` (the canonical game) + reparents each owned row under a game via
owned_games.game_id. Grouping is keyed on a *canonical normalized title*:
edition aliases map to their base title (curated, extensible), and any other
rows that already share a normalized title group together (so "Slay the
Spire" bought AND PS+ claimed, or a game on PS4 + PS5, collapse to one card).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Edition title -> canonical title (normalized). Extend as new edition
# variants are found. Keyed on owned_games.normalized_title. The suffix /
# numeral normalizers in canonical_title() below also fold common edition and
# bundle markers (e.g. " - Digital Deluxe Edition", " (Full Game and Add-On
# Content)") onto the base title, so these exact entries are only needed for
# special cases the generic rules can't express.
EDITION_GROUPS: dict[str, str] = {
    # Alien Isolation: "The Collection" is the same game as the base release.
    "alien isolation - the collection": "alien isolation",
    "alien isolation: the collection": "alien isolation",
    "alien isolation the collection": "alien isolation",
    # Arcade Paradise has a standalone PSVR2 version of the same game.
    "arcade paradise vr": "arcade paradise",
    # Slay the Spire was both bought and PS+ claimed — same game.
    "slay the spire": "slay the spire",
}

# Edition / bundle markers folded onto the base title. These do NOT denote a
# different game (a Deluxe/Complete/Collection/Enhanced edition is the same
# title), so they're stripped before grouping. Kept out: "remastered" /
# "remake" / numbered sequels, which can be genuinely distinct titles.
_EDITION_MARKERS = [
    r"\(full game and add-on content\)",
    r"\(full game and add-ons\)",
    r"\(full game\)",
    r"\(ps3™/psp®/ps vita\)( \d+ mb required)?",
    r"- digital deluxe edition",
    r"- cross-gen deluxe bundle",
    r"- total mayhem bundle.*",
    r"- the collection",
    r"- complete edition",
    r"- enhanced edition",
    r"- launch edition",
    r"- championship edition",
    r"- special edition",
    r"- definitive edition",
    r"- ultimate edition",
    r"- gold edition",
    r"- deluxe edition",
    r"- collection",
    r"- maestro beard edition",
    r"- 20th anniversary edition",
    # Episodic games are listed on PSN both as the full season and as
    # " - Episode 1" (the entry point) — the same game, so fold them.
    r"- episode 1",
    r"- chapter 1",
    r"- episode i",
    r"- chapter i",
    r"- edition",
]

_ROMAN_TO_ARABIC = {
    "iii": "3", "ii": "2", "iv": "4", "vi": "6", "vii": "7",
    "ix": "9", "v": "5", "x": "10",
}


def _normalize_punct(s: str) -> str:
    return (s.replace("™", "").replace("®", "").replace("’", "'")
             .replace("‘", "'").replace("–", "-").replace("—", "-"))


def _strip_edition_markers(s: str) -> str:
    out = s
    for marker in _EDITION_MARKERS:
        out = re.sub(rf"\s*{marker}\s*$", "", out, flags=re.IGNORECASE)
    return out


def _roman_to_arabic(s: str) -> str:
    for rom, arabic in sorted(_ROMAN_TO_ARABIC.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"\b{rom}\b", arabic, s)
    return s


def canonical_title(normalized_title: str | None) -> str:
    """Map an edition's normalized title to its canonical game key.

    Folds common edition/bundle markers (" - Digital Deluxe Edition", "(Full
    Game and Add-On Content)") and roman→arabic numerals onto the base title,
    so "Divinity: Original Sin II - Definitive Edition" and "Divinity:
    Original Sin 2 - Definitive Edition" collapse to the same card.
    """
    norm = _normalize_punct((normalized_title or "").strip().lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    mapped = EDITION_GROUPS.get(norm, norm)
    mapped = _strip_edition_markers(mapped)
    mapped = _roman_to_arabic(mapped)
    return re.sub(r"\s+", " ", mapped).strip()

# Platforms with no real signal (generic receipt/PSN rows) — collapse to a
# single display platform when that's all a game has.
_GENERIC_PLATFORMS = {None, "", "playstation", "ps"}


@dataclass
class GamesReport:
    games: int = 0
    reparented: int = 0
    groups: list[dict] = field(default_factory=list)  # {key, title, editions, igdb_id}


def _base_member(members: list[dict[str, Any]]) -> dict[str, Any]:
    """The 'base' edition of a group: the row whose normalized_title equals the
    canonical key, else the shortest title (the base release usually has the
    shortest name)."""
    key = canonical_title(members[0]["normalized_title"])
    for m in members:
        if (m.get("normalized_title") or "").strip().lower() == key:
            return m
    return min(members, key=lambda m: len(m["title"] or ""))


def _edition_summary(row: dict[str, Any]) -> dict[str, Any]:
    """A compact, serializable summary of one owned/edition row."""
    return {
        "id": row["id"],
        "title": row["title"],
        "platform": row.get("platform"),
        "format": row.get("format"),
        "ownership_class": row.get("ownership_class"),
        "igdb_id": row.get("igdb_id"),
        "psn_content_id": row.get("psn_content_id"),
        "price": row.get("price"),
        "acquisition_date": row.get("acquisition_date"),
        "source": row.get("source"),
        "provenance": row.get("provenance"),
    }


def _agg(values: list, generic: set | None = None) -> str:
    generic = generic or set()
    return ", ".join(sorted({v for v in values if v not in generic})) or next(iter(generic), "") or ""


def build_games(
    owned_rows: list[dict[str, Any]],
    *,
    metadata: dict[int, dict] | None = None,
) -> tuple[list[dict[str, Any]], GamesReport]:
    """Group owned_games rows into canonical `games` rows.

    Returns (games, report). `metadata` maps igdb_id -> metadata payload (for
    the is_psvr2 flag from IGDB platform 390).
    """
    metadata = metadata or {}
    report = GamesReport()
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in owned_rows:
        buckets[canonical_title(r.get("normalized_title"))].append(dict(r))

    # Merge buckets that share an IGDB id: two editions of the same game that
    # normalize to different titles (e.g. "Divinity: Original Sin II - Definitive
    # Edition" on PS5 digital and "Divinity: Original Sin 2 - Definitive Edition"
    # on PS physical) are the SAME logical game by igdb_id, so they fold into one
    # card even when the title rules don't line up. idempotent and safe — a shared
    # igdb_id is the definition of one game in the catalog model.
    merged: list[list[dict[str, Any]]] = []
    igdb_owner: dict[int, int] = {}
    for key in sorted(buckets):
        members = buckets[key]
        gids = {m["igdb_id"] for m in members if m.get("igdb_id")}
        target = next((igdb_owner[g] for g in gids if g in igdb_owner), None)
        if target is None:
            target = len(merged)
            merged.append([])
        merged[target].extend(members)
        for g in gids:
            igdb_owner[g] = target

    games: list[dict[str, Any]] = []
    for members in merged:
        base = _base_member(members)
        key = canonical_title(base.get("normalized_title"))

        platforms = {m["platform"] for m in members if m.get("platform") not in _GENERIC_PLATFORMS}
        platform = next(iter(platforms)) if len(platforms) == 1 else (
            ", ".join(sorted(platforms)) if platforms else (base.get("platform") or "playstation")
        )
        igdb_id = base.get("igdb_id") or next((m.get("igdb_id") for m in members if m.get("igdb_id")), None)

        # is_psvr2: true if ANY edition's primary IGDB metadata has platform 390.
        is_psvr2 = 0
        for m in members:
            gid = m.get("igdb_id")
            payload = metadata.get(gid) if gid else None
            if payload and any(
                p.get("id") == 390 for p in (payload.get("platforms") or []) if isinstance(p, dict)
            ):
                is_psvr2 = 1
                break

        dates = [m["acquisition_date"] for m in members if m.get("acquisition_date")]
        earliest = min(dates) if dates else None
        purchased = 1 if any(m.get("ownership_class") == "purchased" for m in members) else 0

        prov: list[str] = []
        for m in members:
            for p in _prov_parts(m.get("provenance")):
                if p not in prov:
                    prov.append(p)

        games.append(
            {
                "title": base["title"],
                "normalized_title": key,
                "igdb_id": igdb_id,
                "platform": platform,
                "platforms": _agg([m["platform"] for m in members], _GENERIC_PLATFORMS) or platform,
                "formats": _agg([m["format"] for m in members]),
                "ownership_classes": _agg([m["ownership_class"] for m in members]),
                "num_editions": len(members),
                "purchased": purchased,
                "earliest_acquisition": earliest,
                "price": base.get("price"),
                "provenance": json.dumps(prov) if prov else None,
                "editions": json.dumps([_edition_summary(m) for m in members]),
                "is_psvr2": is_psvr2,
            }
        )
        report.games += 1
        report.groups.append(
            {"key": key, "title": base["title"], "editions": len(members), "igdb_id": igdb_id}
        )
    return games, report


def _prov_parts(provenance: str | None) -> list[str]:
    """Split a provenance value into its parts (scalar or JSON array)."""
    if not provenance:
        return []
    s = provenance.strip()
    if s.startswith("["):
        try:
            return [p for p in json.loads(s) if isinstance(p, str) and p]
        except (ValueError, TypeError):
            return [s]
    return [s]
