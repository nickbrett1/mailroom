"""PS+ Essential monthly lookup + acquisition_date backfill.

Design (memos/psplus-essentials-acquisition-date-backfill): PS+ Essential
monthly claims carry no acquisition_date (no Sony receipt email, and the PSN
API returns no per-claim date). For the Essential **monthly** class the claim
window equals the availability window: each month's titles are claimable from
the first Tuesday until the next month's batch replaces them. So we can't know
"when I claimed it," but we CAN know the structural offer date:
`acquisition_date = essentials_lineup.available_from` (first Tuesday).

This module is the seed + the enricher. `ESSENTIALS_MONTHS` is the curated
asset we own (no clean third-party dataset exists — memos 2026-08 finding):
one record per offer month with its claim window and the (title, platform)
games offered. `seed_essentials_lineup` loads it into `essentials_lineup`;
`enrich_psplus_claim_dates` writes acquisition_date back onto owned_games rows.

Safety rules (from the memo):
- ONLY `ownership_class='psplus_claimed'` AND `format='digital'` rows are
  eligible. Physical rows never get a monthly date (you can't "claim" a
  monthly digitally-as-physical); `purchased` / `psplus_extra` untouched.
- Only rows that actually appear in the lineup get a date. Freebies / demos /
  Extra-catalog titles that were never an Essentials monthly stay undated and
  are REPORTED (never guessed).
- Idempotent: only sets acquisition_date where it is not already set.
"""

from __future__ import annotations

import json
import sqlite3

from mailroom.db import checkpoint_wal, enqueue_review
from mailroom.verticals.game_catalog.parsers.psn import normalize_title

