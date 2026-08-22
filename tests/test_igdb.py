"""IGDB enrichment asset tests (mocked client): igdb_matches (external_games
for digital, name search for physical), game_metadata (paced, resumable),
catalog_views read model."""

from __future__ import annotations

import json
import tempfile

from dagster import build_op_context

from mailroom.db import connect, init_db, upsert_owned_game
from mailroom.verticals.game_catalog import assets


class _StubIgdb:
    def __init__(self, external: dict | None = None, search: dict | None = None, details: dict | None = None):
        self.external = external or {}
        self.search = search or {}
        self.details = details or {}

    def game_by_external_psn_uid(self, uid: str) -> int | None:
        return self.external.get(uid)

    def search_game(self, name: str) -> list[dict]:
        for key, games in self.search.items():
            if key in name:
                return games
        return []

    def game_details(self, game_id: int) -> dict:
        return self.details.get(game_id, {})


def _seed_game(conn, title, platform="playstation 5", psn_content_id=None, igdb_id=None, source="psn_receipt"):
    upsert_owned_game(
        conn,
        {
            "title": title,
            "normalized_title": assets.normalize_title(title),
            "platform": platform,
            "format": "digital" if psn_content_id else "physical",
            "ownership_class": "purchased",
            "retailer": None,
            "order_number": None,
            "item_id": None,
            "condition": None,
            "psn_content_id": psn_content_id,
            "igdb_id": igdb_id,
            "acquisition_date": None,
            "price": None,
            "source": source,
            "source_ref": f"t:{title}",
            "status": "owned",
            "is_owned": 1,
            "provenance": f"{source}:{title}",
        },
    )


def _ctx(db, stub):
    return build_op_context(resources={"db_url": db, "igdb": stub})


def test_igdb_search_term_strips_noise():
    assert assets.igdb_search_term("Cyberpunk 2077 - PlayStation 4") == "cyberpunk 2077"
    assert assets.igdb_search_term("SEALED Wildermyth for Sony PlayStation 5 (PS5) w/ Monster") == "wildermyth"
    assert assets.igdb_search_term("Persona 5 Royal - PlayStation 5") == "persona 5 royal"
    assert assets.igdb_search_term("ABZÛ - PlayStation 4") == "abzu"  # accents kept


def test_igdb_search_terms_fallbacks():
    terms = assets.igdb_search_terms("DIRT5")
    assert "dirt 5" in terms  # digit-split recovers the space
    terms = assets.igdb_search_terms("God of War III Remastered Standard Edition - PlayStation 4")
    assert terms[0] == "god war iii"  # stripped (edition phrases removed whole)
    assert "god of war iii remastered standard edition - playstation 4" in terms  # raw fallback
    assert "god war 3" in terms  # roman -> digits variant
    terms = assets.igdb_search_terms("CoffeeTalk")
    assert terms[0] == "coffeetalk"


