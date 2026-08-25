"""catalog_quality_repairs tests: cancelled-order junk retirement, non-game
add-on retirement, jammed-content-id splitting, and short-title rematch.
Every repair must be idempotent and audited to review_queue."""

from __future__ import annotations

import json
import tempfile

from mailroom.db import connect, init_db
from mailroom.verticals.game_catalog.parsers.psn import normalize_title
from mailroom.verticals.game_catalog.repairs import apply_catalog_repairs

JAMMED_PROVENANCE = json.dumps(
    [
        "psn_receipt:252319130093:0",
        "cdkeys:0151509331:God of War Ragnarök PS5 (US)",
        "psn_api:UP3909-CUSA15513_00-GODSREMASTERED00",
        "psn_api:UP9000-PPSA08329_00-GOWRAGNAROK00000",
    ]
)


def _db():
    path = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{path}")
    init_db(conn)
    return conn, path


def _seed(conn, **kw) -> int:
    title = kw.get("title", "Some Game")
    cur = conn.execute(
        """INSERT INTO owned_games
           (title, normalized_title, platform, format, ownership_class, retailer,
            order_number, item_id, condition, psn_content_id, igdb_id,
            acquisition_date, price, source, source_ref, status, is_owned, provenance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'owned', 1, ?)""",
        (
            title,
            normalize_title(title),
            kw.get("platform", "playstation 4"),
            kw.get("format", "digital"),
            kw.get("ownership_class", "purchased"),
            kw.get("retailer"),
            kw.get("order_number"),
            None,
            None,
            kw.get("psn_content_id"),
            kw.get("igdb_id"),
            None,
            None,
            kw.get("source", "psn_api"),
            kw.get("source_ref") or kw.get("psn_content_id") or f"ref:{title}",
            kw.get("provenance") or f"psn_api:{kw.get('psn_content_id') or 'n/a'}",
        ),
    )
    conn.commit()
    return cur.lastrowid


def test_retires_cancelled_order_junk():
    conn, _ = _db()
    pid = _seed(
        conn,
        title='Master Plunger MPS4 Sink ..." has been canceled',
        platform="playstation",
        format="physical",
        source="amazon",
        order_number="111-8374686-1690617",
        igdb_id=None,
    )
    report = apply_catalog_repairs(conn)
    assert [r["id"] for r in report.retired] == [pid]
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (pid,)).fetchone()
    assert row["is_owned"] == 0
    assert row["retire_reason"] == "not_a_game:cancelled_order"
    # audited
    assert conn.execute("SELECT COUNT(*) n FROM review_queue WHERE source='catalog_repair'").fetchone()["n"] == 1
    conn.close()


def test_retires_dreams_ost_and_artbook():
    conn, _ = _db()
    ost = _seed(conn, title="The Music of Dreams", psn_content_id="UP9000-CUSA18544_00-DREAMSOST0000001", igdb_id=49478)
    art = _seed(conn, title="The Art of Dreams", psn_content_id="UP9000-CUSA18543_00-DREAMSARTBOOK001", igdb_id=202330)
    report = apply_catalog_repairs(conn)
    assert {r["id"] for r in report.retired} == {ost, art}
    for rid in (ost, art):
        row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (rid,)).fetchone()
        assert row["is_owned"] == 0
        assert row["retire_reason"].startswith("not_a_game:dreams_")
    conn.close()


def test_splits_jammed_content_ids():
    conn, _ = _db()
    jammed = _seed(
        conn,
        title="GODS Remastered",
        platform="playstation 4",
        psn_content_id="UP3909-CUSA15513_00-GODSREMASTERED00,UP9000-PPSA08329_00-GOWRAGNAROK00000",
        igdb_id=112875,
        provenance=JAMMED_PROVENANCE,
    )
    report = apply_catalog_repairs(conn)

    # Original row keeps GODS Remastered with its own content id + correct igdb.
    gods = conn.execute("SELECT * FROM owned_games WHERE id = ?", (jammed,)).fetchone()
    assert gods["title"] == "GODS Remastered"
    assert gods["psn_content_id"] == "UP3909-CUSA15513_00-GODSREMASTERED00"
    assert gods["igdb_id"] == 112099
    assert gods["platform"] == "playstation 4"
    assert "252319130093" in gods["provenance"] and "GOWRAGNAROK" not in gods["provenance"]

    # A fresh row exists for Ragnarök with its own content id + the 94.6 metadata.
    ragnarok = conn.execute(
        "SELECT * FROM owned_games WHERE psn_content_id = ? AND is_owned = 1",
        ("UP9000-PPSA08329_00-GOWRAGNAROK00000",),
    ).fetchone()
    assert ragnarok is not None
    assert ragnarok["title"] == "God of War Ragnarök"
    assert ragnarok["platform"] == "playstation 5"
    assert ragnarok["igdb_id"] == 112875
    assert ragnarok["id"] != jammed
    prov = json.loads(ragnarok["provenance"])
    assert "cdkeys:0151509331:God of War Ragnarök PS5 (US)" in prov
    assert "psn_api:UP9000-PPSA08329_00-GOWRAGNAROK00000" in prov
    assert "GODSREMASTERED" not in ragnarok["provenance"]

    assert len(report.split) == 2  # kept + new row
    conn.close()


