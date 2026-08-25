"""Catalog data-quality repairs — idempotent, audited one-time fixes.

The pipeline heals most wrong picks automatically (exact-name/platform
matcher, dedup-by-IGDB), but a few bad rows need explicit surgery that no
re-match can undo:

  1. CANCELLED-ORDER JUNK  — an Amazon cancellation email whose subject tail
     was swallowed into the item title ("Master Plunger MPS4 Sink ...\" has
     been canceled") and the 'MPS4' substring classified it as a PS game.
  2. NON-GAME ADD-ONS      — Dreams OST ('The Music of Dreams',
     DREAMSOST…) and Dreams Art Book ('The Art of Dreams', DREAMSARTBOOK…)
     entered the catalog via the PSN API and got matched to random IGDB games.
  3. JAMMED CONTENT IDS    — a wrong IGDB match (GODS Remastered ->
     God of War Ragnarök) let dedup merge TWO games into one row with a
     comma-joined psn_content_id, Ragnarök's 94.6 metadata and the GODS
     title. Split back into their correct rows.
  4. AMBIGUOUS SHORT TITLE — 'Unplugged' (VR air guitar) matched to
     'Rock Band Unplugged' (2009 PSP) by the results[0] fallback; the correct
     IGDB entry is pinned by content id.

Repairs are keyed on stable facts (content ids / exact titles) so re-runs are
no-ops. Nothing is ever deleted: bad rows are RETIRED (is_owned=0 +
retire_reason) and merged rows are SPLIT with provenance preserved. Every
action is audited to review_queue (status='resolved').
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from mailroom.db import merge_provenance, provenance_parts
from mailroom.verticals.game_catalog.parsers.psn import normalize_title

# --- non-game add-ons: (content-id marker, retire reason) -------------------
# The PSN title library lists Dreams' OST / art book under their own names
# ('The Music of Dreams', 'The Art of Dreams') — they are not games and have
# no IGDB entry; the name markers ('the music of', 'artbook') live in the
# ingestion filter (clients._CONTENT_RE), this catches rows already stored.
NON_GAME_CONTENT_MARKERS = (
    ("DREAMSOST", "dreams_ost"),
    ("DREAMSARTBOOK", "dreams_artbook"),
)

# --- cancelled-order junk: the swallowed subject tail ------------------------
_CANCELLED_TITLE_RE = re.compile(r"\bcancel", re.IGNORECASE)

# --- jammed content-id splits ------------------------------------------------
# Merged rows carried TWO games' content ids because a wrong IGDB match let
# dedup collapse them. Verified against the archive (msgvault):
#   - PSN receipt 252319130093 (2021-03-17): GODS Remastered $3.99
#   - CDKeys 0151509331 (2023-01-27): God of War Ragnarök PS5 (US) $47.99
#   - PSN receipt 293544327031 (2021-09-19): Super Meat Boy $2.99
#   - PSN receipt 786997197463109 (2026-01-06): Super Meat Boy Forever $2.49
#   - PSN receipt 253086790958 (2021-03-04): FINAL FANTASY VII REMAKE $0.00
#     (PS+ Essential claim — ownership_class psplus_claimed)
SPLIT_OVERRIDES = {
    "UP3909-CUSA15513_00-GODSREMASTERED00": {
        "title": "GODS Remastered",
        "platform": "playstation 4",
        "igdb_id": 112099,  # IGDB 'Gods Remastered' (2018)
        "acquisition_date": "03/17/2021",
        "price": "$3.99",
    },
    "UP9000-PPSA08329_00-GOWRAGNAROK00000": {
        "title": "God of War Ragnarök",
        "platform": "playstation 5",
        "igdb_id": 112875,  # IGDB 'God of War Ragnarök'
        "acquisition_date": "01/27/2023",
        "price": "$47.99",
    },
    "UP1055-CUSA16602_00-SUPERMEATBOYFORE": {
        "title": "Super Meat Boy Forever",
        "platform": "playstation 4",
        "igdb_id": 44129,  # IGDB 'Super Meat Boy Forever' (2020)
        "acquisition_date": "01/06/2026",
        "price": "$2.49",
    },
    "UP1055-CUSA02845_00-SUPERMEATBOY0000": {
        "title": "Super Meat Boy",
        "platform": "playstation 4",
        "igdb_id": 885,  # IGDB 'Super Meat Boy' (2010)
        "acquisition_date": "09/19/2021",
        "price": "$2.99",
    },
    "UP0082-CUSA07211_00-FFVIIREMAKE00000": {
        "title": "FINAL FANTASY VII REMAKE",
        "platform": "playstation 4",
        "igdb_id": 11169,  # IGDB 'Final Fantasy VII Remake' (2020)
        "ownership_class": "psplus_claimed",  # PS+ Essential claim ($0 receipt)
        "acquisition_date": "03/04/2021",
        "price": "$0.00",
    },
    "UP0082-CUSA01875_00-FINALFANTASY7ZZZ": {
        "title": "FINAL FANTASY VII",
        "platform": "playstation 4",
        "igdb_id": 207026,  # IGDB 'Final Fantasy VII' (PS4 port of the 1997 game)
    },
    "UP1003-CUSA02218_00-DISHONOREDGAMENA": {
        "title": "Dishonored® Definitive Edition",
        "platform": "playstation 4",
        "igdb_id": 20863,  # IGDB 'Dishonored: Definitive Edition' (PS4, 2015)
        "acquisition_date": "03/08/2023",
        "price": "$4.99",
    },
    "UP1003-CUSA03642_00-DISHONORED2NAMER": {
        "title": "Dishonored 2",
        "platform": "playstation 4",
        "igdb_id": 11118,  # IGDB 'Dishonored 2' (2016)
        "acquisition_date": "12/08/2023",
        "price": "$2.99",
    },
}

# Provenance refs that don't carry the content id (receipt/cdkeys order
# numbers) -> which game's content id they belong to.
_ORDER_TO_CID = {
    "252319130093": "UP3909-CUSA15513_00-GODSREMASTERED00",  # PSN receipt GODS Remastered
    "0151509331": "UP9000-PPSA08329_00-GOWRAGNAROK00000",    # CDKeys God of War Ragnarök
    "293544327031": "UP1055-CUSA02845_00-SUPERMEATBOY0000",  # PSN receipt Super Meat Boy
    "786997197463109": "UP1055-CUSA16602_00-SUPERMEATBOYFORE",  # PSN receipt Super Meat Boy Forever
    "253086790958": "UP0082-CUSA07211_00-FFVIIREMAKE00000",  # PSN receipt FFVII REMAKE ($0 PS+ claim)
    "407682352826": "UP1003-CUSA02218_00-DISHONOREDGAMENA",  # PSN receipt Dishonored® Definitive Edition
    "490700637631": "UP1003-CUSA03642_00-DISHONORED2NAMER",   # PSN receipt Dishonored 2
}

# --- collection bundles split into their member games -----------------------
# A PSN/retailer collection bundle (Batman: Arkham Collection, BioShock: The
# Collection, Shadowrun Trilogy, Hotline Miami Collection) shows up as ONE row /
# ONE card, but the user wants each constituent game as its own entry with its
# own cover art. Keyed on the collection's IGDB id -> member games. Members
# already owned (same igdb_id or canonical grouping key, e.g. BioShock Infinite
# already owned as 'The Complete Edition') get the collection's receipt merged
# into their provenance; unowned members get a fresh row. The collection row is
# retired (nothing is deleted).
COLLECTION_SPLITS: dict[int, list[dict]] = {
    112659: [  # Batman: Arkham Collection
        {"title": "Batman: Arkham Asylum", "igdb_id": 500, "platform": "playstation 4"},
        {"title": "Batman: Arkham City", "igdb_id": 501, "platform": "playstation 4"},
        {"title": "Batman: Arkham Knight", "igdb_id": 5503, "platform": "playstation 4"},
    ],
    19839: [  # BioShock: The Collection (2016 remasters)
        {"title": "BioShock Remastered", "igdb_id": 34293, "platform": "playstation 4"},
        {"title": "BioShock 2 Remastered", "igdb_id": 34294, "platform": "playstation 4"},
        {"title": "BioShock Infinite", "igdb_id": 538, "platform": "playstation 4"},
    ],
    154181: [  # Shadowrun Trilogy
        {"title": "Shadowrun Returns", "igdb_id": 3020, "platform": "playstation 4"},
        {"title": "Shadowrun: Dragonfall", "igdb_id": 22652, "platform": "playstation 4"},
        {"title": "Shadowrun: Hong Kong", "igdb_id": 11772, "platform": "playstation 4"},
    ],
    99733: [  # Hotline Miami Collection
        {"title": "Hotline Miami", "igdb_id": 1384, "platform": "playstation 4"},
        {"title": "Hotline Miami 2: Wrong Number", "igdb_id": 2126, "platform": "playstation 4"},
    ],
    106988: [  # Persona Dancing: Endless Night Collection
        {"title": "Persona 3: Dancing in Moonlight", "igdb_id": 54217, "platform": "playstation 4"},
        {"title": "Persona 4: Dancing All Night", "igdb_id": 11056, "platform": "playstation 4"},
        {"title": "Persona 5: Dancing in Starlight", "igdb_id": 54218, "platform": "playstation 4"},
    ],
    168670: [  # Uncharted: Legacy of Thieves Collection (PS5 remasters of
               # Uncharted 4: A Thief's End + Uncharted: The Lost Legacy). The
               # collection card hides The Lost Legacy, so it is broken out as
               # its own entry. Uncharted 4 is already owned (PS+ PS4), so that
               # member merges into it; Lost Legacy gets a fresh PS5 row.
        {"title": "Uncharted 4: A Thief's End", "igdb_id": 7331, "platform": "playstation 5"},
        {"title": "Uncharted: The Lost Legacy", "igdb_id": 26193, "platform": "playstation 5"},
    ],
    393638: [  # Metal Gear Solid: Master Collection Vol. 1 (PS5, 2023). One
               # bundle hiding three classic games — break out each MGS so they
               # get their own card/cover. The member ids are the canonical
               # base entries (MGS 1998 / MGS2 / MGS3), not the 'Master
               # Collection Version' sub-entries.
        {"title": "Metal Gear Solid", "igdb_id": 375, "platform": "playstation 5"},
        {"title": "Metal Gear Solid 2: Sons of Liberty", "igdb_id": 376, "platform": "playstation 5"},
        {"title": "Metal Gear Solid 3: Snake Eater", "igdb_id": 379, "platform": "playstation 5"},
    ],
    # Final Fantasy I-VI (Anniversary Edition / Pixel Remaster collection).
    # The owned row ('FINAL FANTASY I-VI Collection Anniversary Edition') is
    # currently UNMATCHED (igdb_id NULL — no clean IGDB bundle entry), so it is
    # split by title (COLLECTION_TITLES below), never by a real igdb_id. The
    # key 0 is a sentinel: `WHERE igdb_id = 0` matches nothing, keeping the
    # split title-only (a guessed id could falsely match an unrelated game).
    0: [
        {"title": "Final Fantasy", "igdb_id": 158980, "platform": "playstation 5"},
        {"title": "Final Fantasy II", "igdb_id": 158981, "platform": "playstation 5"},
        {"title": "Final Fantasy III", "igdb_id": 158982, "platform": "playstation 5"},
        {"title": "Final Fantasy IV", "igdb_id": 158983, "platform": "playstation 5"},
        {"title": "Final Fantasy V", "igdb_id": 158984, "platform": "playstation 5"},
        {"title": "Final Fantasy VI", "igdb_id": 158985, "platform": "playstation 5"},
    ],
}


# Title substrings used as a FALLBACK to match a collection row whose igdb_id
# is NULL / differs from the COLLECTION_SPLITS key in a given DB. Keyed on the
# same igdb_id as COLLECTION_SPLITS. The split fires if a row's igdb_id equals
# the key OR its title contains one of these fragments (case-insensitive).
COLLECTION_TITLES: dict[int, list[str]] = {
    112659: ["batman: arkham collection"],
    19839: ["bioshock: the collection"],
    154181: ["shadowrun trilogy"],
    99733: ["hotline miami collection"],
    106988: ["persona dancing: endless night collection"],
    168670: ["uncharted: legacy of thieves collection"],
    393638: ["metal gear solid master collection"],
    # Final Fantasy collection split is title-driven (igdb_id is NULL / key 0):
    # both the full Anniversary Edition title and the generic 'final fantasy
    # pixel remaster' fragment catch the physical + digital bundles.
    0: [
        "final fantasy i-vi collection anniversary edition",
        "final fantasy pixel remaster",
        "final fantasy i-vi collection",
    ],
}


def _existing_owned_by_key(conn, member: dict) -> dict | None:
    """An owned row that already represents this member game (same igdb_id, or
    the same canonical grouping key so 'BioShock Infinite' matches an already-
    owned 'BioShock Infinite: The Complete Edition'), else None."""
    from mailroom.verticals.game_catalog.game_groups import canonical_title

    if member.get("igdb_id"):
        row = conn.execute(
            "SELECT * FROM owned_games WHERE is_owned = 1 AND igdb_id = ?",
            (member["igdb_id"],),
        ).fetchone()
        if row:
            return dict(row)
    key = canonical_title(member["title"])
    for r in conn.execute("SELECT * FROM owned_games WHERE is_owned = 1").fetchall():
        if canonical_title(r["normalized_title"]) == key:
            return dict(r)
    return None


def _split_collection(conn, row, members: list[dict], report: RepairReport) -> None:
    """Retire a collection receipt row and ensure each member game is owned."""
    from mailroom.db import merge_provenance

    _retire(conn, row, "collection_split")
    _audit(conn, row["id"], row["title"], "split_collection",
           f"collection broken into {len(members)} member games")
    report.retired.append({"id": row["id"], "title": row["title"], "reason": "collection_split"})
    prov_parts = provenance_parts(row["provenance"])
    for m in members:
        existing = _existing_owned_by_key(conn, m)
        prov = merge_provenance(existing["provenance"] if existing else None,
                                ", ".join(prov_parts) if prov_parts else row["provenance"])
        if existing:
            conn.execute(
                """UPDATE owned_games SET provenance = ?, price = COALESCE(?, price),
                   updated_at = datetime('now') WHERE id = ?""",
                (prov, row["price"], existing["id"]),
            )
            _audit(conn, existing["id"], m["title"], "merge_collection_member",
                   f"member of '{row['title']}' already owned — collection receipt merged into provenance")
            report.merged.append({"id": existing["id"], "title": m["title"], "source": "collection_split"})
        else:
            cur = conn.execute(
                """INSERT INTO owned_games
                   (title, normalized_title, platform, format, ownership_class, retailer,
                    order_number, item_id, condition, psn_content_id, igdb_id,
                    acquisition_date, price, source, source_ref, status, is_owned, provenance)
                   VALUES (?, ?, ?, ?, 'purchased', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 'owned', 1, ?)""",
                (m["title"], normalize_title(m["title"]), m["platform"], row["format"],
                 row["retailer"], None, None, None,
                 m["igdb_id"], row["acquisition_date"], row["price"], row["source"],
                 f"collection:{row['id']}", prov),
            )
            new_id = cur.lastrowid
            if m["igdb_id"]:
                conn.execute(
                    """INSERT OR IGNORE INTO igdb_matches(owned_game_id, igdb_id, confidence, matched_title)
                       VALUES (?, ?, 'manual', ?)""",
                    (new_id, m["igdb_id"], m["title"]),
                )
            _audit(conn, new_id, m["title"], "split_collection_member",
                   f"member broken out of collection '{row['title']}' (owned row {row['id']})")
            report.split.append({"id": new_id, "title": m["title"], "kept_cid": None, "split_cids": []})


# --- ambiguous short titles pinned by content id ----------------------------
# 'Unplugged' (VR air guitar, 2021) — IGDB search ranks 'Rock Band Unplugged'
# (2009 PSP) first and the single-token title is too ambiguous to auto-pick.
MATCH_OVERRIDES = {
    "UP3535-PPSA14005_00-0297859396070977": 153854,  # IGDB 'Unplugged' (VR air guitar)
    "UP1162-CUSA03610_00-CRYPTNECRODANCER": 7886,  # IGDB 'Crypt of the NecroDancer' (base game, 2015), not the 'Synchrony' DLC (212583)
}

# IGDB match overrides for receipt rows that carry NO psn_content_id (so
# MATCH_OVERRIDES can't reach them), keyed on a symbol-stripped normalized
# title. IGDB's search ranks DLC skins before the base game and never surfaces
# the base entry ('Batman: Arkham Knight' -> only '... Skin' DLC packs), so the
# wrong art needs a manual pin. Applied in apply_catalog_repairs step 4b.
TITLE_MATCH_OVERRIDES = {
    # Receipt row 'Batman™: Arkham Knight (Game)' -> base game (2015), not a
    # skin DLC (26041). IGDB id 5503 = 'Batman: Arkham Knight' (2015).
    "batman arkham knight": 5503,
    # 'Beyond A Steel Sky: Beyond A SteelBook Edition (PS5)' (Amazon order
    # 113-7134038-7289042) -> the SteelBook Edition (171279), which IGDB search
    # can't surface from the noisy Amazon title.
    "beyond a steel sky beyond a steelbook edition": 171279,
    # 'Subnautica' (base game, 2018) — the matcher has been landing it on
    # 'Subnautica: Below Zero' (107315), so the base game's card shows the
    # Below Zero cover. Pin it to its own IGDB entry (9254) so each game keeps
    # its own artwork.
    "subnautica": 9254,
    # 'Synth Riders' — IGDB search can surface a PS5-only re-listing (372492)
    # ahead of the canonical entry. Pin to the base game (105333) so the row
    # carries the IGDB platform-390 (PSVR2) signal.
    "synth riders": 105333,
}


from mailroom.verticals.game_catalog.game_groups import canonical_title


def _title_match_key(normalized_title: str | None) -> str:
    """Symbol-stripped key for TITLE_MATCH_OVERRIDES (normalized titles keep
    '™'/colons, e.g. 'batman™: arkham knight')."""
    s = (normalized_title or "").lower()
    s = re.sub(r"[™®©&()\[\]:,;]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class RepairReport:
    retired: list[dict] = field(default_factory=list)
    split: list[dict] = field(default_factory=list)
    rematched: list[dict] = field(default_factory=list)
    merged: list[dict] = field(default_factory=list)
    cleaned: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _retire(conn, row, reason: str) -> None:
    conn.execute(
        """UPDATE owned_games SET is_owned = 0, status = 'retired',
           retire_reason = ?, updated_at = datetime('now') WHERE id = ?""",
        (reason, row["id"]),
    )


def _audit(conn, row_id: int, title: str, reason: str, note: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO review_queue(source, order_number, title, reason, payload, status)
           VALUES ('catalog_repair', ?, ?, ?, ?, 'resolved')""",
        (str(row_id), title[:200], reason, note),
    )


