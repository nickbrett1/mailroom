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
    # Dragon's Crown Pro is the PS4 version of Dragon's Crown.
    "dragon's crown pro": "dragon's crown",
    # Guacamelee! 2 Complete is the DLC-complete edition of Guacamelee! 2.
    "guacamelee! 2 complete": "guacamelee! 2",
    # Same game, only the punctuation differs between PSN listings.
    "prince of persia: the lost crown": "prince of persia the lost crown",
    "subnautica: below zero": "subnautica below zero",
    # Valkyria Chronicles Remastered is the PS4 version of Valkyria Chronicles.
    "valkyria chronicles remastered": "valkyria chronicles",
    # Synth Riders + its "80s Mixtape" DLC bundle is the same game.
    "synth riders + 80s mixtape - side a": "synth riders",
    "synth riders + 80s mixtape side a": "synth riders",
    "synth riders + 80s mixtape - side b": "synth riders",
    "synth riders + 80s mixtape side b": "synth riders",
    # Cuphead + its "The Delicious Last Course" DLC bundle is the same game.
    "cuphead & the delicious last course": "cuphead",
    "cuphead and the delicious last course": "cuphead",
    # Steredenn: Binary Stars is the same game as Steredenn (with DLC).
    "steredenn: binary stars": "steredenn",
    # Geometry Wars 3: Dimensions Evolved is the updated version of Geometry Wars 3.
    "geometry wars 3: dimensions": "geometry wars 3: dimensions evolved",
    "geometry wars³: dimensions": "geometry wars 3: dimensions evolved",
    # The Complete First Season == Season 1.
    "the walking dead: the complete first season": "the walking dead: season 1",
    # User only owns the remastered Teslagrad.
    "teslagrad": "teslagrad remastered",
    # The Last of Us Part II Remastered is the same game as Part II (PS5
    # enhanced port of the 2020 PS4 release) — collapse to one card.
    "the last of us part ii remastered": "the last of us part ii",
    "the last of us part 2 remastered": "the last of us part 2",
}

# Edition / bundle markers folded onto the base title. These do NOT denote a
# different game (a Deluxe/Complete/Collection/Enhanced edition is the same
# title), so they're stripped before grouping. The `(?:...)?\s*` prefix is
# tolerant of " - ", " : ", or no separator ("Children of Morta: Complete
# Edition", "Aragami: Shadow Edition", "Burly Men at Sea Maestro Beard
# Edition"). Kept out: "remastered" / "remake" / numbered sequels, which can
# be genuinely distinct titles.
_EDITION_MARKERS = [
    r"\([^)]*\)",                                   # any trailing parenthetical (size / platform / "Full Game..." notes)
    r"(?:[-–—:]?\s*)?super turbo championship edition",
    r"(?:[-–—:]?\s*)?maestro beard edition",
    r"(?:[-–—:]?\s*)?championship edition",
    r"(?:[-–—:]?\s*)?shadow edition",
    r"(?:[-–—:]?\s*)?console edition",
    r"(?:[-–—:]?\s*)?reloaded edition",
    r"(?:[-–—:]?\s*)?royal edition",
    r"(?:[-–—:]?\s*)?founders edition",
    r"(?:[-–—:]?\s*)?the complete edition",
    r"(?:[-–—:]?\s*)?complete edition",
    r"(?:[-–—:]?\s*)?zombies chronicles edition",
    r"(?:[-–—:]?\s*)?standard edition",
    r"(?:[-–—:]?\s*)?tourist edition",
    r"(?:[-–—:]?\s*)?farewell edition",
    r"(?:[-–—:]?\s*)?platinum edition",
    r"(?:[-–—:]?\s*)?jumbo edition",
    r"(?:[-–—:]?\s*)?rivals bundle",
    r"(?:[-–—:]?\s*)?total mayhem bundle",
    r"(?:[-–—:]?\s*)?chicken edition",
    r"(?:[-–—:]?\s*)?mormo's curse",
    r"(?:[-–—:]?\s*)?special edition bundle",
    r"(?:[-–—:]?\s*)?digital deluxe edition",
    r"(?:[-–—:]?\s*)?cross-gen deluxe bundle",
    r"(?:[-–—:]?\s*)?deluxe edition",
    r"(?:[-–—:]?\s*)?launch edition",
    r"(?:[-–—:]?\s*)?definitive edition",
    r"(?:[-–—:]?\s*)?ultimate edition",
    r"(?:[-–—:]?\s*)?gold edition",
    r"(?:[-–—:]?\s*)?special edition",
    r"(?:[-–—:]?\s*)?enhanced edition",
    r"(?:[-–—:]?\s*)?the collection",
    r"(?:[-–—:]?\s*)?new dimension",
    r"(?:[-–—:]?\s*)?ps vita",
    r"(?:[-–—:]?\s*)?\+\s+soca valley",   # Kayak VR: Mirage + Soča Valley (DLC)
    r"(?:[-–—:]?\s*)?episode 1",
    r"(?:[-–—:]?\s*)?episode i",
    r"(?:[-–—:]?\s*)?chapter 1",
    r"(?:[-–—:]?\s*)?chapter i",
]