def test_rematches_ambiguous_short_title_by_content_id():
    conn, _ = _db()
    unplugged = _seed(
        conn,
        title="Unplugged",
        platform="playstation 5",
        psn_content_id="UP3535-PPSA14005_00-0297859396070977",
        igdb_id=2721,  # wrong: Rock Band Unplugged
        source="psn_api",
    )
    report = apply_catalog_repairs(conn)
    assert [r["id"] for r in report.rematched] == [unplugged]
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (unplugged,)).fetchone()
    assert row["igdb_id"] == 153854  # Unplugged (VR air guitar)
    assert conn.execute(
        "SELECT confidence FROM igdb_matches WHERE owned_game_id = ?", (unplugged,)
    ).fetchone()["confidence"] == "manual"
    conn.close()


def test_rematches_dlc_shadow_to_base_game_by_content_id():
    conn, _ = _db()
    game = _seed(
        conn,
        title="Crypt of the NecroDancer",
        platform="playstation 4",
        psn_content_id="UP1162-CUSA03610_00-CRYPTNECRODANCER",
        igdb_id=212583,  # wrong: IGDB 'Crypt of the NecroDancer: Synchrony' (DLC)
        source="psn_api",
    )
    report = apply_catalog_repairs(conn)
    assert [r["id"] for r in report.rematched] == [game]
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (game,)).fetchone()
    assert row["igdb_id"] == 7886  # IGDB 'Crypt of the NecroDancer' (base game, 2015)
    assert conn.execute(
        "SELECT confidence FROM igdb_matches WHERE owned_game_id = ?", (game,)
    ).fetchone()["confidence"] == "manual"
    conn.close()


def test_rematches_dlc_shadow_to_base_game_by_title():
    """A receipt row with no psn_content_id ('Batman™: Arkham Knight (Game)')
    matched to a skin DLC (26041) is re-pinned to the base game by cleaned title."""
    conn, _ = _db()
    game = _seed(
        conn,
        title="Batman™: Arkham Knight (Game)",
        platform="playstation",
        source="psn_receipt",
        igdb_id=26041,  # wrong: 'Batman: Arkham Knight - 2008 Movie Batman Skin'
    )
    report = apply_catalog_repairs(conn)
    assert [r["id"] for r in report.rematched] == [game]
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (game,)).fetchone()
    assert row["igdb_id"] == 5503  # IGDB 'Batman: Arkham Knight' (2015)
    conn.close()


def test_splits_collection_bundle_into_member_games():
    """'Hotline Miami Collection' (one receipt row) is retired and split into
    Hotline Miami + Hotline Miami 2, each owned with the correct IGDB id."""
    conn, _ = _db()
    coll = _seed(
        conn, title="Hotline Miami Collection (Full Game)", platform="playstation",
        format="digital", source="psn_receipt", igdb_id=99733,
        provenance="psn_receipt:298438957255:0",
    )
    apply_catalog_repairs(conn)
    # collection retired
    c = conn.execute("SELECT * FROM owned_games WHERE id = ?", (coll,)).fetchone()
    assert c["is_owned"] == 0 and c["retire_reason"] == "collection_split"
    # both members now owned
    ids = {r["igdb_id"] for r in conn.execute(
        "SELECT igdb_id FROM owned_games WHERE is_owned = 1")}
    assert 1384 in ids and 2126 in ids  # Hotline Miami, Hotline Miami 2
    conn.close()