def _prov_cid(part: str) -> str | None:
    """Which game's content id a provenance part belongs to (best effort)."""
    for cid in SPLIT_OVERRIDES:
        if cid in part:
            return cid
    m = re.match(r"^[a-z_]+:([^:]+)", part)
    if m and m.group(1) in _ORDER_TO_CID:
        return _ORDER_TO_CID[m.group(1)]
    return None


def _split_plan(cid: str) -> dict | None:
    """Split row fields for one content id: a verified override, else None.
    Without the PSN title dump no generic plan can be built, so unknown ids
    can't be resolved and the row is skipped as unrecognized."""
    if cid in SPLIT_OVERRIDES:
        return dict(SPLIT_OVERRIDES[cid])
    return None


def _prov_json(parts: list[str]) -> str | None:
    return "[" + ", ".join(json.dumps(p) for p in parts) + "]" if parts else None


def _split_jammed_row(conn, row, report: RepairReport) -> None:
    """Split one owned_games row whose psn_content_id carries two games."""
    cids = [c.strip() for c in (row["psn_content_id"] or "").split(",") if c.strip()]
    plans = [(cid, _split_plan(cid)) for cid in cids]
    resolvable = [(cid, ov) for cid, ov in plans if ov is not None]
    if len(resolvable) < 2:
        report.skipped.append(
            {"id": row["id"], "title": row["title"], "reason": "unrecognized merged content ids"}
        )
        return
    parts = provenance_parts(row["provenance"])
    first_cid, first_ov = resolvable[0]
    generic = first_ov.get("generic", False)
    keep_prov = [p for p in parts if _prov_cid(p) in (None, first_cid)]
    # Original row keeps the FIRST game (id stable).
    conn.execute(
        """UPDATE owned_games SET
             title = ?, normalized_title = ?, platform = ?, igdb_id = ?,
             psn_content_id = ?, acquisition_date = ?, price = ?,
             ownership_class = ?, provenance = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (
            first_ov["title"],
            normalize_title(first_ov["title"]),
            first_ov["platform"],
            first_ov["igdb_id"],
            first_cid,
            first_ov.get("acquisition_date"),
            first_ov.get("price"),
            first_ov.get("ownership_class", row["ownership_class"]),
            _prov_json(keep_prov),
            row["id"],
        ),
    )
    conn.execute("DELETE FROM igdb_matches WHERE owned_game_id = ?", (row["id"],))
    if first_ov["igdb_id"]:
        conn.execute(
            """INSERT OR IGNORE INTO igdb_matches(owned_game_id, igdb_id, confidence, matched_title)
               VALUES (?, ?, 'manual', ?)""",
            (row["id"], first_ov["igdb_id"], first_ov["title"]),
        )
    _audit(
        conn, row["id"], first_ov["title"],
        "split_jammed_content_ids",
        f"kept {first_cid}; split out {', '.join(c for c, _ in resolvable[1:])} (was {row['psn_content_id']})"
        + ("; generic plan — verify IGDB match" if generic else ""),
    )
    report.split.append(
        {"id": row["id"], "title": first_ov["title"], "kept_cid": first_cid, "split_cids": [c for c, _ in resolvable[1:]]}
    )

    # Insert a fresh row for each additional game.
    for cid, ov in resolvable[1:]:
        prov = [p for p in parts if _prov_cid(p) == cid]
        cur = conn.execute(
            """INSERT INTO owned_games
               (title, normalized_title, platform, format, ownership_class, retailer,
                order_number, item_id, condition, psn_content_id, igdb_id,
                acquisition_date, price, source, source_ref, status, is_owned, provenance)
               VALUES (?, ?, ?, 'digital', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'owned', 1, ?)""",
            (
                ov["title"],
                normalize_title(ov["title"]),
                ov["platform"],
                ov.get("ownership_class", row["ownership_class"]),
                row["retailer"],
                None,
                None,
                None,
                cid,
                ov["igdb_id"],
                ov.get("acquisition_date"),
                ov.get("price"),
                row["source"],
                cid,
                _prov_json(prov),
            ),
        )
        new_id = cur.lastrowid
        if ov["igdb_id"]:
            conn.execute(
                """INSERT OR IGNORE INTO igdb_matches(owned_game_id, igdb_id, confidence, matched_title)
                   VALUES (?, ?, 'manual', ?)""",
                (new_id, ov["igdb_id"], ov["title"]),
            )
        _audit(
            conn, new_id, ov["title"],
            "split_jammed_content_ids",
            f"split out of owned game {row['id']} (was merged under {row['psn_content_id']})"
            + ("; generic plan — verify IGDB match" if ov.get("generic") else ""),
        )
        report.split.append({"id": new_id, "title": ov["title"], "kept_cid": cid, "split_cids": []})


# --- wrong-IGDB-match jam: Valkyria Chronicles 4 + Remastered ---------------
# PSN receipts 411948960000 (Valkyria Chronicles 4, $5.99, 2023-05-24) and
# 507107929603 (Valkyria Chronicles Remastered, $4.99, 2024-03-06) were
# dedup-merged into ONE row because the VC4 receipt was wrongly matched to
# igdb 75848 (the Remastered entry). The row kept the VC4 API content id
# (UP0177-CUSA10633) but carries BOTH receipts. Split back into two rows;
# igdb is left NULL so the next igdb_matches pass matches each by title.
VALKYRIA_VC4_CID = "UP0177-CUSA10633_00-BFVALKYRIE000100"
VALKYRIA_RECEIPTS = {
    "411948960000": {"title": "Valkyria Chronicles 4", "platform": "playstation 4",
                     "acquisition_date": "05/24/2023", "price": "$5.99"},
    "507107929603": {"title": "Valkyria Chronicles Remastered", "platform": "playstation 4",
                     "acquisition_date": "03/06/2024", "price": "$4.99"},
}


def _split_valkyria_jam(conn, report: RepairReport) -> None:
    """Split the VC4 + VC Remastered row jammed by a wrong IGDB match."""
    row = conn.execute(
        "SELECT * FROM owned_games WHERE psn_content_id = ? AND is_owned = 1",
        (VALKYRIA_VC4_CID,),
    ).fetchone()
    if not row:
        return
    parts = provenance_parts(row["provenance"])
    if not (any("507107929603" in p for p in parts) and any("411948960000" in p for p in parts)):
        return  # already split (idempotent)
    vc4 = VALKYRIA_RECEIPTS["411948960000"]
    rem = VALKYRIA_RECEIPTS["507107929603"]
    keep = [p for p in parts if "507107929603" not in p]
    conn.execute(
        """UPDATE owned_games SET title = ?, normalized_title = ?, platform = ?,
           igdb_id = NULL, acquisition_date = ?, price = ?, provenance = ?,
           updated_at = datetime('now') WHERE id = ?""",
        (vc4["title"], normalize_title(vc4["title"]), vc4["platform"],
         vc4["acquisition_date"], vc4["price"], _prov_json(keep), row["id"]),
    )
    conn.execute("DELETE FROM igdb_matches WHERE owned_game_id = ?", (row["id"],))
    _audit(
        conn, row["id"], vc4["title"], "split_wrong_igdb_jam",
        "kept Valkyria Chronicles 4 (receipt 411948960000); split out Valkyria Chronicles Remastered "
        "(receipt 507107929603) — wrong IGDB match (75848) merged two games; igdb left for re-match",
    )
    report.split.append({"id": row["id"], "title": vc4["title"], "kept_cid": VALKYRIA_VC4_CID,
                         "split_cids": ["psn_receipt:507107929603"]})
    cur = conn.execute(
        """INSERT INTO owned_games
           (title, normalized_title, platform, format, ownership_class, retailer,
            order_number, item_id, condition, psn_content_id, igdb_id,
            acquisition_date, price, source, source_ref, status, is_owned, provenance)
           VALUES (?, ?, ?, 'digital', 'purchased', NULL, ?, NULL, NULL, NULL, NULL, ?, ?, 'psn_receipt', ?, 'owned', 1, ?)""",
        (rem["title"], normalize_title(rem["title"]), rem["platform"], "507107929603",
         rem["acquisition_date"], rem["price"], "507107929603:0",
         _prov_json(["psn_receipt:507107929603:0"])),
    )
    new_id = cur.lastrowid
    _audit(
        conn, new_id, rem["title"], "split_wrong_igdb_jam",
        f"split out of owned game {row['id']} (was merged under {row['psn_content_id']}); igdb left for re-match",
    )
    report.split.append({"id": new_id, "title": rem["title"], "kept_cid": "507107929603", "split_cids": []})


# --- game-key marketplace purchases merged as provenance --------------------
# gameflip/woot/shopify seller titles often lack a platform token, so the
# classifier buckets them 'platform ambiguous' and they never reach
# owned_games — but the games ARE owned (redeemed on PSN, visible in the
# library/API). Merge the key purchase into the matching owned row as
# provenance instead of dropping it. Skipped: add-on/DLC purchases and
# non-PlayStation hardware (Evercade carts). Idempotent per flag.
GAME_KEY_SOURCES = {"gameflip", "woot", "shopify"}
_REVIEW_SKIP_RE = (
    re.compile(r"special outfit|add-?on|evercade", re.IGNORECASE),
)
# Normalized-title overrides: seller title -> owned normalized_title.
REVIEW_TITLE_OVERRIDES = {
    "republique remastered": "republique",
    "plumbers don't wear ties": "plumbers don't wear ties: definitive edition",
    "ɪɴ𝐬ᴛᴀɴᴛ lara croft go": "lara croft go",  # seller '⭐INSTANT⭐' decoration (unicode small caps)
    "baby shark sing & swim party": "baby shark: sing & swim party",
}


def _clean_review_title(title: str) -> str:
    """Strip seller decoration (emoji/unicode) from a marketplace title."""
    cleaned = re.sub(r"[^\w\s'\-:&]", " ", title, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def _flag_payload(raw: str | None) -> dict:
    """Parse a review_queue payload — JSON or the Python-dict-string form the
    classify asset writes (str(dict(row)))."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    try:
        import ast

        value = ast.literal_eval(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, SyntaxError):
        return {}


def apply_review_merges(conn, report: RepairReport) -> None:
    """Merge open game-key purchase flags into the already-owned game."""
    for fl in conn.execute(
        "SELECT * FROM review_queue WHERE status = 'open' AND reason = 'platform ambiguous'"
    ).fetchall():
        if fl["source"] not in GAME_KEY_SOURCES:
            continue
        if any(p.search(fl["title"] or "") for p in _REVIEW_SKIP_RE):
            continue
        payload = _flag_payload(fl["payload"])
        item_key = payload.get("item_key") or fl["order_number"]
        if not item_key:
            continue
        norm = normalize_title(_clean_review_title(fl["title"] or ""))
        norm = REVIEW_TITLE_OVERRIDES.get(norm, norm)
        target = conn.execute(
            "SELECT * FROM owned_games WHERE is_owned = 1 AND normalized_title = ? LIMIT 1",
            (norm,),
        ).fetchone()
        if not target:
            continue
        prov_ref = f"{fl['source']}:{item_key}"
        if any(prov_ref == p for p in provenance_parts(target["provenance"])):
            conn.execute("UPDATE review_queue SET status = 'resolved' WHERE id = ?", (fl["id"],))
            continue
        conn.execute(
            """UPDATE owned_games SET provenance = ?, price = COALESCE(?, price),
               updated_at = datetime('now') WHERE id = ?""",
            (merge_provenance(target["provenance"], prov_ref), payload.get("price"), target["id"]),
        )
        payload["decision"] = "merged_as_provenance"
        conn.execute(
            "UPDATE review_queue SET status = 'resolved', payload = ? WHERE id = ?",
            (json.dumps(payload), fl["id"]),
        )
        _audit(
            conn, target["id"], target["title"], "merge_key_purchase_provenance",
            f"merged {fl['source']} purchase '{fl['title']}' ({item_key}) as provenance",
        )
        report.merged.append({"id": target["id"], "title": target["title"], "source": fl["source"]})


# --- title noise + alternative-name merges ----------------------------------
# ' and N more items' is a marketplace listing summary a parser swallowed into
# an item title (e.g. "Diablo IV - PlayStation 5 and 1 more item").
_MORE_ITEMS_RE = re.compile(r"(?:\s*,?\s*and\s+\d+\s+more\s+items?)\s*$", re.IGNORECASE)
# PlayStation Vita console models (hardware, not games) that bypassed the
# platform gate under a console-model title (e.g. "PlayStation Vita Wi-Fi…").
_CONSOLE_HARDWARE_RE = re.compile(
    r"^(?:sony\s+)?playstation\s+vita\s+(?:wi-?fi(?:\s*\+?\s*3g)?|slim|pch-\w+|fat)\b",
    re.IGNORECASE,
)


def _clean_catalog_title(title: str) -> str:
    """Strip catalog-only noise from a stored title: trailing ' and N more
    items' (a listing summary swallowed by a parser), '(Game)' catalog markers,
    and trailing platform markers (' - PlayStation 5', '(PS4 & PS5)',
    'PS4 & PS5'). The platform suffix is display-only — the real platform
    lives in the `platform` column — so dropping it from the title is safe
    and matches how the matcher already normalizes.
    """
    t = title.strip()
    t = re.sub(_MORE_ITEMS_RE, "", t)
    t = re.sub(r"\s*\(\s*(?:downloadable\s+)?(?:full\s+)?game\s*\)\s*$", "", t, flags=re.IGNORECASE)
    # size-note parenthetical: '(Full Game 128 MB)' / '(Full Game 979 MB)'
    t = re.sub(
        r"\s*\(\s*(?:downloadable\s+)?(?:full\s+)?game\s+[\d.,]+\s*(?:kb|mb|gb)\s*\)\s*$",
        "", t, flags=re.IGNORECASE,
    )
    # retailer-exclusive marker ('Secret of Mana - PlayStation 4 GameStop
    # Exclusive') — display-only noise, the game title is just 'Secret of Mana'.
    t = re.sub(
        r"\s*[-–—]?\s*(?:gamestop|best buy|amazon|target|walmart|eb games|playstation store)\s+exclusive\s*$",
        "", t, flags=re.IGNORECASE,
    )
    # One PS platform token: 'playstation 5', 'ps4', 'playstation 4 & playstation 5'.
    # (A lone 'ps?'+digit covers the bare 'ps4'/'p5' forms; the full word is its
    # own alternative so 'PlayStation 5' is not mis-parsed as 'ps' + '5'.)
    ps_token = r"(?:playstation\s*[45]|ps?\s*[45])"
    multi = rf"{ps_token}(?:\s*[&/+]\s*{ps_token})?"
    # trailing platform parenthetical: (PS4 & PS5) / (PS5) / (PlayStation 4)
    t = re.sub(rf"\s*\(\s*{multi}\s*\)\s*$", "", t, flags=re.IGNORECASE)
    # trailing ' - PlayStation 5' / ' - PS4 & PS5' / ' - PS4'
    t = re.sub(rf"\s*[-–—]\s*{multi}\s*$", "", t, flags=re.IGNORECASE)
    # trailing bare 'PS4 & PS5' / 'PlayStation 4'
    t = re.sub(rf"\s+{multi}\s*$", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip(" -–—:;") if t else t


def _apply_title_cleanup(conn, report: RepairReport) -> None:
    """Clean catalog-only noise from stored titles (memos/game-catalog-titles):
    strip ' and N more items' (a listing summary a parser swallowed), '(Game)'
    markers, and trailing platform suffixes; retire Vita-console hardware rows
    the platform gate let through under a console-model title."""
    for r in conn.execute("SELECT * FROM owned_games WHERE is_owned = 1").fetchall():
        title = r["title"] or ""
        cleaned = _clean_catalog_title(title)
        if cleaned == title:
            continue
        if _CONSOLE_HARDWARE_RE.search(cleaned):
            _retire(conn, r, "not_a_game:hardware_console")
            _audit(conn, r["id"], title, "retire_hardware_console",
                   f"'{cleaned}' is a console (hardware), not a game")
            report.retired.append({"id": r["id"], "title": cleaned, "reason": "hardware_console"})
            continue
        if cleaned:
            conn.execute(
                """UPDATE owned_games SET title = ?, normalized_title = ?,
                   updated_at = datetime('now') WHERE id = ?""",
                (cleaned, normalize_title(cleaned), r["id"]),
            )
            report.cleaned.append({"id": r["id"], "title": cleaned})


# Alternative-name titles to merge into their already-owned canonical entry
# (IGDB alternative names / PSN concatenated spellings). Keyed on the row's
# normalized_title; the canonical row (if owned) supplies the IGDB id so the
# dedup pass collapses the pair into one entry.
TITLE_ALIASES = {
    # IGDB lists 'Another Fisherman's Tale' as an alternative name for the
    # sequel — both are the same game, so they merge into one entry.
    "another fisherman's tale": {
        "title": "A Fisherman's Tale 2",
        "normalized_title": "a fisherman's tale 2",
    },
    # 'CoffeeTalk' is PSN's concatenated rendering of 'Coffee Talk' — merge
    # the space-less duplicate (which carried no cover) into the real entry.
    "coffeetalk": {
        "title": "Coffee Talk",
        "normalized_title": "coffee talk",
    },
}


def _apply_title_aliases(conn, report: RepairReport) -> None:
    """Merge rows whose title is just an alternative name for an already-owned
    game into the canonical entry: adopt the canonical title + normalized_title
    and (when the canonical row has an IGDB match) its igdb_id, then the dedup
    pass at the end of apply_catalog_repairs collapses the pair into one row."""
    from mailroom.verticals.game_catalog.dedup import (
        merge_group,  # lazy: dedup imports db
    )

    for alias_norm, canon in TITLE_ALIASES.items():
        alias_rows = conn.execute(
            "SELECT * FROM owned_games WHERE is_owned = 1 AND normalized_title = ?",
            (alias_norm,),
        ).fetchall()
        if not alias_rows:
            continue
        canon_row = conn.execute(
            """SELECT * FROM owned_games WHERE is_owned = 1
               AND normalized_title = ? ORDER BY igdb_id IS NOT NULL DESC, id LIMIT 1""",
            (canon["normalized_title"],),
        ).fetchone()
        for r in alias_rows:
            new_igdb = r["igdb_id"]
            if canon_row and canon_row["igdb_id"]:
                new_igdb = canon_row["igdb_id"]
            try:
                conn.execute(
                    """UPDATE owned_games SET title = ?, normalized_title = ?,
                       igdb_id = ?, updated_at = datetime('now') WHERE id = ?""",
                    (canon["title"], canon["normalized_title"], new_igdb, r["id"]),
                )
            except sqlite3.IntegrityError:
                # dedup guard: another OWNED row already has (igdb_id, platform,
                # format) — collapse into it instead of leaving a duplicate.
                other = conn.execute(
                    """SELECT * FROM owned_games
                       WHERE is_owned = 1 AND igdb_id = ? AND platform = ? AND format = ? AND id != ?""",
                    (new_igdb, r["platform"], r["format"], r["id"]),
                ).fetchone()
                if other:
                    merge_group(conn, [r, other])
                else:
                    raise
            _audit(
                conn, r["id"], canon["title"], "merge_title_alias",
                f"'{r['title']}' is an alternative name for '{canon['title']}' — merged into the canonical entry",
            )
            report.merged.append({"id": r["id"], "title": canon["title"], "source": "title_alias"})


def _apply_crossgen_platform(conn, report: RepairReport) -> None:
    """Pin rows whose title advertises BOTH PS4 and PS5 to PlayStation 5.

    A cross-gen title ("One Hand Clapping PS4 & PS5") is a release that ships
    on PS5; the classifier used to fall back to the generic 'playstation' for
    these, so already-ingested rows are stuck generic. Mirrors the classifier
    rule (both tokens -> PS5); idempotent.
    """
    rows = conn.execute(
        """SELECT * FROM owned_games WHERE is_owned = 1
           AND (platform IN ('playstation', 'ps') OR platform IS NULL)"""
    ).fetchall()
    for r in rows:
        text = (r["title"] or "").lower()
        if re.search(r"\bps4\b", text) and re.search(r"\bps5\b", text):
            conn.execute(
                """UPDATE owned_games SET platform = 'playstation 5', updated_at = datetime('now')
                   WHERE id = ?""",
                (r["id"],),
            )
            _audit(
                conn, r["id"], r["title"], "crossgen_platform_ps5",
                "title advertises PS4 & PS5 — pinned to PlayStation 5",
            )
            report.cleaned.append({"id": r["id"], "title": r["title"], "reason": "crossgen_platform_ps5"})


def apply_catalog_repairs(conn) -> RepairReport:
    """Idempotent repair pass over owned_games. Safe to re-run."""
    report = RepairReport()

    # 0a) cross-gen platform FIRST (before title cleanup strips the 'PS4 & PS5'
    #     suffix): a title advertising BOTH PS4 & PS5 is pinned to PS5 (the
    #     classifier now does this at parse time; this heals old rows).
    _apply_crossgen_platform(conn, report)

    # 0) title noise: strip '(Game)' / platform suffixes / ' and N more items'
    #    from stored titles and retire Vita-console hardware rows.
    _apply_title_cleanup(conn, report)

    # 1) cancelled-order junk (Amazon subject fallback swallowed the tail).
    for r in conn.execute(
        "SELECT * FROM owned_games WHERE is_owned = 1 AND igdb_id IS NULL"
    ).fetchall():
        if _CANCELLED_TITLE_RE.search(r["title"]):
            _retire(conn, r, "not_a_game:cancelled_order")
            _audit(conn, r["id"], r["title"], "retire_cancelled_order", "Amazon cancellation subject parsed as an item")
            report.retired.append({"id": r["id"], "title": r["title"], "reason": "cancelled_order"})

    # 2) non-game add-ons by content id (Dreams OST / art book).
    for r in conn.execute(
        "SELECT * FROM owned_games WHERE is_owned = 1 AND psn_content_id IS NOT NULL"
    ).fetchall():
        cid = (r["psn_content_id"] or "").upper()
        for marker, reason in NON_GAME_CONTENT_MARKERS:
            if marker in cid:
                _retire(conn, r, f"not_a_game:{reason}")
                _audit(conn, r["id"], r["title"], f"retire_{reason}", f"content id {r['psn_content_id']} is an add-on, not a game")
                report.retired.append({"id": r["id"], "title": r["title"], "reason": reason})
                break

    # 3) jammed content ids (two games merged into one row).
    for r in conn.execute(
        "SELECT * FROM owned_games WHERE is_owned = 1 AND psn_content_id LIKE '%,%'"
    ).fetchall():
        _split_jammed_row(conn, r, report)

    # 4) ambiguous short titles pinned by content id.
    for r in conn.execute(
        "SELECT * FROM owned_games WHERE is_owned = 1 AND psn_content_id IS NOT NULL"
    ).fetchall():
        cid = (r["psn_content_id"] or "").strip()
        if cid in MATCH_OVERRIDES and r["igdb_id"] != MATCH_OVERRIDES[cid]:
            gid = MATCH_OVERRIDES[cid]
            conn.execute(
                """INSERT OR IGNORE INTO igdb_matches(owned_game_id, igdb_id, confidence, matched_title)
                   VALUES (?, ?, 'manual', ?)""",
                (r["id"], gid, r["title"]),
            )
            try:
                conn.execute("UPDATE owned_games SET igdb_id = ?, updated_at = datetime('now') WHERE id = ?", (gid, r["id"]))
            except sqlite3.IntegrityError:
                # Dedup guard: another OWNED row already has (igdb_id, platform,
                # format) — collapse into it instead of leaving a duplicate.
                from mailroom.verticals.game_catalog.dedup import merge_group

                other = conn.execute(
                    """SELECT * FROM owned_games
                       WHERE is_owned = 1 AND igdb_id = ? AND platform = ? AND format = ? AND id != ?""",
                    (gid, r["platform"], r["format"], r["id"]),
                ).fetchone()
                if other:
                    merge_group(conn, [r, other])
                else:
                    raise
            _audit(
                conn, r["id"], r["title"], "rematch_by_content_id",
                f"pinned IGDB {gid} (was {r['igdb_id']}) — ambiguous short title",
            )
            report.rematched.append({"id": r["id"], "title": r["title"], "igdb_id": gid})

    # 4b) receipt rows without a content id pinned by cleaned title (e.g.
    #     'Batman™: Arkham Knight (Game)' -> base game, not a skin DLC).
    for r in conn.execute(
        "SELECT * FROM owned_games WHERE is_owned = 1"
    ).fetchall():
        gid = TITLE_MATCH_OVERRIDES.get(_title_match_key(r["normalized_title"]))
        if not gid or r["igdb_id"] == gid:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO igdb_matches(owned_game_id, igdb_id, confidence, matched_title)
               VALUES (?, ?, 'manual', ?)""",
            (r["id"], gid, r["title"]),
        )
        try:
            conn.execute("UPDATE owned_games SET igdb_id = ?, updated_at = datetime('now') WHERE id = ?", (gid, r["id"]))
        except sqlite3.IntegrityError:
            from mailroom.verticals.game_catalog.dedup import merge_group

            other = conn.execute(
                """SELECT * FROM owned_games
                   WHERE is_owned = 1 AND igdb_id = ? AND platform = ? AND format = ? AND id != ?""",
                (gid, r["platform"], r["format"], r["id"]),
            ).fetchone()
            if other:
                merge_group(conn, [r, other])
            else:
                raise
        _audit(
            conn, r["id"], r["title"], "rematch_by_title",
            f"pinned IGDB {gid} (was {r['igdb_id']}) — IGDB search surfaced a DLC/skin instead of the base game",
        )
        report.rematched.append({"id": r["id"], "title": r["title"], "igdb_id": gid})

    # 4c) collection bundles split into their member games (Batman: Arkham
    #     Collection, BioShock: The Collection, Shadowrun Trilogy, Hotline
    #     Miami Collection).
    for coll_igdb, members in COLLECTION_SPLITS.items():
        rows = conn.execute(
            "SELECT * FROM owned_games WHERE is_owned = 1 AND igdb_id = ?", (coll_igdb,)
        ).fetchall()
        # title-substring fallback: match rows whose igdb_id is NULL / differs
        # from the key (a collection row may be unmatched in a given DB).
        for frag in COLLECTION_TITLES.get(coll_igdb, []):
            like = f"%{frag.lower()}%"
            rows += conn.execute(
                """SELECT * FROM owned_games WHERE is_owned = 1
                   AND (igdb_id IS NULL OR igdb_id != ?) AND LOWER(title) LIKE ?""",
                (coll_igdb, like),
            ).fetchall()
            # canonical-key fallback: match even when raw title characters
            # differ (non-breaking space, dash, ™ etc.) by comparing the
            # normalized grouping key (e.g. 'Batman: Arkham Collection (Full
            # Game and Add-On Content)' -> 'batman: arkham collection').
            canon = canonical_title(frag)
            for r in conn.execute(
                "SELECT * FROM owned_games WHERE is_owned = 1 AND igdb_id IS NULL"
            ).fetchall():
                if canonical_title(r["normalized_title"]) == canon:
                    rows.append(r)
        seen: set[int] = set()
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            _split_collection(conn, r, members, report)

    # 5) wrong-IGDB-match jam (Valkyria Chronicles 4 + Remastered in one row).
    _split_valkyria_jam(conn, report)

    # 6) game-key marketplace purchases (gameflip/woot/shopify) merged into
    # the already-owned game as provenance.
    apply_review_merges(conn, report)

    # 7) alternative-name titles merged into their canonical entry
    #    ('Another Fisherman's Tale' -> 'A Fisherman's Tale 2', 'CoffeeTalk'
    #    -> 'Coffee Talk') so the dedup pass below collapses the pair.
    _apply_title_aliases(conn, report)

    conn.commit()
    # Collapse duplicates created by the rematch (e.g. the pinned 'Unplugged'
    # API row + its receipt row now share igdb_id 153854) so the repair is
    # atomic — no waiting for the next scheduled dedup pass.
    from mailroom.verticals.game_catalog.dedup import dedupe_owned_games

    dedupe_owned_games(conn)
    return report
