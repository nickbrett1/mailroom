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