def test_splits_collection_merges_into_already_owned_member():
    """A collection member already owned ('BioShock Infinite' as the Complete
    Edition) gets the collection receipt merged into its provenance instead of
    a duplicate card."""
    conn, _ = _db()
    existing = _seed(
        conn, title="Bioshock Infinite: The Complete Edition",
        platform="playstation 4", source="psn_api",
        psn_content_id="UP1001-CUSA03979_00-BIOSHOCKCOLLECTN", igdb_id=41595,
        provenance="psn_api:UP1001-CUSA03979_00-BIOSHOCKCOLLECTN",
    )
    coll = _seed(
        conn, title="BioShock: The Collection (Full Game)", platform="playstation",
        format="digital", source="psn_receipt", igdb_id=19839,
        provenance="psn_receipt:504599545638:0",
    )
    apply_catalog_repairs(conn)
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (existing,)).fetchone()
    assert row["is_owned"] == 1
    assert "psn_receipt:504599545638:0" in (row["provenance"] or "")
    assert conn.execute("SELECT * FROM owned_games WHERE id = ?", (coll,)).fetchone()["is_owned"] == 0
    conn.close()


def test_pins_subnautica_to_its_own_game():
    """'Subnautica' (base) was landing on the Below Zero IGDB entry, so the
    base card showed Below Zero's cover. The title override pins it to its own
    game (9254) so each game keeps its own artwork."""
    conn, _ = _db()
    base = _seed(
        conn, title="Subnautica", platform="playstation 4",
        source="psn_api", psn_content_id="UP4083-CUSA07147_00-SUBNAUTICA000000",
        igdb_id=107315,  # wrongly matched to 'Subnautica: Below Zero'
        provenance="psn_api:UP4083-CUSA07147_00-SUBNAUTICA000000",
    )
    report = apply_catalog_repairs(conn)
    assert [r["id"] for r in report.rematched] == [base]
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (base,)).fetchone()
    assert row["igdb_id"] == 9254  # IGDB 'Subnautica' (base game, 2018)
    conn.close()


def test_pins_synth_riders_to_base_game():
    """'Synth Riders' pinned to the canonical base entry (105333), which is the
    IGDB id that carries the platform-390 (PSVR2) signal — not the PS5-only
    re-listing IGDB search can surface first."""
    conn, _ = _db()
    game = _seed(
        conn, title="Synth Riders", platform="playstation 5",
        source="psn_api", psn_content_id="UP4363-PPSA10201_00-SYNTHRIDERS00000",
        igdb_id=372492,  # a PS5-only re-listing without platform 390
        provenance="psn_api:UP4363-PPSA10201_00-SYNTHRIDERS00000",
    )
    report = apply_catalog_repairs(conn)
    assert [r["id"] for r in report.rematched] == [game]
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (game,)).fetchone()
    assert row["igdb_id"] == 105333  # IGDB 'Synth Riders' (base, has PSVR2 platform 390)
    conn.close()


def test_splits_metal_gear_solid_master_collection():
    """'Metal Gear Solid: Master Collection Vol. 1' (one PS5 receipt) is
    retired and broken into Metal Gear Solid / MGS2 / MGS3, each owned."""
    conn, _ = _db()
    coll = _seed(
        conn, title="Metal Gear Solid: Master Collection", platform="playstation 5",
        format="physical", source="amazon", igdb_id=393638,
        order_number="114-0000000-0000000", price="$59.99",
        provenance="amazon:114-0000000-0000000:Metal Gear Solid: Master Collection",
    )
    apply_catalog_repairs(conn)
    assert conn.execute("SELECT * FROM owned_games WHERE id = ?", (coll,)).fetchone()["is_owned"] == 0
    ids = {r["igdb_id"] for r in conn.execute("SELECT igdb_id FROM owned_games WHERE is_owned = 1")}
    assert 375 in ids and 376 in ids and 379 in ids  # MGS1 / MGS2 / MGS3
    conn.close()


def test_crossgen_title_pinned_to_ps5():
    """A cross-gen receipt title advertising both PS4 and PS5 ('One Hand
    Clapping PS4 & PS5') was classified as generic 'playstation'; the repair
    pins it to PlayStation 5. Single-platform / concrete rows untouched."""
    conn, _ = _db()
    one_hand = _seed(
        conn, title="One Hand Clapping PS4 & PS5", platform="playstation",
        format="digital", source="psn_receipt", igdb_id=None,
        provenance="psn_receipt:298438957255:0",
    )
    sonic = _seed(conn, title="Sonic Superstars PS4", platform="playstation",
                  format="digital", source="psn_receipt", igdb_id=None,
                  provenance="psn_receipt:111111111111:0")
    report = apply_catalog_repairs(conn)
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (one_hand,)).fetchone()
    assert row["platform"] == "playstation 5"
    sonic_row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (sonic,)).fetchone()
    assert sonic_row["platform"] == "playstation"  # single-platform — untouched
    assert any(c["id"] == one_hand for c in report.cleaned)
    conn.close()


