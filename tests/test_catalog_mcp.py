"""Catalog MCP server tests: read-only tools over the catalog_views model."""

from __future__ import annotations

import json
import tempfile

import pytest

from mailroom.db import connect, init_db, upsert_owned_game


def _seed(db_path: str) -> None:
    conn = connect(f"sqlite:///{db_path}")
    init_db(conn)
    for i, (title, platform, fmt, cls, igdb) in enumerate(
        [
            ("God of War", "playstation 4", "digital", "purchased", 1942),
            ("Cyberpunk 2077", "playstation 5", "physical", "purchased", 41494),
            ("Some PS+ Game", "playstation 5", "digital", "psplus_claimed", None),
        ]
    ):
        upsert_owned_game(
            conn,
            {
                "title": title,
                "normalized_title": title.lower().replace(" ", ""),
                "platform": platform,
                "format": fmt,
                "ownership_class": cls,
                "retailer": None if fmt == "digital" else "amazon",
                "order_number": None,
                "item_id": None,
                "condition": None,
                "psn_content_id": None,
                "igdb_id": igdb,
                "acquisition_date": None,
                "price": None,
                "source": "psn_api",
                "source_ref": f"s{i}",
                "status": "owned",
                "is_owned": 1,
                "provenance": f"psn_api:s{i}",
            },
        )
    if True:  # metadata for the two matched games
        for gid, name in ((1942, "God of War"), (41494, "Cyberpunk 2077")):
            conn.execute(
                "INSERT OR REPLACE INTO game_metadata(igdb_id, payload) VALUES (?, ?)",
                (gid, json.dumps({"id": gid, "name": name, "total_rating": 90.0})),
            )
    conn.commit()
    conn.close()


@pytest.fixture()
def catalog_env(monkeypatch):
    db = tempfile.mktemp(suffix=".db")
    _seed(db)
    monkeypatch.setenv("CATALOG_DB", db)
    from catalog_mcp import server

    return server


def test_search_catalog(catalog_env):
    srv = catalog_env
    results = srv.search_catalog("god")
    assert len(results) == 1
    assert results[0]["title"] == "God of War"
    assert results[0]["igdb_payload"]["total_rating"] == 90.0


def test_search_filters(catalog_env):
    srv = catalog_env
    assert len(srv.search_catalog("", format="physical")) == 1
    assert len(srv.search_catalog("", ownership_class="psplus_claimed")) == 1
    assert len(srv.search_catalog("", platform="playstation 4")) == 1


def test_get_game(catalog_env):
    srv = catalog_env
    row = srv.search_catalog("cyberpunk")[0]
    g = srv.get_game(row["game_id"])
    assert g["title"] == "Cyberpunk 2077"
    assert g["format"] == "physical"


def test_catalog_stats_ps_plus_split(catalog_env):
    srv = catalog_env
    stats = srv.catalog_stats()
    assert stats["total"] == 3
    assert stats["by_format"] == {"digital": 2, "physical": 1}
    assert stats["keep_if_ps_plus_cancelled"] == 2
    assert stats["lost_if_ps_plus_cancelled"] == 1


def test_recently_added(catalog_env):
    srv = catalog_env
    assert len(srv.recently_added()) >= 3


def _seed_metadata(db_path):
    """Add ratings/genres/release to the seeded store for the query tools."""
    import json

    from mailroom.db import connect

    conn = connect(f"sqlite:///{db_path}")
    payloads = {
        1942: {"id": 1942, "name": "God of War", "total_rating": 93.0, "aggregated_rating": 94.0, "first_release_date": 1522454400, "cover": {"url": "//c1.jpg"}, "genres": [{"name": "Action"}, {"name": "Adventure"}]},
        41494: {"id": 41494, "name": "Cyberpunk 2077", "total_rating": 86.0, "first_release_date": 1606694400, "genres": [{"name": "Role-playing (RPG)"}]},
    }
    for gid, payload in payloads.items():
        conn.execute("INSERT OR REPLACE INTO game_metadata(igdb_id, payload) VALUES (?, ?)", (gid, json.dumps(payload)))
    conn.commit()
    conn.close()


def test_psvr2_filter(catalog_env):
    """is_psvr2 is a category flag (IGDB platform 390) filterable on the view —
    PSVR2 games keep platform 'playstation 5'."""
    import os

    from mailroom.db import connect

    conn = connect(f"sqlite:///{os.environ['CATALOG_DB']}")
    conn.execute(
        "INSERT OR REPLACE INTO game_metadata(igdb_id, payload) VALUES (?, ?)",
        (41494, json.dumps({"id": 41494, "name": "Cyberpunk 2077",
                            "platforms": [{"id": 167, "name": "PlayStation 5"},
                                          {"id": 390, "name": "PlayStation VR2"}]})),
    )
    conn.commit()
    conn.close()
    srv = catalog_env
    # flagged + filterable
    row = srv.search_catalog("cyberpunk")[0]
    assert row["is_psvr2"] == 1
    assert len(srv.search_catalog("", is_psvr2=True)) == 1
    assert len(srv.search_catalog("", is_psvr2=False)) == 2
    assert [g["title"] for g in srv.catalog_list(is_psvr2=True)] == ["Cyberpunk 2077"]


def test_top_rated_and_catalog_list(catalog_env):
    _seed_metadata(catalog_env.__name__ and __import__("os").environ["CATALOG_DB"])
    srv = catalog_env
    top = srv.top_rated()
    assert top[0]["title"] == "God of War"  # 93.0 first
    assert top[0]["rating"] == 93.0
    assert top[1]["title"] == "Cyberpunk 2077"
    # the extracted columns are exposed
    assert top[0]["cover_url"] and top[0]["genres"] == "Action, Adventure"
    # sort + filters
    rpg = srv.catalog_list(genre="Role-playing", sort="rating")
    assert [g["title"] for g in rpg] == ["Cyberpunk 2077"]
    recent = srv.catalog_list(sort="recent")
    assert recent[0]["title"] == "Cyberpunk 2077"  # 2020 > 2018
    byg = srv.by_genre("Action")
    assert [g["title"] for g in byg] == ["God of War"]