def test_igdb_matches_prefers_platform_among_same_name_results():
    """Resident Evil 4 case: three same-name entries (2005/2011/2023) — the
    PS5 copy must match the 2023 remake (132181), not the HD (20065)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Resident Evil 4 - PS5", platform="ps5")
    conn.close()

    stub = _StubIgdb(
        search={
            "resident evil 4": [
                {"id": 145201, "name": "Resident Evil 4 and Resident Evil Code: Veronica X Bundle", "platforms": [48, 167]},
                {"id": 20065, "name": "Resident Evil 4", "platforms": [9, 12]},  # HD (PS3)
                {"id": 132181, "name": "Resident Evil 4", "platforms": [48, 167]},  # 2023 remake (PS5)
                {"id": 145191, "name": "Resident Evil 4", "platforms": [8]},  # 2005 original (PS2)
            ]
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 132181, "must pick the PS5-platform same-name entry"
    conn.close()


def test_igdb_matches_prefers_exact_name_over_first_result():
    """Elden Ring case: IGDB search ranks Nightreign first — the exact-name
    result must win (the 2022 original, 119133)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "ELDEN RING™", platform="playstation 5")
    conn.close()

    stub = _StubIgdb(
        search={
            "elden ring": [
                {"id": 325591, "name": "Elden Ring Nightreign"},
                {"id": 119133, "name": "Elden Ring"},
            ]
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 119133, "must pick the exact-name Elden Ring, not Nightreign"
    m = conn.execute("SELECT matched_title FROM igdb_matches").fetchone()
    assert m["matched_title"] == "Elden Ring"
    conn.close()


def test_igdb_matches_recheck_config_rematches_all():
    """recheck config clears igdb_id and re-matches — heals wrong picks
    (a row matched to igdb 999 re-resolves to the exact-name result)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Elden Ring", platform="playstation 5", igdb_id=325591)  # wrong (Nightreign)
    conn.close()

    stub = _StubIgdb(
        search={
            "elden ring": [
                {"id": 325591, "name": "Elden Ring Nightreign", "platforms": [167]},
                {"id": 119133, "name": "Elden Ring", "platforms": [48, 167]},
            ]
        }
    )
    assets.igdb_matches(build_op_context(resources={"db_url": f"sqlite:///{db}", "igdb": stub}, config={"recheck": True}))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 119133, "recheck must re-resolve to the exact-name Elden Ring"
    conn.close()


def test_igdb_matches_falls_back_when_stripped_term_misses():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "DIRT5", platform="playstation 4")
    conn.close()

    # stripped term "dirt5" matches nothing; digit-split "dirt 5" does
    stub = _StubIgdb(search={"dirt 5": [{"id": 18623, "name": "DIRT 5"}]})
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 18623
    conn.close()


def test_igdb_search_term_strips_hits_branding():
    """'Hits' (PlayStation Hits re-release branding) is stripped so the search
    term drops the branding; the internal ':' is retained (matching the rest
    of the pipeline — token matching strips punctuation downstream)."""
    assert assets.igdb_search_term("Uncharted: Nathan Drake Collection Hits - PlayStation 4") == "uncharted: nathan drake collection"
    assert assets._igdb_norm("Uncharted: Nathan Drake Collection Hits") == "uncharted: nathan drake collection"


def test_igdb_matches_hits_branding_title():
    """A 'Hits' re-release title matches its plain IGDB entry (forward token
    containment handles the IGDB 'The' that the title omits)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Uncharted: Nathan Drake Collection Hits - PlayStation 4", platform="playstation 4")
    conn.close()

    stub = _StubIgdb(search={"uncharted: nathan drake collection": [{"id": 35316, "name": "Uncharted: The Nathan Drake Collection", "platforms": [48]}]})
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 35316
    conn.close()


def test_igdb_matches_digital_via_external_and_physical_via_search():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "God of War", psn_content_id="UP9000-CUSA07408_00-00000000GODOFWAR")
    _seed_game(conn, "Cyberpunk 2077 - PlayStation 4", platform="playstation 4")
    _seed_game(conn, "Some Obscure Disc Game - PlayStation 5")
    conn.close()

    stub = _StubIgdb(
        external={"UP9000-CUSA07408_00-00000000GODOFWAR": 1942},
        search={"cyberpunk 2077": [{"id": 41494, "name": "Cyberpunk 2077"}]},
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))

    conn = connect(f"sqlite:///{db}")
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM owned_games").fetchall()}
    assert rows["God of War"]["igdb_id"] == 1942
    assert rows["Cyberpunk 2077 - PlayStation 4"]["igdb_id"] == 41494
    assert rows["Some Obscure Disc Game - PlayStation 5"]["igdb_id"] is None
    m = conn.execute("SELECT confidence, matched_title FROM igdb_matches WHERE owned_game_id = ?", (rows["God of War"]["id"],)).fetchone()
    assert m["confidence"] == "high"
    conn.close()


def test_game_metadata_paced_resumable_and_catalog_view():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "God of War", igdb_id=1942)
    _seed_game(conn, "Cyberpunk 2077", igdb_id=41494)
    conn.close()

    stub = _StubIgdb(details={1942: {"id": 1942, "name": "God of War", "total_rating": 90.0}})
    assets.game_metadata(_ctx(f"sqlite:///{db}", stub))
    assets.catalog_views(_ctx(f"sqlite:///{db}", stub))

    conn = connect(f"sqlite:///{db}")
    payload = conn.execute("SELECT payload FROM game_metadata WHERE igdb_id = 1942").fetchone()["payload"]
    assert json.loads(payload)["total_rating"] == 90.0
    # resumable: 41494 has no details -> not stored; re-run doesn't refetch 1942
    assets.game_metadata(_ctx(f"sqlite:///{db}", stub))
    assert conn.execute("SELECT COUNT(*) n FROM game_metadata").fetchone()["n"] == 1
    # catalog view joins metadata
    row = conn.execute("SELECT game_id, title, igdb_id, igdb_payload FROM catalog_views WHERE title = 'God of War'").fetchone()
    assert row is not None
    assert row["igdb_payload"] is not None
    conn.close()