def test_splits_final_fantasy_i_vi_collection_by_title():
    """'FINAL FANTASY I-VI Collection Anniversary Edition' (one physical
    receipt, igdb_id NULL / unmatched) is split by TITLE into the six Pixel
    Remaster games, each owned with its own IGDB id."""
    conn, _ = _db()
    coll = _seed(
        conn, title="FINAL FANTASY I-VI Collection Anniversary Edition",
        platform="playstation 5", format="physical", source="amazon",
        igdb_id=None,  # unmatched — split is title-driven
        order_number="112-0000000-0000000", price="$74.99",
        provenance="amazon:112-0000000-0000000:FINAL FANTASY I-VI Collection Anniversary Edition",
    )
    apply_catalog_repairs(conn)
    assert conn.execute("SELECT * FROM owned_games WHERE id = ?", (coll,)).fetchone()["is_owned"] == 0
    ids = {r["igdb_id"] for r in conn.execute("SELECT igdb_id FROM owned_games WHERE is_owned = 1")}
    assert {158980, 158981, 158982, 158983, 158984, 158985} <= ids  # FF I-VI Pixel Remaster
    conn.close()


def test_rematches_beyond_a_steel_sky_steelbook_by_title():
    """'Beyond A Steel Sky: Beyond A SteelBook Edition (PS5)' (Amazon order) is
    pinned to the SteelBook Edition (171279), which IGDB search can't surface
    from the noisy Amazon title."""
    conn, _ = _db()
    game = _seed(
        conn,
        title="Beyond A Steel Sky: Beyond A SteelBook Edition",
        platform="playstation 5",
        source="amazon",
        igdb_id=None,
        order_number="113-7134038-7289042",
    )
    report = apply_catalog_repairs(conn)
    assert [r["id"] for r in report.rematched] == [game]
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (game,)).fetchone()
    assert row["igdb_id"] == 171279  # IGDB 'Beyond a Steel Sky: Beyond a Steel Book Edition'
    conn.close()


def test_splits_persona_endless_night_collection():
    """Persona Dancing: Endless Night Collection splits into its 3 games;
    Persona 3 Dancing in Moonlight (already owned) is merged, Persona 4 and
    Persona 5 get fresh rows."""
    conn, _ = _db()
    p3 = _seed(
        conn, title="Persona 3: Dancing in Moonlight", platform="playstation 4",
        source="psn_api", psn_content_id="UP2611-CUSA12380_00-PDANENDLESSNIGHT", igdb_id=54217,
        provenance="psn_api:UP2611-CUSA12380_00-PDANENDLESSNIGHT",
    )
    coll = _seed(
        conn, title="Persona Dancing: Endless Night Collection (Full Game)",
        platform="playstation", format="digital", source="psn_receipt", igdb_id=106988,
        provenance="psn_receipt:786850916202565:0",
    )
    apply_catalog_repairs(conn)
    # collection retired; Persona 3 merged (still owned), P4 + P5 created
    assert conn.execute("SELECT * FROM owned_games WHERE id = ?", (coll,)).fetchone()["is_owned"] == 0
    ids = {r["igdb_id"] for r in conn.execute("SELECT igdb_id FROM owned_games WHERE is_owned = 1")}
    assert 54217 in ids and 11056 in ids and 54218 in ids
    p3row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (p3,)).fetchone()
    assert "psn_receipt:786850916202565:0" in (p3row["provenance"] or "")
    conn.close()


def test_splits_uncharted_legacy_of_thieves_collection():
    """'Uncharted: Legacy of Thieves Collection' (one PS5 physical receipt) is
    retired and broken out: Uncharted 4 (already owned, PS4 PS+) merges the
    collection receipt into its provenance, and The Lost Legacy gets a fresh
    owned row so it's no longer hidden inside the collection card."""
    conn, _ = _db()
    uncharted4 = _seed(
        conn, title="UNCHARTED 4: A Thief’s End", platform="playstation 4",
        source="psn_receipt", psn_content_id="UP9000-CUSA00341_00-UNCHARTED0000000",
        igdb_id=7331, ownership_class="psplus_claimed",
        provenance="psn_receipt:247418470492:0",
    )
    coll = _seed(
        conn, title="Uncharted: Legacy of Thieves Collection", platform="playstation 5",
        format="physical", source="amazon", igdb_id=168670,
        order_number="112-8319987-1313862", price="$19.99",
        provenance="amazon:112-8319987-1313862:Uncharted: Legacy of Thieves Collection",
    )
    apply_catalog_repairs(conn)
    # collection retired; Uncharted 4 kept (receipt merged), Lost Legacy created
    assert conn.execute("SELECT * FROM owned_games WHERE id = ?", (coll,)).fetchone()["is_owned"] == 0
    u4 = conn.execute("SELECT * FROM owned_games WHERE id = ?", (uncharted4,)).fetchone()
    assert u4["is_owned"] == 1
    assert "amazon:112-8319987-1313862:Uncharted: Legacy of Thieves Collection" in (u4["provenance"] or "")
    lost = conn.execute(
        "SELECT * FROM owned_games WHERE igdb_id = 26193 AND is_owned = 1"
    ).fetchone()
    assert lost is not None and lost["title"] == "Uncharted: The Lost Legacy"
    assert lost["platform"] == "playstation 5" and lost["format"] == "physical"
    conn.close()


