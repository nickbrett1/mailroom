"""Cross-check the PSN dump (name + content_id) against the email-derived
digital catalog (owned_games / parsed_purchases from PSN receipts).

Usage:
    python3 scripts/check_psn_dump.py --dump inputs/psn_dump.txt --db sqlite:////tmp/backfill_phase1.db
"""

from __future__ import annotations

import argparse
import re
import unicodedata

from mailroom.db import connect

# content ids: UP0700-CUSA13505_00-ELEVENELEVEN1111, IP9101-NPIA90005_01-..., EP2187-...
CONTENT_ID_RE = re.compile(r"([A-Z]{2}\d{4}-[A-Z0-9]{4,}_\d{2}-[A-Z0-9]+)")

# clear non-game markers (subscriptions, apps, avatars, themes, books, dlc...)
NON_GAME_MARKERS = (
    "playstation plus", "ps plus", "ps+", "subscription", "12-month", "12 month",
    "youtube", "netflix", "plex", "spotify", "disney+", "hulu", "crunchyroll",
    "amazon prime video", "apple tv", "skype", "sony pictures core", "mapcache",
    "avatar", "wrap-up", "wrap up", "theme", "art book", "art of dreams",
    "soundtrack", "ost", "the music of",
    "demo", "beta", "trial", "public beta",
    "dlc", "upgrade", "expansion", "expansion pack", "content pack", "bonus",
    "pre-order", "preorder", "pre order", "episode ", "season 2", "season 20",
    "vr mode", "vr access", "dlc bundle", "character pack", "costume",
    "sets", "starter pack", "photo mode frame", "preload", "avatar pack",
    "premium pack", "chosen dlc", "40th", "dynamic theme", "music collection",
    "xr bodycombat", "les mills",
)

PLATFORM_TOKENS = re.compile(r"(playstation\s*[45]|ps4|ps5|psvita|vita|ps3|psone)", re.IGNORECASE)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode()
    s = PLATFORM_TOKENS.sub(" ", s)
    s = re.sub(r"\([^)]*\)", " ", s, flags=re.IGNORECASE)  # any parenthetical suffix
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s


def parse_dump(path: str) -> list[tuple[str, str | None]]:
    """Entries are 'Name CONTENT_ID'; names may span lines (wrapped).
    Returns (name, content_id | None)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = text.replace("\r\n", "\n")
    entries: list[tuple[str, str | None]] = []
    pos = 0
    for m in CONTENT_ID_RE.finditer(text):
        name = text[pos : m.start()]
        pos = m.end()
        name = re.sub(r"\s*\n\s*", " ", name)  # join wrapped name lines
        name = re.sub(r"\s*-\s*", " - ", name)
        name = re.sub(r"\s+", " ", name).strip(" -\n")
        entries.append((name, m.group(1)))
    # anything after the last content id (e.g. truncated final entry)
    tail = text[pos:].strip()
    if tail:
        entries.append((re.sub(r"\s+", " ", tail).strip(" -"), None))
    return entries


def classify(name: str, content_id: str | None) -> str:
    low = name.lower()
    for m in NON_GAME_MARKERS:
        if m in low:
            return "non-game"
    return "game"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default="inputs/psn_dump.txt")
    ap.add_argument("--db", default="sqlite:////tmp/backfill_phase1.db")
    args = ap.parse_args()

    conn = connect(args.db)
    # PlayStation digital content: Store receipts + code resellers.
    email_titles = {
        norm(r["title"])
        for r in conn.execute(
            """SELECT title FROM parsed_purchases
               WHERE source IN ('psn_receipt', 'cdkeys', 'gameflip')"""
        ).fetchall()
    }
    conn.close()

    def sub_match(name: str) -> bool:
        """Dump names often lack edition/remake/platform suffixes that the
        receipt title carries ('Until Dawn' vs 'Until Dawn 2015'). Accept a
        one-way containment when the shorter side is distinctive enough."""
        n = norm(name)
        if len(n) < 6:
            return False
        return any(n in t or (len(t) < len(n) and t in n) for t in email_titles)

    entries = parse_dump(args.dump)
    matched: list[tuple[str, str | None]] = []
    unmatched_games: list[tuple[str, str | None]] = []
    unmatched_nongame: list[tuple[str, str | None]] = []
    missing_id: list[str] = []
    for name, cid in entries:
        if cid is None:
            missing_id.append(name)
            continue
        if norm(name) in email_titles or sub_match(name):
            matched.append((name, cid))
        elif classify(name, cid) == "non-game":
            unmatched_nongame.append((name, cid))
        else:
            unmatched_games.append((name, cid))

    print(f"PSN dump entries: {len(entries)}  (content-id parsed: {len(entries) - len(missing_id)})")
    print(f"  matched by email receipts : {len(matched)}")
    print(f"  unmatched non-game (apps/DLC/subs/etc): {len(unmatched_nongame)}")
    print(f"  UNMATCHED GAMES           : {len(unmatched_games)}  <-- potential email gaps")
    if missing_id:
        print(f"  entries WITHOUT content id (truncated/wrapped): {len(missing_id)}")
        for n in missing_id:
            print(f"    * {n}")

    print(f"\n--- unmatched GAMES ({len(unmatched_games)}) ---")
    for name, cid in sorted(unmatched_games):
        print(f"  {cid}  {name}")

    print(f"\n--- unmatched non-game sample ({min(len(unmatched_nongame), 30)} shown) ---")
    for name, cid in sorted(unmatched_nongame)[:30]:
        print(f"  {cid}  {name}")


if __name__ == "__main__":
    main()