def test_game_metadata_backfills_generic_platform_and_marks_psvr2():
    """A generic 'playstation' owned row backfills to the concrete PS5 when the
    matched IGDB entry's platforms are unambiguous, and the view flags PSVR2
    (platform id 390) without changing the platform (PSVR2 runs on PS5)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Some VR Game", platform="playstation", igdb_id=777001)
    conn.close()

    stub = _StubIgdb(details={777001: {"id": 777001, "name": "Some VR Game",
                                       "platforms": [{"id": 167, "name": "PlayStation 5"},
                                                     {"id": 390, "name": "PlayStation VR2"}]}})
    assets.game_metadata(_ctx(f"sqlite:///{db}", stub))
    assets.catalog_views(_ctx(f"sqlite:///{db}", stub))

    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT platform FROM owned_games").fetchone()
    assert row["platform"] == "playstation 5", "generic row must backfill to PS5 (167 present)"
    view = conn.execute("SELECT is_psvr2 FROM catalog_views").fetchone()
    assert view["is_psvr2"] == 1, "platform 390 (PSVR2) must flag is_psvr2"
    conn.close()


def test_game_metadata_backfills_generic_platform_to_ps4():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Some PS4 Game", platform="ps", igdb_id=777002)
    conn.close()

    stub = _StubIgdb(details={777002: {"id": 777002, "name": "Some PS4 Game",
                                       "platforms": [{"id": 48, "name": "PlayStation 4"}]}})
    assets.game_metadata(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT platform FROM owned_games").fetchone()
    assert row["platform"] == "playstation 4"
    conn.close()


def test_game_metadata_backfill_leaves_crossgen_ambiguous():
    """A cross-gen game (both PS4 AND PS5) has no single concrete platform —
    the generic row is left alone rather than guessed."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Cross Gen Game", platform="playstation", igdb_id=777003)
    conn.close()

    stub = _StubIgdb(details={777003: {"id": 777003, "name": "Cross Gen Game",
                                       "platforms": [{"id": 48, "name": "PlayStation 4"},
                                                     {"id": 167, "name": "PlayStation 5"}]}})
    assets.game_metadata(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT platform FROM owned_games").fetchone()
    assert row["platform"] == "playstation", "cross-gen must stay generic (ambiguous)"
    conn.close()


def test_owned_games_receipts_path_supplies_ownership_class():
    """Regression: the receipts path of owned_games must supply ownership_class
    (latent bug from the schema change — ProgrammingError otherwise)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    conn.execute(
        """INSERT INTO classified_game_items (source, order_number, item_key, title, platform, classification, reason)
           VALUES ('gamestop', 'o1', 'o1:game', 'Cyberpunk 2077 - PlayStation 4', 'playstation 4', 'playstation_game', 'platform match')"""
    )
    conn.commit()
    conn.close()

    assets.owned_games(_ctx(f"sqlite:///{db}", _StubIgdb()))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT format, ownership_class FROM owned_games").fetchone()
    assert row["format"] == "physical"
    assert row["ownership_class"] == "purchased"
    conn.close()


def test_owned_games_classifies_zero_price_psn_receipt_as_psplus_claim():
    """PSN Store emails with a $0.00 line item are PS+ claims (Hot Wheels
    Unleashed 10/04/2022, Witcher 3 Complete Edition 12/14/2022) — NOT
    purchases. A paid PSN item stays purchased; the price is threaded through."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    for order, title, price in [
        ("369813850135", "HOT WHEELS UNLEASHED™ (Game)", "$0.00"),
        ("384720390939", "The Witcher 3: Wild Hunt – Complete Edition (Game)", "$0.00"),
        ("787094772850142", "Armello™ (Game)", "$7.49"),
    ]:
        item_key = f"{order}:0"
        conn.execute(
            """INSERT INTO parsed_purchases(source, order_number, item_key, purchased_at, title, platform, price)
               VALUES ('psn_receipt', ?, ?, '2022-10-04', ?, 'playstation', ?)""",
            (order, item_key, title, price),
        )
        conn.execute(
            """INSERT INTO classified_game_items(source, order_number, item_key, title, platform, classification, reason)
               VALUES ('psn_receipt', ?, ?, ?, 'playstation', 'playstation_game', 'PSN receipt (platform implicit)')""",
            (order, item_key, title),
        )
    conn.commit()
    conn.close()

    assets.owned_games(_ctx(f"sqlite:///{db}", _StubIgdb()))
    conn = connect(f"sqlite:///{db}")
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM owned_games").fetchall()}
    assert rows["HOT WHEELS UNLEASHED™ (Game)"]["ownership_class"] == "psplus_claimed"
    assert rows["HOT WHEELS UNLEASHED™ (Game)"]["price"] == "$0.00"
    assert rows["The Witcher 3: Wild Hunt – Complete Edition (Game)"]["ownership_class"] == "psplus_claimed"
    assert rows["Armello™ (Game)"]["ownership_class"] == "purchased"
    assert rows["Armello™ (Game)"]["price"] == "$7.49"
    conn.close()