def test_splits_collection_by_title_when_igdb_unmatched():
    """A collection row whose igdb_id is NULL (never matched) is still split by
    its title, so it can't linger as one card in the UI."""
    conn, _ = _db()
    coll = _seed(
        conn, title="BioShock: The Collection (Full Game)", platform="playstation",
        format="digital", source="psn_receipt", igdb_id=None,  # unmatched
        provenance="psn_receipt:504599545638:0",
    )
    apply_catalog_repairs(conn)
    assert conn.execute("SELECT * FROM owned_games WHERE id = ?", (coll,)).fetchone()["is_owned"] == 0
    ids = {r["igdb_id"] for r in conn.execute("SELECT igdb_id FROM owned_games WHERE is_owned = 1")}
    assert 34293 in ids and 34294 in ids and 538 in ids  # BioShock Remastered / 2 / Infinite
    conn.close()


def test_repairs_are_idempotent():
    conn, _ = _db()
    _seed(conn, title='Master Plunger MPS4 Sink ..." has been canceled', platform="playstation", format="physical", source="amazon", igdb_id=None)
    _seed(conn, title="The Music of Dreams", psn_content_id="UP9000-CUSA18544_00-DREAMSOST0000001", igdb_id=49478)
    _seed(
        conn,
        title="GODS Remastered",
        platform="playstation 4",
        psn_content_id="UP3909-CUSA15513_00-GODSREMASTERED00,UP9000-PPSA08329_00-GOWRAGNAROK00000",
        igdb_id=112875,
        provenance=JAMMED_PROVENANCE,
    )
    _seed(conn, title="Unplugged", platform="playstation 5", psn_content_id="UP3535-PPSA14005_00-0297859396070977", igdb_id=2721)

    first = apply_catalog_repairs(conn)
    second = apply_catalog_repairs(conn)
    assert first.retired and first.split and first.rematched
    # re-run finds nothing left to do
    assert second.retired == []
    assert second.split == []
    assert second.rematched == []
    assert second.skipped == []
    # and no duplicate audit rows
    assert conn.execute("SELECT COUNT(*) n FROM review_queue WHERE source='catalog_repair'").fetchone()["n"] == 5
    conn.close()


def test_splits_super_meat_boy_jam():
    """Super Meat Boy + Super Meat Boy Forever were merged the same way as
    GODS/Ragnarök — each content id must become its own row with the right
    IGDB match."""
    conn, _ = _db()
    jammed = _seed(
        conn,
        title="Super Meat Boy!",
        platform="playstation 4",
        psn_content_id="UP1055-CUSA16602_00-SUPERMEATBOYFORE,UP1055-CUSA02845_00-SUPERMEATBOY0000",
        igdb_id=44129,  # Forever's metadata on the merged row
        provenance=json.dumps(
            [
                "psn_receipt:293544327031:0",
                "psn_api:UP1055-CUSA16602_00-SUPERMEATBOYFORE",
                "psn_api:UP1055-CUSA02845_00-SUPERMEATBOY0000",
                "psn_receipt:786997197463109:0",
            ]
        ),
    )
    apply_catalog_repairs(conn)

    original = conn.execute(
        "SELECT * FROM owned_games WHERE psn_content_id = ? AND is_owned = 1",
        ("UP1055-CUSA02845_00-SUPERMEATBOY0000",),
    ).fetchone()
    forever = conn.execute(
        "SELECT * FROM owned_games WHERE psn_content_id = ? AND is_owned = 1",
        ("UP1055-CUSA16602_00-SUPERMEATBOYFORE",),
    ).fetchone()
    assert original is not None and forever is not None
    assert original["title"] == "Super Meat Boy"
    assert original["igdb_id"] == 885
    assert forever["title"] == "Super Meat Boy Forever"
    assert forever["igdb_id"] == 44129
    assert original["id"] != forever["id"] and original["id"] != jammed or forever["id"] != jammed
    conn.close()