# ---------------------------------------------------------------------------
# Curated seed: PS+ Essential monthly lineups (title, platform) per offer month
# with the claim window. Platform uses the SAME strings owned_games.platform
# carries ('playstation 5', 'playstation 4', ...). available_from is the first
# Tuesday of the month; available_to is the end of the claim window.
# Populated month-by-month from Sony's announcements (authoritative).
# ---------------------------------------------------------------------------
ESSENTIALS_MONTHS: list[dict] = [
    # 2024-01-02
    {
        'month': "2024-01", 'available_from': "2024-01-02", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("A Plague Tale: Requiem", "playstation 5", 152242),
            ("Evil West", "playstation 4", 141547),
            ("Nobody Saves the World", "playstation 4", 145089),
        ],
    },
    # 2026-02-03
    {
        'month': "2026-02", 'available_from': "2026-02-03", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("ACE COMBAT™ 7: SKIES UNKNOWN", "playstation 4", 14758),
            ("Subnautica Below Zero", "playstation 4", 107315),
            ("Ultros", "playstation 4", 250626),
            ("Undisputed", "playstation 5", 146957),
        ],
    },
    # 2024-06-04
    {
        'month': "2024-06", 'available_from': "2024-06-04", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("AEW: Fight Forever", "playstation 4", 145216),
            ("SpongeBob SquarePants: The Cosmic Shake", "playstation 4", 171219),
        ],
    },
    # 2025-05-06
    {
        'month': "2025-05", 'available_from': "2025-05-06", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("ARK Survival Ascended", "playstation 5", 250509),
            ("Balatro", "playstation 4", 251833),
            ("Warhammer 40,000: Boltgun", "playstation 4", 203258),
        ],
    },
    # 2023-07-04
    {
        'month': "2023-07", 'available_from': "2023-07-04", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Alan Wake Remastered", "playstation 4", 167611),
            ("Call of Duty®: Black Ops Cold War", "playstation 4", 137001),
            ("Endling", "playstation 4", 386652),
        ],
    },
    # 2024-12-03
    {
        'month': "2024-12", 'available_from': "2024-12-03", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Aliens: Dark Descent", "playstation 4", 204354),
            ("It Takes Two", "playstation 4", 135243),
            ("Temtem", "playstation 5", 100357),
        ],
    },
    # 2023-11-07
    {
        'month': "2023-11", 'available_from': "2023-11-07", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Aliens: Fireteam Elite", "playstation 4", 144153),
            ("DRAGON BALL: THE BREAKERS", "playstation 4", 182179),
            ("Mafia II: Definitive Edition", "playstation 4", 134071),
        ],
    },
    # 2025-06-03
    {
        'month': "2025-06", 'available_from': "2025-06-03", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Alone in the Dark", "playstation 5", 213237),
            ("Bomb Rush Cyberfunk", "playstation 4", 135940),
            ("NBA 2K25", "playstation 4", 308034),
        ],
    },
    # 2024-07-02
    {
        'month': "2024-07", 'available_from': "2024-07-02", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Among Us", "playstation 4", 111469),
            ("Borderlands® 3", "playstation 4", 19164),
            ("NHL® 24", "playstation 5", 261413),
            ("NHL® 24", "playstation 4", 261413),
        ],
    },
    # 2023-09-05
    {
        'month': "2023-09", 'available_from': "2023-09-05", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Black Desert", "playstation 4", 6292),
            ("Generation Zero", "playstation 4", 65445),
            ("Saints Row", "playstation 4", 165346),
        ],
    },
    # 2023-05-02
    {
        'month': "2023-05", 'available_from': "2023-05-02", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Chivalry 2", "playstation 4", 119342),
            ("Descenders", "playstation 4", 52200),
            ("GRID Legends", "playstation 4", 159116),
        ],
    },
    # 2025-10-07
    {
        'month': "2025-10", 'available_from': "2025-10-07", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Cocoon", "playstation 4", 204627),
            ("Goat Simulator 3", "playstation 4", 204360),
        ],
    },
    # 2026-01-06
    {
        'month': "2026-01", 'available_from': "2026-01-06", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Core Keeper", "playstation 5", 152127),
            ("Core Keeper", "playstation 4", 152127),
            ("Disney Epic Mickey: Rebrushed", "playstation 4", 287849),
            ("Need for Speed™ Unbound", "playstation 5", 219442),
        ],
    },
    # 2024-11-05
    {
        'month': "2024-11", 'available_from': "2024-11-05", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("DEATH NOTE Killer Within", "playstation 4", 320363),
            ("Ghostwire: Tokyo", "playstation 5", 119308),
            ("HOT WHEELS UNLEASHED™ 2 - Turbocharged", "playstation 4", 251471),
        ],
    },
    # 2025-08-05
    {
        'month': "2025-08", 'available_from': "2025-08-05", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("DayZ", "playstation 4", 2117),
            ("Lies of P", "playstation 4", 148241),
            ("MY HERO ONE'S JUSTICE 2", "playstation 4", 122599),
        ],
    },
    # 2024-10-01
    {
        'month': "2024-10", 'available_from': "2024-10-01", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Dead Space™", "playstation 5", 159119),
            ("Doki Doki Literature Club Plus", "playstation 4", 152122),
            ("WWE 2K24", "playstation 4", 283600),
        ],
    },
    # 2025-07-01
    {
        'month': "2025-07", 'available_from': "2025-07-01", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Diablo IV", "playstation 4", 125165),
            ("THE KING OF FIGHTERS XV", "playstation 4", 113104),
        ],
    },
    # 2025-04-01
    {
        'month': "2025-04", 'available_from': "2025-04-01", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Digimon Story: Cyber Sleuth - Hacker's Memory", "playstation 4", 27920),
            ("The Texas Chain Saw Massacre", "playstation 4", 185238),
        ],
    },
    # 2023-01-03
    {
        'month': "2023-01", 'available_from': "2023-01-03", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Divine Knockout", "playstation 4", 204408),
        ],
    },
    # 2025-03-04
    {
        'month': "2025-03", 'available_from': "2025-03-04", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Dragon Age™: The Veilguard", "playstation 5", 30208),
            ("Sonic Colors: Ultimate", "playstation 4", 150005),
            ("Teenage Mutant Ninja Turtles: The Cowabunga Collection", "playstation 4", 194206),
        ],
    },
    # 2023-08-01
    {
        'month': "2023-08", 'available_from': "2023-08-01", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Dreams™", "playstation 4", 11155),
            ("PGA TOUR® 2K23", "playstation 4", 214135),
        ],
    },
    # 2024-06-18
    {
        'month': "2024-06", 'available_from': "2024-06-18", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("EA SPORTS FC 24", "playstation 4", 256092),
        ],
    },
    # 2026-05-05
    {
        'month': "2026-05", 'available_from': "2026-05-05", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("EA SPORTS FC 26", "playstation 4", 353848),
            ("Nine Sols", "playstation 4", 194821),
            ("WUCHANG: Fallen Feathers", "playstation 5", 171225),
        ],
    },
    # 2025-11-04
    {
        'month': "2025-11", 'available_from': "2025-11-04", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("EA SPORTS™ WRC", "playstation 5", 264412),
            ("Stray", "playstation 4", 110248),
            ("Totally Accurate Battle Simulator", "playstation 4", 21642),
        ],
    },
    # 2024-03-05
    {
        'month': "2024-03", 'available_from': "2024-03-05", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("F1® 23", "playstation 4", 240558),
            ("Hello Neighbor 2", "playstation 4", 135991),
            ("Sifu", "playstation 4", 144022),
        ],
    },
    # 2024-02-06
    {
        'month': "2024-02", 'available_from': "2024-02-06", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("FOAMSTARS", "playstation 4", 250623),
            ("Steelrising", "playstation 5", 135670),
            ("ROLLERDROME", "playstation 4", 203366),
        ],
    },
    # 2023-10-03
    {
        'month': "2023-10", 'available_from': "2023-10-03", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Farming Simulator 22", "playstation 4", 146112),
            ("The Callisto Protocol™", "playstation 4", 141538),
            ("Weird West", "playstation 4", 127350),
        ],
    },
    # 2024-05-07
    {
        'month': "2024-05", 'available_from': "2024-05-07", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Ghostrunner 2", "playstation 5", 250617),
        ],
    },
    # 2024-09-03
    {
        'month': "2024-09", 'available_from': "2024-09-03", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Harry Potter: Quidditch Champions", "playstation 4", 246166),
            ("Little Nightmares II", "playstation 4", 121760),
            ("MLB® The Show™ 24", "playstation 4", 282900),
        ],
    },
    # 2025-02-04
    {
        'month': "2025-02", 'available_from': "2025-02-04", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("High on Life", "playstation 4", 204618),
            ("PAC-MAN WORLD Re-PAC", "playstation 4", 206811),
            ("PAYDAY 3", "playstation 5", 27270),
        ],
    },
    # 2024-04-02
    {
        'month': "2024-04", 'available_from': "2024-04-02", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Immortals of Aveum", "playstation 5", 228531),
            ("Minecraft Legends", "playstation 4", 307731),
            ("Skul", "playstation 4", 127842),
        ],
    },
    # 2023-06-06
    {
        'month': "2023-06", 'available_from': "2023-06-06", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Jurassic World Evolution 2", "playstation 4", 152064),
            ("NBA 2K23", "playstation 4", 207393),
            ("Trek To Yomi", "playstation 4", 152203),
        ],
    },
    # 2025-12-02
    {
        'month': "2025-12", 'available_from': "2025-12-02", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Killing Floor 3", "playstation 5", 262530),
            ("LEGO® Horizon Adventures™", "playstation 5", 305003),
            ("SYNDUALITY Echo of Ada", "playstation 5", 217601),
            ("The Outlast Trials", "playstation 4", 127165),
        ],
    },
    # 2023-12-05
    {
        'month': "2023-12", 'available_from': "2023-12-05", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("LEGO® 2K Drive", "playstation 4", 242493),
            ("PowerWash Simulator", "playstation 4", 138590),
            ("Sable", "playstation 5", 79995),
        ],
    },
    # 2026-04-07
    {
        'month': "2026-04", 'available_from': "2026-04-07", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Lords of the Fallen", "playstation 5", 21593),
            ("SWORD ART ONLINE Fractured Daydream", "playstation 5", 287852),
            ("Tomb Raider I-III Remastered Starring Lara Croft", "playstation 5", 266683),
            ("Tomb Raider I-III Remastered Starring Lara Croft", "playstation 4", 266683),
        ],
    },
    # 2023-04-04
    {
        'month': "2023-04", 'available_from': "2023-04-04", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Meet Your Maker", "playstation 4", 212710),
            ("Sackboy™: A Big Adventure", "playstation 4", 134585),
            ("Tails of Iron", "playstation 4", 116422),
        ],
    },
    # 2026-03-03
    {
        'month': "2026-03", 'available_from': "2026-03-03", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Monster Hunter Rise", "playstation 4", 138950),
            ("PGA TOUR® 2K25", "playstation 5", 328079),
            ("Slime Rancher 2", "playstation 5", 152243),
            ("The Elder Scrolls Online", "playstation 4", 1081),
        ],
    },
    # 2025-01-07
    {
        'month': "2025-01", 'available_from': "2025-01-07", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Need For Speed™ Hot Pursuit Remastered", "playstation 4", 139346),
            ("Suicide Squad: Kill the Justice League", "playstation 5", 136627),
        ],
    },
    # 2021-04-06
    {
        'month': "2021-04", 'available_from': "2021-04-06", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Oddworld: Soulstorm Enhanced Edition", "playstation 5", 178211),
        ],
    },
    # 2022-02-01
    {
        'month': "2022-02", 'available_from': "2022-02-01", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Planet Coaster", "playstation 5", 18829),
        ],
    },
    # 2025-09-02
    {
        'month': "2025-09", 'available_from': "2025-09-02", 'available_to': None,
        'source': 'playstation-fandom-NA', 'games': [
            ("Psychonauts 2", "playstation 4", 14741),
            ("Stardew Valley", "playstation 4", 17000),
        ],
    },
]