def test_owned_games_merge_order_paid_receipt_wins_over_claim():
    """Same game from two receipts — one paid, one $0 PS+ claim — the merged
    row is 'purchased' (any real purchase owns the game) regardless of row
    order; a claim-only game stays 'psplus_claimed'."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    for order, price in [
        ("263932697921", "$39.99"),   # paid purchase
        ("384720390939", "$0.00"),    # later $0 PS+ claim (Witcher 3 case)
    ]:
        item_key = f"{order}:0"
        conn.execute(
            """INSERT INTO parsed_purchases(source, order_number, item_key, purchased_at, title, platform, price)
               VALUES ('psn_receipt', ?, ?, '2022-12-14', 'The Witcher 3: Wild Hunt – Complete Edition (Game)', 'playstation', ?)""",
            (order, item_key, price),
        )
        conn.execute(
            """INSERT INTO classified_game_items(source, order_number, item_key, title, platform, classification, reason)
               VALUES ('psn_receipt', ?, ?, 'The Witcher 3: Wild Hunt – Complete Edition (Game)', 'playstation', 'playstation_game', 'PSN receipt (platform implicit)')""",
            (order, item_key),
        )
    conn.commit()
    conn.close()

    assets.owned_games(_ctx(f"sqlite:///{db}", _StubIgdb()))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT ownership_class, price FROM owned_games").fetchone()
    assert row["ownership_class"] == "purchased"  # the $39.99 purchase wins
    conn.close()


class _ExactKeyStub(_StubIgdb):
    """search stub keyed on the FULL normalized term (not substring), so a
    multi-term search ('gods' vs 'gods remastered') returns different results."""

    def search_game(self, name: str) -> list[dict]:
        key = name.strip().lower()
        if key in self.search:
            return self.search[key]
        return []


def test_igdb_matches_multi_term_exact_surfaces_gods_remastered():
    """GODS Remastered case: stripped term 'gods' ranks God of War Ragnarök
    first, but the raw term 'gods remastered' surfaces the exact entry — the
    matcher must search every term and prefer the exact-name result."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "GODS Remastered", platform="playstation 4", source="psn_api")
    conn.close()

    stub = _ExactKeyStub(
        search={
            "gods": [{"id": 112875, "name": "God of War Ragnarök", "platforms": [48, 167]}],
            "gods remastered": [{"id": 112099, "name": "Gods Remastered", "platforms": [48, 130]}],
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 112099, "must pick the exact-name Gods Remastered, not Ragnarök"
    conn.close()


def test_igdb_matches_single_token_title_corrected_by_repair():
    """Unplugged case: the single-token search ranks Rock Band Unplugged first
    (no exact name in the top results), so igdb_matches alone picks it — the
    content-id repair that runs after it in the same job must re-pin the VR
    game (153854). The read model never shows the wrong pick."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Unplugged", platform="playstation 5", psn_content_id="UP3535-PPSA14005_00-0297859396070977", source="psn_api")
    conn.close()

    stub = _StubIgdb(
        search={
            "unplugged": [{"id": 2721, "name": "Rock Band Unplugged", "platforms": [38]}],
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    from mailroom.verticals.game_catalog.repairs import apply_catalog_repairs

    conn = connect(f"sqlite:///{db}")
    apply_catalog_repairs(conn)  # the repair runs after igdb_matches in production
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 153854, "repair must pin Unplugged (VR air guitar), not Rock Band Unplugged"
    conn.close()


def test_igdb_matches_token_gate_rejects_unrelated_first_result():
    """The Music of Dreams case: no exact IGDB game exists; the gated fallback
    must reject 'High School Musical: Livin' the Dream' (no token overlap on
    'music'/'dreams') instead of linking a random game."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "The Music of Dreams", platform="playstation 4", psn_content_id="UP9000-CUSA18544_00-DREAMSOST0000001", source="psn_api")
    conn.close()

    stub = _StubIgdb(
        search={
            "music dreams": [{"id": 49478, "name": "High School Musical: Livin' the Dream", "platforms": [38]}],
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] is None, "must not match the Dreams OST to a random High School Musical game"
    conn.close()


def test_igdb_matches_roman_numeral_sequel():
    """God of War III Remastered case: 'iii' normalizes to '3' so the sequel
    search term finds the correct entry; the PS4 copy picks the Remastered
    entry over the 2010 PS3 original."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "God of War III Remastered Standard Edition - PlayStation 4", platform="playstation 4", source="gamestop")
    conn.close()

    stub = _ExactKeyStub(
        search={
            "god war iii": [
                {"id": 1112, "name": "God of War III", "platforms": [9]},
                {"id": 15854, "name": "God of War III Remastered", "platforms": [48]},
            ],
            "god war 3": [
                {"id": 1112, "name": "God of War III", "platforms": [9]},
                {"id": 15854, "name": "God of War III Remastered", "platforms": [48]},
            ],
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 15854, "must pick the PS4 God of War III Remastered, not the 2010 original"
    conn.close()


def test_igdb_matches_single_token_edition_title():
    """'Control Ultimate Edition' strips to the single token 'control' — the
    exact name 'Control' must still match (no blind-fallback regression)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Control Ultimate Edition", platform="playstation 4", source="psn_api")
    conn.close()

    stub = _StubIgdb(
        search={
            "control": [
                {"id": 103329, "name": "Control", "platforms": [48, 167]},
                {"id": 227808, "name": "Mob Control", "platforms": [34]},
            ]
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 103329, "single-token term must match the exact-name 'Control'"
    conn.close()


def test_igdb_matches_concatenated_title_matches_spaced_name():
    """'CoffeeTalk' (PSN concatenates the words) must match IGDB 'Coffee Talk'
    via the space-less name check."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "CoffeeTalk", platform="playstation 4", source="psn_api")
    conn.close()

    stub = _StubIgdb(
        search={
            "coffeetalk": [{"id": 106847, "name": "Coffee Talk", "platforms": [48]}],
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 106847
    conn.close()


def test_igdb_matches_receipt_title_with_extra_words():
    """'Unplugged - Air Guitar (Game)' (receipt) must match the VR game
    'Unplugged' via reverse containment (name tokens are a subset of the
    term), not stay unmatched."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Unplugged - Air Guitar (Game)", platform="playstation", source="psn_receipt")
    conn.close()

    stub = _StubIgdb(
        search={
            "unplugged - air guitar": [{"id": 153854, "name": "Unplugged", "platforms": [167]}],
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 153854
    conn.close()


def test_igdb_matches_single_token_common_word_stays_unmatched():
    """'Skyrim' / 'Dreams' are single common-word tokens: a longer name that
    merely CONTAINS the token ('The Elder Scrolls V: Skyrim', '...of Dreams')
    must not auto-link — ambiguous titles stay unmatched for review."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Dreams™", platform="playstation 4", source="psn_api")
    conn.close()

    stub = _StubIgdb(
        search={
            "dreams": [
                {"id": 330338, "name": "The House in Fata Morgana: Remake of Dreams", "platforms": [48, 167]},
                {"id": 11155, "name": "Dreams", "platforms": [48]},
            ]
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 11155, "the exact-name 'Dreams' entry wins when present"
    conn.close()


def test_igdb_matches_single_token_ambiguous_no_exact_stays_unmatched():
    """No exact-name entry in the results -> single-token title stays
    unmatched instead of linking a game that merely contains the word."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "Skyrim", platform="playstation 4", source="psn_api")
    conn.close()

    stub = _StubIgdb(
        search={
            "skyrim": [{"id": 13934, "name": "The Elder Scrolls V: Skyrim", "platforms": [48, 167]}],
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] is None, "ambiguous single-token title must stay unmatched (review)"
    conn.close()


def test_igdb_matches_roman_single_i_at_end():
    """'The Last of Us Part I' normalizes to 'part 1' — a 'Part 1 - PS5'
    title must exact-match it (single 'i' roman conversion, final token only)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "The Last of Us Part 1 - PlayStation 5", platform="playstation 5", source="bestbuy")
    conn.close()

    stub = _StubIgdb(
        search={
            "last part 1": [
                {"id": 204350, "name": "The Last of Us Part I", "platforms": [6, 167]},
            ]
        }
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 204350
    conn.close()


def test_igdb_search_term_strips_version_word():
    assert assets.igdb_search_term("Wreckfest PlayStation®5 Version (Game)") == "wreckfest"