def test_splits_ffvii_remake_jam_with_psplus_class():
    """FINAL FANTASY VII + FINAL FANTASY VII REMAKE were merged; the Remake
    was a PS+ Essential claim ($0 receipt) so its split row must be
    psplus_claimed."""
    conn, _ = _db()
    _seed(
        conn,
        title="FINAL FANTASY VII",
        platform="playstation 4",
        psn_content_id="UP0082-CUSA07211_00-FFVIIREMAKE00000,UP0082-CUSA01875_00-FINALFANTASY7ZZZ",
        igdb_id=207026,
        provenance=json.dumps(
            [
                "psn_receipt:253086790958:0",
                "psn_api:UP0082-CUSA07211_00-FFVIIREMAKE00000",
                "psn_api:UP0082-CUSA01875_00-FINALFANTASY7ZZZ",
            ]
        ),
    )
    apply_catalog_repairs(conn)

    remake = conn.execute(
        "SELECT * FROM owned_games WHERE psn_content_id = ? AND is_owned = 1",
        ("UP0082-CUSA07211_00-FFVIIREMAKE00000",),
    ).fetchone()
    orig = conn.execute(
        "SELECT * FROM owned_games WHERE psn_content_id = ? AND is_owned = 1",
        ("UP0082-CUSA01875_00-FINALFANTASY7ZZZ",),
    ).fetchone()
    assert remake is not None and orig is not None
    assert remake["title"] == "FINAL FANTASY VII REMAKE"
    assert remake["igdb_id"] == 11169
    assert remake["ownership_class"] == "psplus_claimed"
    assert orig["title"] == "FINAL FANTASY VII"
    assert orig["igdb_id"] == 207026
    conn.close()


def test_splits_valkyria_wrong_igdb_jam():
    """Two DIFFERENT games (Valkyria Chronicles 4 + Remastered) were merged
    into one row by a wrong IGDB match (both receipts under igdb 75848).
    The repair splits them back: VC4 keeps the row + API cid, Remastered gets
    a fresh row; both igdb=NULL for the next matcher pass. Idempotent."""
    conn, _ = _db()
    jammed = _seed(
        conn,
        title="Valkyria Chronicles 4",
        platform="playstation 4",
        psn_content_id="UP0177-CUSA10633_00-BFVALKYRIE000100",
        igdb_id=75848,
        provenance=json.dumps(
            ["psn_receipt:507107929603:0", "psn_receipt:411948960000:0",
             "psn_api:UP0177-CUSA10633_00-BFVALKYRIE000100"]
        ),
    )
    report = apply_catalog_repairs(conn)
    assert len(report.split) == 2

    rows = conn.execute("SELECT * FROM owned_games WHERE is_owned = 1 ORDER BY id").fetchall()
    assert len(rows) == 2
    by_title = {r["title"]: r for r in rows}
    vc4 = by_title["Valkyria Chronicles 4"]
    rem = by_title["Valkyria Chronicles Remastered"]
    assert vc4["id"] == jammed
    assert vc4["igdb_id"] is None  # left for the matcher
    assert "psn_receipt:507107929603" not in vc4["provenance"]
    assert "psn_receipt:411948960000" in vc4["provenance"]
    assert vc4["price"] == "$5.99"
    assert rem["id"] != jammed
    assert rem["igdb_id"] is None
    assert rem["provenance"] == json.dumps(["psn_receipt:507107929603:0"])
    assert rem["price"] == "$4.99"

    # idempotent
    apply_catalog_repairs(conn)
    assert len(conn.execute("SELECT * FROM owned_games WHERE is_owned = 1").fetchall()) == 2