_ROMAN_TO_ARABIC = {
    "iii": "3", "ii": "2", "iv": "4", "vi": "6", "vii": "7",
    "ix": "9", "v": "5", "x": "10",
}

# Accent folding so "Soča Valley" == "Soca Valley", "Café" == "Cafe", etc.
_ACCENT_MAP = {
    "č": "c", "ć": "c", "š": "s", "ž": "z", "ó": "o", "á": "a",
    "é": "e", "í": "i", "ú": "u", "ü": "u", "ö": "o", "ñ": "n",
}


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def sortable_date(value: str | None) -> str | None:
    """Normalize an acquisition date string to YYYY-MM-DD for sorting.

    Stored dates arrive in mixed formats (MM/DD/YYYY, 'November 23, 2023',
    ISO timestamps), so a plain lexicographic sort orders '04/10/2026' before
    '1/19/2023'. Normalizing to ISO keeps a game's purchase-date sort correct.
    None/unparseable -> None (callers treat as unknown / sort last).
    """
    if not value:
        return None
    s = str(value).strip()
    # ISO date / timestamp: '2021-11-21T04:08:56Z', '2024-04-12…'
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # MM/DD/YYYY (also single-digit: '1/19/2023', '04/10/2026')
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    # 'November 23, 2023' / 'April 10, 2026'
    m = re.match(r"^([A-Za-z]+)[\s.,]+\s*(\d{1,2}),?\s+(\d{4})$", s)
    if m:
        mo = _MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}"
    return None


def _normalize_punct(s: str) -> str:
    out = (s.replace("™", "").replace("®", "").replace("’", "'")
             .replace("‘", "'").replace("–", "-").replace("—", "-"))
    for ch, repl in _ACCENT_MAP.items():
        out = out.replace(ch, repl)
    return out


