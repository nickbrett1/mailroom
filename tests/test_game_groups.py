"""Canonical-game grouping tests (memos/catalog-games-model): multiple editions
/ purchases of one game collapse into a single game row with aggregated
editions; catalog_games asset reparents owned_games under games."""

from __future__ import annotations

import json
import tempfile

from dagster import build_op_context

from mailroom.db import connect, init_db
from mailroom.verticals.game_catalog import assets
from mailroom.verticals.game_catalog.game_groups import (
    build_games,
    canonical_title,
    sortable_date,
)
from mailroom.verticals.game_catalog.parsers.psn import normalize_title


def _seed(conn, **kw) -> int:
    title = kw.get("title", "Some Game")
    cur = conn.execute(
        """INSERT INTO owned_games
           (title, normalized_title, platform, format, ownership_class, retailer,
            order_number, igdb_id, acquisition_date, price, source, status, is_owned,
            provenance, psn_content_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'owned', 1, ?, ?)""",
        (
            title,
            kw.get("normalized_title") or normalize_title(title),
            kw.get("platform", "playstation 4"),
            kw.get("format", "digital"),
            kw.get("ownership_class", "purchased"),
            kw.get("retailer"),
            kw.get("order_number"),
            kw.get("igdb_id"),
            kw.get("acquisition_date"),
            kw.get("price"),
            kw.get("source", "psn_api"),
            kw.get("provenance") or f"psn_api:{title}",
            kw.get("psn_content_id"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def test_edition_groups_map_to_base():
    assert canonical_title("alien isolation - the collection") == "alien isolation"
    assert canonical_title("arcade paradise vr") == "arcade paradise"
    assert canonical_title("alien isolation") == "alien isolation"


def test_never_alone_arctic_collection_folds_onto_never_alone():
    """'Never Alone Arctic Collection (Full Game and Add-On Content)' is the
    same game as 'Never Alone' (base + Foxtales DLC) — different IGDB entries
    and titles — so they collapse into ONE card with 2 editions."""
    assert canonical_title("never alone arctic collection") == "never alone"
    assert canonical_title("never alone arctic collection (full game and add-on content)") == "never alone"
    rows = [
        {"id": 1, "title": "Never Alone", "normalized_title": "never alone",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 273575, "price": None, "acquisition_date": None,
         "provenance": "psn_api:UP2159-CUSA01305_00-B000000000001769"},
        {"id": 2, "title": "Never Alone Arctic Collection (Full Game and Add-On Content)",
         "normalized_title": "never alone arctic collection (full game and add-on content)",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 46702, "price": "$3.59", "acquisition_date": "04/08/2023",
         "provenance": "psn_receipt:414348861650:0"},
    ]
    games, report = build_games(rows)
    assert len(games) == 1
    g = games[0]
    assert g["normalized_title"] == "never alone"
    assert g["title"] == "Never Alone"  # base edition wins the display title
    assert g["igdb_id"] == 273575
    assert g["num_editions"] == 2
    editions = json.loads(g["editions"])
    assert {e["title"] for e in editions} == {"Never Alone", "Never Alone Arctic Collection (Full Game and Add-On Content)"}
    assert report.games == 1


def test_build_games_consolidates_editions():
    """Two editions of Alien Isolation (base + 'The Collection') — different
    IGDB entries and titles — collapse into ONE game with 2 editions."""
    rows = [
        {"id": 1, "title": "Alien Isolation", "normalized_title": "alien isolation",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 1001, "price": "$19.99", "acquisition_date": "2020-01-01",
         "provenance": "psn_api:cid1"},
        {"id": 2, "title": "Alien Isolation - The Collection", "normalized_title": "alien isolation - the collection",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 1002, "price": "$29.99", "acquisition_date": "2021-02-02",
         "provenance": "psn_api:cid2"},
    ]
    games, report = build_games(rows)
    assert len(games) == 1
    g = games[0]
    assert g["normalized_title"] == "alien isolation"
    assert g["title"] == "Alien Isolation"  # base edition wins the display title
    assert g["igdb_id"] == 1001  # base edition's igdb
    assert g["num_editions"] == 2
    assert g["purchased"] == 1
    editions = json.loads(g["editions"])
    assert len(editions) == 2
    assert {e["title"] for e in editions} == {"Alien Isolation", "Alien Isolation - The Collection"}
    assert report.games == 1


def test_build_games_claimed_and_purchased_same_game():
    """Slay the Spire bought + PS+ claimed is one game, flagged purchased."""
    rows = [
        {"id": 1, "title": "Slay the Spire", "normalized_title": "slay the spire",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 106850, "provenance": "psn_receipt:o1:0"},
        {"id": 2, "title": "Slay the Spire", "normalized_title": "slay the spire",
         "platform": "playstation 5", "format": "digital", "ownership_class": "psplus_claimed",
         "igdb_id": 106850, "provenance": "psn_api:cid-slay"},
    ]
    games, _ = build_games(rows)
    assert len(games) == 1
    g = games[0]
    assert g["purchased"] == 1  # any edition bought
    assert g["platforms"] == "playstation 4, playstation 5"
    assert g["ownership_classes"] == "psplus_claimed, purchased"
    assert g["num_editions"] == 2


def test_build_games_psvr2_flag_from_metadata():
    metadata = {200: {"id": 200, "platforms": [{"id": 167}, {"id": 390}]}}
    rows = [
        {"id": 1, "title": "Arcade Paradise VR", "normalized_title": "arcade paradise vr",
         "platform": "playstation 5", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 200, "provenance": "psn_api:cid-ap-vr"},
    ]
    games, _ = build_games(rows, metadata=metadata)
    assert games[0]["is_psvr2"] == 1
    assert games[0]["normalized_title"] == "arcade paradise"  # VR is an edition of the base


def test_build_groups_same_igdb_different_titles_and_formats():
    """Two editions of Divinity: OS2 (PS5 digital + PS physical) share the same
    igdb_id (103337) but different titles/formats/platforms — they must fold
    into ONE card with 2 editions."""
    rows = [
        {"id": 1, "title": "Divinity: Original Sin 2 - Definitive Edition",
         "normalized_title": "divinity: original sin 2 - definitive edition",
         "platform": "playstation 5", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 103337, "provenance": "psn_api:cid-div2", "acquisition_date": "2025-12-15"},
        {"id": 2, "title": "Divinity: Original Sin II - Definitive Edition",
         "normalized_title": "divinity: original sin ii - definitive edition",
         "platform": "playstation", "format": "physical", "ownership_class": "purchased",
         "igdb_id": 103337, "provenance": "gamestop:1100000042691133", "acquisition_date": "2021-12-27"},
    ]
    games, _ = build_games(rows)
    assert len(games) == 1
    g = games[0]
    assert g["igdb_id"] == 103337
    assert g["num_editions"] == 2
    assert g["formats"] == "digital, physical"
    assert g["earliest_acquisition"] == "2021-12-27"


def test_build_groups_edition_suffix_and_roman_numeral():
    """Deus Ex base + 'Digital Deluxe Edition' collapse via suffix stripping,
    and a roman-numeral variant maps onto its arabic counterpart."""
    # suffix stripping: same base, different edition
    assert canonical_title("deus ex: mankind divided - digital deluxe edition") == "deus ex: mankind divided"
    # roman -> arabic
    assert canonical_title("divinity: original sin ii - definitive edition") == "divinity: original sin 2"
    # episodic entry-point listing folds onto the full season
    assert canonical_title("batman: the enemy within - episode 1") == "batman: the enemy within"
    assert canonical_title("life is strange 2 - episode 1") == "life is strange 2"
    # reloaded edition folds onto the base
    assert canonical_title("mercenary kings: reloaded edition") == "mercenary kings"
    # same card for different-igdb editions of the same title
    rows = [
        {"id": 1, "title": "Cities: VR", "normalized_title": "cities: vr",
         "platform": "playstation 5", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 185895, "provenance": "psn_api:cvr"},
        {"id": 2, "title": "Cities: VR - Enhanced Edition", "normalized_title": "cities: vr - enhanced edition",
         "platform": "playstation", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 225958, "provenance": "psn_api:cvr-ee"},
    ]
    games, _ = build_games(rows)
    assert len(games) == 1
    assert games[0]["num_editions"] == 2


def test_last_of_us_part2_remastered_collapses_with_part2():
    """Part II Remastered is the same game as Part II — both rows collapse to
    one card (the PS5 remaster is just the enhanced port)."""
    assert canonical_title("the last of us part ii remastered") == "the last of us part 2"
    assert canonical_title("the last of us part ii") == "the last of us part 2"
    rows = [
        {"id": 1, "title": "The Last of Us™ Part II", "normalized_title": "the last of us part ii",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 202503, "provenance": "psn_api:part2"},
        {"id": 2, "title": "The Last of Us™ Part II Remastered", "normalized_title": "the last of us part ii remastered",
         "platform": "playstation 5", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 26192, "provenance": "psn_api:part2remastered"},
    ]
    games, _ = build_games(rows)
    assert len(games) == 1
    assert games[0]["num_editions"] == 2
    assert games[0]["platform"] == "playstation 4, playstation 5"


def test_earliest_acquisition_normalized_for_sorting():
    """Mixed-format purchase dates ('04/10/2026', '1/19/2023',
    'November 23, 2023', ISO) normalize to YYYY-MM-DD so a game's earliest
    purchase date sorts correctly (2026-04-10 must come AFTER 2023 dates)."""
    assert sortable_date("04/10/2026") == "2026-04-10"
    assert sortable_date("1/19/2023") == "2023-01-19"
    assert sortable_date("November 23, 2023") == "2023-11-23"
    assert sortable_date("2024-04-12T23:43:36Z") == "2024-04-12"
    assert sortable_date("2021-11-21T04:08:56Z") == "2021-11-21"
    assert sortable_date(None) is None
    rows = [
        {"id": 1, "title": "Blasphemous", "normalized_title": "blasphemous",
         "platform": "playstation", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 1, "provenance": "r", "acquisition_date": "04/10/2026"},
        {"id": 2, "title": "Limbo", "normalized_title": "limbo",
         "platform": "playstation", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 2, "provenance": "r", "acquisition_date": "1/19/2023"},
    ]
    games, _ = build_games(rows)
    earliest = {g["title"]: g["earliest_acquisition"] for g in games}
    assert earliest["Blasphemous"] == "2026-04-10"
    assert earliest["Limbo"] == "2023-01-19"
    assert earliest["Blasphemous"] > earliest["Limbo"]


def test_build_groups_more_edition_variants():
    """Edition aliases the suffix/marker normalizer folds: Shadow/Complete/
    Console/Royal/Founders/Championship/Maestro Beard/New Dimension editions,
    parenthetical platform+size notes, and curated aliases (Dragon's Crown Pro,
    Guacamelee! 2 Complete, Kayak VR + Soča Valley)."""
    rows = [
        {"id": 1, "title": "Aragami", "normalized_title": "aragami", "platform": "playstation 4",
         "format": "digital", "ownership_class": "purchased", "igdb_id": 18853, "provenance": "psn_api:a"},
        {"id": 2, "title": "Aragami: Shadow Edition (Full Game and Add-On Content)",
         "normalized_title": "aragami: shadow edition (full game and add-on content)",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 100437, "provenance": "psn_api:b"},
        {"id": 3, "title": "Burly Men at Sea Maestro Beard Edition",
         "normalized_title": "burly men at sea maestro beard edition", "platform": "playstation 4",
         "format": "digital", "ownership_class": "purchased", "igdb_id": 52672, "provenance": "psn_api:c"},
        {"id": 4, "title": "Burly Men At Sea", "normalized_title": "burly men at sea",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 24501, "provenance": "psn_api:d"},
        {"id": 5, "title": "Children of Morta: Complete Edition",
         "normalized_title": "children of morta: complete edition", "platform": "playstation 4",
         "format": "digital", "ownership_class": "purchased", "igdb_id": 175878, "provenance": "psn_api:e"},
        {"id": 6, "title": "Children of Morta", "normalized_title": "children of morta",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 36198, "provenance": "psn_api:f"},
        {"id": 7, "title": "Dragon's Crown Pro", "normalized_title": "dragon's crown pro",
         "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 68283, "provenance": "psn_api:g"},
        {"id": 8, "title": "Dragon's Crown™ (Full Game 979 MB)",
         "normalized_title": "dragon's crown™ (full game 979 mb)", "platform": "playstation",
         "format": "digital", "ownership_class": "purchased", "igdb_id": 3002, "provenance": "psn_api:h"},
        {"id": 9, "title": "Kayak VR: Mirage + Soča Valley",
         "normalized_title": "kayak vr: mirage + soča valley", "platform": "playstation",
         "format": "digital", "ownership_class": "purchased", "igdb_id": 305363, "provenance": "psn_api:i"},
        {"id": 10, "title": "Kayak VR: Mirage", "normalized_title": "kayak vr: mirage",
         "platform": "playstation 5", "format": "digital", "ownership_class": "purchased",
         "igdb_id": 157244, "provenance": "psn_api:j"},
    ]
    games, _ = build_games(rows)
    by_norm = {g["normalized_title"]: g for g in games}
    # Aragami (shadow edition) -> 1 card, 2 editions
    assert by_norm["aragami"]["num_editions"] == 2
    # Burly Men At Sea -> 1 card, 2 editions
    assert by_norm["burly men at sea"]["num_editions"] == 2
    # Children of Morta -> 1 card, 2 editions
    assert by_norm["children of morta"]["num_editions"] == 2
    # Dragon's Crown (Pro + original) -> 1 card, 2 editions
    assert by_norm["dragon's crown"]["num_editions"] == 2
    # Kayak VR: Mirage (+ Soča Valley) -> 1 card, 2 editions
    assert by_norm["kayak vr: mirage"]["num_editions"] == 2


def test_build_groups_more_edition_variants_2():
    """More edition/collection folds: Rock Band 4 Rivals, Civ VI Platinum,
    Spiritfarer Farewell, Subnautica/Prince of Persia colon variants,
    Naheulbeuk Chicken, Swords of Ditto, Saints & Sinners (Standard/Tourist),
    Two Point JUMBO, Valkyria Remastered, Virginia Special Edition Bundle."""
    pairs = [
        ("rock band 4", "rock band 4 rivals bundle"),
        ("sid meier's civilization vi", "sid meier's civilization vi platinum edition"),
        ("spiritfarer", "spiritfarer: farewell edition"),
        ("subnautica below zero", "subnautica: below zero"),
        ("prince of persia the lost crown", "prince of persia: the lost crown"),
        ("the dungeon of naheulbeuk: the amulet of chaos",
         "the dungeon of naheulbeuk: the amulet of chaos - chicken edition"),
        ("the swords of ditto", "the swords of ditto: mormo's curse"),
        ("the walking dead: saints & sinners",
         "the walking dead: saints & sinners - standard edition"),
        ("the walking dead: saints & sinners",
         "the walking dead: saints & sinners tourist edition"),
        ("two point hospital", "two point hospital: jumbo edition"),
        ("valkyria chronicles", "valkyria chronicles remastered"),
        ("virginia", "virginia - special edition bundle"),
    ]
    for a, b in pairs:
        assert canonical_title(a) == canonical_title(b), (a, b)
    # distinct titles must NOT fold together
    assert canonical_title("metroid prime") != canonical_title("metroid prime remastered")
    assert canonical_title("rayman legends") != canonical_title("rayman origins")
    # Synth Riders + 80s Mixtape DLC bundle folds onto the base game
    assert canonical_title("synth riders + 80s mixtape - side a") == "synth riders"
    assert canonical_title("synth riders + 80s mixtape side b") == "synth riders"
    # Agents of Mayhem - Total Mayhem Bundle folds onto the base
    assert canonical_title("agents of mayhem - total mayhem bundle (full game and add-on content)") == "agents of mayhem"
    # Cuphead + Delicious Last Course / Steredenn Binary Stars / Geometry Wars / TWD / Teslagrad
    assert canonical_title("cuphead & the delicious last course") == "cuphead"
    assert canonical_title("steredenn: binary stars") == "steredenn"
    assert canonical_title("geometry wars 3: dimensions") == "geometry wars 3: dimensions evolved"
    assert canonical_title("the walking dead: the complete first season") == "the walking dead: season 1"
    assert canonical_title("teslagrad") == "teslagrad remastered"


def test_init_db_idempotent_with_game_id():
    """Re-running init_db (as concurrent assets do) must not fail on the
    ALTER ADD COLUMN game_id migration (duplicate-column race, seen live)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    init_db(conn)  # second run = the concurrent/duplicate path
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(owned_games)").fetchall()}
    assert "game_id" in cols
    assert conn.execute("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name='games'").fetchone()["n"] == 1
    conn.close()


def test_catalog_games_asset_builds_and_reparents():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed(conn, title="Arcade Paradise", platform="playstation 5", igdb_id=100,
          provenance="psn_api:cid-ap")
    _seed(conn, title="Arcade Paradise VR", platform="playstation 5", igdb_id=101,
          provenance="psn_api:cid-ap-vr")
    conn.close()

    assets.catalog_games(build_op_context(resources={"db_url": f"sqlite:///{db}"}))
    assets.catalog_views(build_op_context(resources={"db_url": f"sqlite:///{db}"}))

    conn = connect(f"sqlite:///{db}")
    rows = conn.execute("SELECT * FROM catalog_games").fetchall()
    assert len(rows) == 1
    g = rows[0]
    assert g["title"] == "Arcade Paradise"
    assert g["num_editions"] == 2
    # both owned rows reparented under the one game
    assert conn.execute(
        "SELECT COUNT(*) n FROM owned_games WHERE is_owned = 1 AND game_id = ?",
        (g["game_id"],),
    ).fetchone()["n"] == 2
    conn.close()


def test_platform_and_psvr2_overrides():
    """Curated PLATFORM_OVERRIDES / PSVR2_OVERRIDES pin the display platform for
    cross-gen (PS4+PS5) and PS Vita games that auto-backfill can't pin, and set
    the is_psvr2 flag; non-overridden games are untouched."""
    from mailroom.verticals.game_catalog.game_groups import (
        PLATFORM_OVERRIDES,
        PSVR2_OVERRIDES,
    )

    def _r(rid, title, platform, igdb_id):
        return {
            "id": rid, "title": title, "normalized_title": normalize_title(title),
            "platform": platform, "format": "digital", "ownership_class": "purchased",
            "retailer": None, "order_number": None, "item_id": None, "condition": None,
            "psn_content_id": None, "igdb_id": igdb_id, "acquisition_date": None,
            "price": None, "source": "psn_api", "source_ref": None, "status": "owned",
            "is_owned": 1, "provenance": None,
        }

    # cross-gen / vita / psvr2 games currently stuck on generic 'playstation'.
    # Use full catalog titles to prove the igdb_id key is title-independent.
    rows = [
        _r(1, "Far Cry 6", "playstation", 126290),
        _r(2, "Persona 4 Golden", "playstation", 2985),
        _r(3, "Resident Evil Village", "playstation", 55163),
        _r(4, "Atari 50: The Anniversary Celebration", "playstation", 207018),
        _r(5, "Kingdom Hearts II", "playstation", 1221),
        _r(6, "Synth Riders", "playstation", 105333),
        # control — not in the override map
        _r(7, "Uncharted 4", "playstation", 14731),
    ]
    games, _ = build_games(rows)
    by_title = {g["title"]: g for g in games}
    assert by_title["Far Cry 6"]["platform"] == "playstation 5"
    assert by_title["Persona 4 Golden"]["platform"] == "playstation vita"
    assert by_title["Resident Evil Village"]["platform"] == "playstation 5"
    assert by_title["Resident Evil Village"]["is_psvr2"] == 1
    assert by_title["Synth Riders"]["is_psvr2"] == 1
    # full catalog title still overridden via its igdb_id (the bug this fixes)
    assert by_title["Atari 50: The Anniversary Celebration"]["platform"] == "playstation 5"
    assert by_title["Kingdom Hearts II"]["platform"] == "playstation 4"
    # control is left generic (no auto signal, not overridden)
    assert by_title["Uncharted 4"]["platform"] == "playstation"
    assert by_title["Uncharted 4"]["is_psvr2"] == 0
    assert 132181 in PSVR2_OVERRIDES and 55163 in PSVR2_OVERRIDES
    assert PLATFORM_OVERRIDES[55163] == "playstation 5"