def test_merges_gamekey_purchase_as_provenance():
    """A gameflip key purchase the classifier couldn't platform-pin (seller
    title lacks a platform token) merges into the already-owned game as
    provenance; the flag resolves. Title overrides handle seller variants."""
    conn, _ = _db()
    _seed(conn, title="Desperados III (Game)", platform="playstation 4",
          source="psn_receipt", order_number="o-psn-1",
          provenance="psn_receipt:o-psn-1:0")
    _seed(conn, title="Republique", platform="playstation 4",
          provenance="psn_api:UP1234-CUSA00001_00-REPUBLIQUE0000")
    conn.execute(
        """INSERT INTO review_queue(source, order_number, title, reason, payload, status)
           VALUES ('gameflip', 'gf-1', 'Desperados III', 'platform ambiguous',
                   ?, 'open')""",
        (json.dumps({"item_key": "gf-1:0", "price": "$12.99"}),),
    )
    conn.execute(
        """INSERT INTO review_queue(source, order_number, title, reason, payload, status)
           VALUES ('gameflip', 'gf-2', 'Republique Remastered', 'platform ambiguous',
                   ?, 'open')""",
        (json.dumps({"item_key": "gf-2:0", "price": "$8.99"}),),
    )
    conn.execute(
        """INSERT INTO review_queue(source, order_number, title, reason, payload, status)
           VALUES ('woot', 'w-1', 'Blaze Evercade Tomb Raider Collection 1', 'platform ambiguous',
                   ?, 'open')""",
        (json.dumps({"item_key": "w-1:0"}),),
    )
    conn.commit()

    report = apply_catalog_repairs(conn)
    assert len(report.merged) == 2

    rows = {r["title"]: r for r in conn.execute("SELECT * FROM owned_games WHERE is_owned = 1").fetchall()}
    # title cleanup strips the '(Game)' catalog marker
    assert "Desperados III" in rows
    assert "gameflip:gf-1:0" in rows["Desperados III"]["provenance"]
    assert rows["Desperados III"]["price"] == "$12.99"
    assert "gameflip:gf-2:0" in rows["Republique"]["provenance"]
    assert rows["Republique"]["price"] == "$8.99"

    statuses = dict(conn.execute("SELECT title, status FROM review_queue").fetchall())
    assert statuses["Desperados III"] == "resolved"
    assert statuses["Republique Remastered"] == "resolved"
    assert statuses["Blaze Evercade Tomb Raider Collection 1"] == "open"  # skipped (non-PS)

    # idempotent: re-run adds nothing
    before = conn.execute("SELECT provenance FROM owned_games WHERE title = 'Desperados III'").fetchone()[0]
    apply_catalog_repairs(conn)
    after = conn.execute("SELECT provenance FROM owned_games WHERE title = 'Desperados III'").fetchone()[0]
    assert before == after


def test_title_cleanup_strips_platform_suffix_and_more_items():
    """'Diablo IV - PlayStation 5 and 1 more item' is a broken parse (a listing
    summary swallowed into the title): the ' and N more items' tail and the
    ' - PlayStation 5' platform suffix are stripped, leaving the real title —
    the row is NOT retired (it IS a game)."""
    conn, _ = _db()
    pid = _seed(
        conn,
        title="Diablo IV - PlayStation 5 and 1 more item",
        platform="playstation 5",
        format="physical",
        source="gamestop",
        order_number="o-diablo",
        igdb_id=None,
    )
    report = apply_catalog_repairs(conn)
    assert report.retired == []
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (pid,)).fetchone()
    assert row["title"] == "Diablo IV"
    assert row["normalized_title"] == "diablo iv"
    assert row["is_owned"] == 1
    conn.close()


def test_title_cleanup_strips_stray_quote_from_more_items_tail():
    """The Amazon listing summary can carry a stray wrapping quote, e.g.
    'Diablo IV - PlayStation 5\" and 1 more item'. The ' and N more items' tail
    AND the stray quote must be stripped so the platform-suffix strip can still
    reduce it to 'Diablo IV' (memos/7-unmatched Diablo IV corruption)."""
    conn, _ = _db()
    pid = _seed(
        conn,
        title='Diablo IV - PlayStation 5" and 1 more item',
        platform="playstation 5",
        format="physical",
        source="amazon",
        order_number="o-diablo2",
        igdb_id=None,
    )
    report = apply_catalog_repairs(conn)
    assert report.retired == []
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (pid,)).fetchone()
    assert row["title"] == "Diablo IV"
    assert row["normalized_title"] == "diablo iv"
    assert row["is_owned"] == 1
    conn.close()


def test_title_cleanup_strips_retailer_exclusive_and_size_note():
    """'Secret of Mana - PlayStation 4 GameStop Exclusive' is just Secret of
    Mana: the retailer-exclusive marker and platform suffix are display-only
    noise. 'LIMBO (Full Game 128 MB)' drops the size-note parenthetical."""
    conn, _ = _db()
    mana = _seed(
        conn, title="Secret of Mana - PlayStation 4 GameStop Exclusive",
        platform="playstation 4", format="physical", source="gamestop", igdb_id=None,
    )
    limbo = _seed(
        conn, title="LIMBO (Full Game 128 MB)",
        platform="playstation", format="digital", source="psn_receipt", igdb_id=None,
    )
    report = apply_catalog_repairs(conn)
    assert report.retired == []
    assert conn.execute("SELECT title FROM owned_games WHERE id = ?", (mana,)).fetchone()["title"] == "Secret of Mana"
    assert conn.execute("SELECT title FROM owned_games WHERE id = ?", (limbo,)).fetchone()["title"] == "LIMBO"
    conn.close()