def lineup_row(month: dict, title: str, platform: str, igdb_id: int | None = None) -> dict:
    """Expand one (title, platform[, igdb_id]) into an essentials_lineup row.

    igdb_id is the IGDB match for the game when known — the ROBUST join key
    (owned_games.igdb_id), used before the fallback normalized_title+platform
    join (title normalization drifts between catalog source strings).
    """
    return {
        "month": month["month"],
        "title": title,
        "normalized_title": normalize_title(title),
        "platform": platform,
        "available_from": month.get("available_from"),
        "available_to": month.get("available_to"),
        "igdb_id": igdb_id,
        "source": month.get("source", "seed"),
    }


def seed_essentials_lineup(conn: sqlite3.Connection) -> int:
    """Insert every ESSENTIALS_MONTHS row idempotently.

    Each game is (title, platform[, igdb_id]). Returns the number of NEW rows
    inserted (0 on re-seed -> no-op)."""
    seen: set[tuple] = set()
    inserted = 0
    for month in ESSENTIALS_MONTHS:
        for game in month.get("games", []):
            title, platform = game[0], game[1]
            igdb_id = game[2] if len(game) > 2 else None
            row = lineup_row(month, title, platform, igdb_id)
            key = (row["normalized_title"], row["platform"], row["available_from"])
            if key in seen:
                continue
            seen.add(key)
            cur = conn.execute(
                """INSERT OR IGNORE INTO essentials_lineup
                   (month, title, normalized_title, platform, available_from,
                    available_to, igdb_id, source)
                   VALUES (:month, :title, :normalized_title, :platform,
                           :available_from, :available_to, :igdb_id, :source)""",
                row,
            )
            inserted += cur.rowcount
    conn.commit()
    checkpoint_wal(conn)
    return inserted