def _strip_edition_markers(s: str) -> str:
    out = s
    # strip any/all trailing parenthetical groups (e.g. "(Full Game 979 MB)",
    # "(DKO)") before AND after the edition markers so a parenthetical that
    # sits before a marker ("Divine Knockout (DKO) - Founders Edition") is
    # also folded.
    for _ in range(2):
        while re.search(r"\([^)]*\)$", out):
            out = re.sub(r"\s*\([^)]*\)$", "", out).strip()
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
    Game and Add-On Content)"), parenthetical platform/size notes, and
    roman→arabic numerals onto the base title, so "Divinity: Original Sin II -
    Definitive Edition" and "Divinity: Original Sin 2 - Definitive Edition"
    collapse to the same card. Curated EDITION_GROUPS aliases are applied on
    the marker-stripped base.
    """
    norm = _normalize_punct((normalized_title or "").strip().lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    mapped = _strip_edition_markers(norm)
    mapped = EDITION_GROUPS.get(mapped, mapped)
    mapped = _roman_to_arabic(mapped)
    return re.sub(r"\s+", " ", mapped).strip()

# Platforms with no real signal (generic receipt/PSN rows) — collapse to a
# single display platform when that's all a game has.
_GENERIC_PLATFORMS = {None, "", "playstation", "ps"}

# Curated per-game DISPLAY platform overrides, keyed on IGDB id (the catalog's
# stable per-game id). Cross-gen games (shipped on BOTH PS4 & PS5) and PS Vita
# games can't be auto-pinned by _backfill_platform (which only picks PS4 when
# PS5 is absent and vice-versa), so we pin the platform the user actually owns.
# Keyed on igdb_id (NOT title) because the catalog's full titles ("Atari 50:
# The Anniversary Celebration") don't match the short display names. Applied in
# build_games so both `platform` and `platforms` reflect it; owned_games rows
# are left untouched (no dedup-key impact).
PLATFORM_OVERRIDES: dict[int, str] = {
    172672: "playstation 4",    # Aspire: Ina's Tale
    207018: "playstation 5",    # Atari 50: The Anniversary Celebration
    26676:  "playstation 4",    # Kingdom Hearts 0.2: Birth by Sleep - A Fragmentary Passage
    1226:   "playstation 4",    # Kingdom Hearts 3D: Dream Drop Distance
    1224:   "playstation 4",    # Kingdom Hearts Birth by Sleep
    1221:   "playstation 4",    # Kingdom Hearts II
    214986: "playstation 4",    # Kingdom Hearts Re:Chain of Memories
    144770: "playstation 4",    # Life is Strange Remastered Collection
    126290: "playstation 5",    # Far Cry 6
    101006: "playstation 5",    # Gran Turismo 7
    284857: "playstation 5",    # HITMAN World of Assassination Part One
    393742: "playstation 4",    # Kingdom Hearts
    7599:   "playstation 4",    # Life Is Strange
    217623: "playstation 5",    # Like a Dragon: Infinite Wealth
    146851: "playstation 5",    # Lost Judgment
    152249: "playstation 5",    # Marvel's Guardians of the Galaxy
    165347: "playstation 5",    # Marvel's Midnight Suns
    393638: "playstation 5",    # Metal Gear Solid: Master Collection
    327147: "playstation 5",    # Metro Awakening
    249584: "playstation 5",    # Overwatch 2
    24440:  "playstation 4",    # Pankapu
    252647: "playstation 5",    # Persona 3 Reload
    2985:   "playstation vita",  # Persona 4 Golden
    137132: "playstation 5",    # Puyo Puyo Tetris 2
    55163:  "playstation 5",    # Resident Evil Village
    132181: "playstation 5",    # Resident Evil 4
    139896: "playstation 4",    # Shadow of the Colossus
    1218:   "playstation 4",    # Shenmue II
    241444: "playstation 5",    # Sonic Origins Plus
    254337: "playstation 5",    # Star Ocean: The Second Story R
    217909: "playstation 5",    # Suikoden I & II HD Remastered
    890:    "playstation 5",    # Syberia Remastered
    314945: "playstation 5",    # Tales of Graces f Remastered
    472:    "playstation 5",    # The Elder Scrolls V: Skyrim Special Edition
    9732:   "playstation 4",    # The Flame in the Flood
    113114: "playstation 4",    # The Outer Worlds
    152061: "playstation 5",    # Tiny Tina's Wonderlands
    6247:   "playstation 4",    # Trine
    6250:   "playstation 4",    # Trine 2
    68353:  "playstation 5",    # Unicorn Overlord
}

# PSVR2-compatible games, keyed on IGDB id. IGDB platform 390 (PlayStation VR2)
# isn't always present on the base game's metadata payload (e.g. the RE4 / RE
# Village remakes), so pin the flag here. Display platform stays the console it
# runs on (PS5); this only flips the is_psvr2 flag.
PSVR2_OVERRIDES: set[int] = {
    132181,  # Resident Evil 4 (2023 remake)
    55163,   # Resident Evil Village
}


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

        # Curated display overrides (keyed on igdb_id — robust to full catalog
        # titles): pin a cross-gen/Vita game's platform and the PSVR2 flag.
        if igdb_id in PLATFORM_OVERRIDES:
            platform = PLATFORM_OVERRIDES[igdb_id]
        if igdb_id in PSVR2_OVERRIDES:
            is_psvr2 = 1

        dates = [m["acquisition_date"] for m in members if m.get("acquisition_date")]
        sortable = [d for d in (sortable_date(x) for x in dates) if d]
        earliest = min(sortable) if sortable else None
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
                "platforms": (platform if igdb_id in PLATFORM_OVERRIDES
                              else _agg([m["platform"] for m in members], _GENERIC_PLATFORMS) or platform),
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