def test_title_cleanup_retires_vita_console_hardware():
    """'PlayStation Vita Wi-Fi... and 6 more items' is a console hardware
    listing (not a game) that slipped through the platform gate — after the
    ' and N more items' tail is stripped, the Vita-console model is retired
    as not_a_game:hardware_console."""
    conn, _ = _db()
    pid = _seed(
        conn,
        title="PlayStation Vita Wi-Fi and 6 more items",
        platform="playstation",
        format="physical",
        source="ebay",
        igdb_id=None,
    )
    report = apply_catalog_repairs(conn)
    assert [r["id"] for r in report.retired] == [pid]
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (pid,)).fetchone()
    assert row["is_owned"] == 0
    assert row["retire_reason"] == "not_a_game:hardware_console"
    conn.close()


def test_title_alias_merges_coffeetalk_into_coffee_talk():
    """'CoffeeTalk' (PSN's concatenated spelling, no cover) is the same game
    as 'Coffee Talk' — the alias repair merges the pair into one row that
    inherits the canonical title + IGDB match."""
    conn, _ = _db()
    canon = _seed(conn, title="Coffee Talk", platform="playstation 4",
                  format="digital", source="psn_api", igdb_id=106847,
                  provenance="psn_api:UP1234-CUSA00001_00-COFFEETALK0000")
    alias = _seed(conn, title="CoffeeTalk", platform="playstation 4",
                  format="digital", source="psn_receipt", order_number="o-coffee",
                  igdb_id=None)
    conn.commit()
    report = apply_catalog_repairs(conn)
    assert report.merged, "the alias must be merged"
    rows = conn.execute("SELECT * FROM owned_games WHERE is_owned = 1").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Coffee Talk"
    assert row["normalized_title"] == "coffee talk"
    assert row["igdb_id"] == 106847
    assert row["id"] == canon  # the canonical (psn_api) row is the winner
    alias_row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (alias,)).fetchone()
    assert alias_row["is_owned"] == 0  # retired by the merge
    conn.close()


def test_title_alias_merges_another_fishermans_tale_into_sequel():
    """IGDB lists 'Another Fisherman's Tale' as an alternative name for the
    sequel — a separately-created entry merges into 'A Fisherman's Tale 2'."""
    conn, _ = _db()
    canon = _seed(conn, title="A Fisherman's Tale 2", platform="playstation 5",
                  format="digital", source="psn_api", igdb_id=223344)
    _seed(conn, title="Another Fisherman's Tale", platform="playstation 5",
          format="digital", source="psn_receipt", order_number="o-aft")
    report = apply_catalog_repairs(conn)
    assert report.merged
    rows = conn.execute("SELECT * FROM owned_games WHERE is_owned = 1").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "A Fisherman's Tale 2"
    assert rows[0]["igdb_id"] == 223344
    assert rows[0]["id"] == canon
    conn.close()


def test_generic_split_via_psn_dump_removed_for_unknown_ids():
    """Without the bundled PSN title dump, jammed content ids not in
    SPLIT_OVERRIDES cannot be resolved, so the merged row is skipped (left
    as-is) rather than split."""
    conn, _ = _db()
    _seed(
        conn,
        title="Mystery Merged Game",
        platform="playstation 4",
        psn_content_id="UP9000-CUSA08010_00-DREAMS0000000000,UP9000-CUSA07408_00-00000000GODOFWAR",
        igdb_id=None,
        provenance=json.dumps(
            [
                "psn_api:UP9000-CUSA08010_00-DREAMS0000000000",
                "psn_api:UP9000-CUSA07408_00-00000000GODOFWAR",
            ]
        ),
    )
    apply_catalog_repairs(conn)

    # Neither id resolves to a plan, so the merged row is left unsplit.
    merged = conn.execute(
        "SELECT * FROM owned_games WHERE psn_content_id LIKE '%,%' AND is_owned = 1"
    ).fetchone()
    assert merged is not None
    assert merged["title"] == "Mystery Merged Game"
    conn.close()