def enrich_psplus_claim_dates(conn: sqlite3.Connection) -> dict:
    """Backfill acquisition_date on Essentials-monthly claims from the lineup.

    Eligible: owned, digital, ownership_class='psplus_claimed', no date yet.
    Join on (igdb_id, platform) first (robust), falling back to
    (normalized_title, platform) -> set acquisition_date = available_from.

    Idempotent: skips rows that already have a date. Unmatched eligible claims
    (not found in the lineup: freebies/demos/Extra-catalog) are REPORTED to
    review_queue, never guessed. Returns a counts dict.
    """
    report = {"dated": 0, "already_dated": 0, "unmatched": 0}
    rows = conn.execute(
        """SELECT id, title, normalized_title, platform, igdb_id, acquisition_date
           FROM owned_games
           WHERE is_owned = 1 AND ownership_class = 'psplus_claimed'
             AND format = 'digital'"""
    ).fetchall()
    for row in rows:
        if row["acquisition_date"]:
            report["already_dated"] += 1
            continue
        match = None
        if row["igdb_id"]:
            match = conn.execute(
                """SELECT available_from FROM essentials_lineup
                   WHERE igdb_id = ? AND platform = ?
                   ORDER BY available_from LIMIT 1""",
                (row["igdb_id"], row["platform"]),
            ).fetchone()
        if not match:
            match = conn.execute(
                """SELECT available_from FROM essentials_lineup
                   WHERE normalized_title = ? AND platform = ?
                   ORDER BY available_from LIMIT 1""",
                (row["normalized_title"], row["platform"]),
            ).fetchone()
        if not match:
            report["unmatched"] += 1
            enqueue_review(
                conn,
                {
                    "source": "essentials_lineup",
                    "order_number": str(row["id"]),
                    "title": row["title"],
                    "reason": "psplus_claimed_no_essentials_month",
                    "payload": json.dumps(
                        {"platform": row["platform"], "normalized_title": row["normalized_title"]}
                    ),
                },
            )
            continue
        conn.execute(
            """UPDATE owned_games
               SET acquisition_date = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (match["available_from"], row["id"]),
        )
        report["dated"] += 1
    conn.commit()
    checkpoint_wal(conn)
    return report
