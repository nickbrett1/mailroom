"""catalog_views/catalog_games expose the IGDB canonical page (memos/igdb-page-link).

The front-end links each game to its IGDB page. IGDB pages are slug-based
(https://www.igdb.com/games/<slug>); a numeric igdb_id is not a URL. The slug and
the full public URL both live in the raw IGDB payload already cached in
game_metadata, so the read models surface them as igdb_slug/igdb_url.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from mailroom.db import connect, init_db, upsert_owned_game


def _seed_owned(conn, *, title: str, igdb_id: int | None, ref: str) -> None:
    upsert_owned_game(
        conn,
        {
            "title": title,
            "normalized_title": title.lower().replace(" ", ""),
            "platform": "playstation 5",
            "format": "digital",
            "ownership_class": "purchased",
            "retailer": None,
            "order_number": None,
            "item_id": None,
            "condition": None,
            "psn_content_id": None,
            "igdb_id": igdb_id,
            "acquisition_date": None,
            "price": None,
            "source": "psn_api",
            "source_ref": ref,
            "status": "owned",
            "is_owned": 1,
            "provenance": f"psn_api:{ref}",
        },
    )


@pytest.fixture()
def db_url() -> str:
    return f"sqlite:///{tempfile.mktemp(suffix='.db')}"


def _metadata(conn, igdb_id: int, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO game_metadata(igdb_id, payload) VALUES (?, ?)",
        (igdb_id, json.dumps(payload)),
    )


def _seed_games(conn, *, title: str, igdb_id: int | None) -> None:
    """Simulate the catalog_games materialization's canonical `games` row.

    catalog_views reads owned_games directly, but catalog_games reads the
    materialized `games` table (rebuilt DELETE+INSERT by the Dagster asset), so
    it needs a row here to join against game_metadata.
    """
    conn.execute(
        """INSERT INTO games(title, normalized_title, igdb_id, platform, platforms,
               formats, ownership_classes, num_editions, purchased, editions)
           VALUES (?, ?, ?, 'playstation 5', 'playstation 5', 'digital',
                   'purchased', 1, 1, '[]')""",
        (title, title.lower().replace(" ", ""), igdb_id),
    )


def _view_row(conn, view: str, igdb_id: int) -> dict:
    return conn.execute(
        f"SELECT igdb_slug, igdb_url FROM {view} WHERE igdb_id = ?", (igdb_id,)
    ).fetchone()


def test_views_expose_slug_and_url_from_payload(db_url):
    conn = connect(db_url)
    init_db(conn)
    _seed_owned(conn, title="Portal 2", igdb_id=1020, ref="s1")
    _seed_games(conn, title="Portal 2", igdb_id=1020)
    _metadata(
        conn,
        1020,
        {
            "id": 1020,
            "name": "Portal 2",
            "slug": "portal-2",
            "url": "https://www.igdb.com/games/portal-2",
        },
    )
    conn.commit()

    for view in ("catalog_views", "catalog_games"):
        row = _view_row(conn, view, 1020)
        assert row["igdb_slug"] == "portal-2", view
        assert row["igdb_url"] == "https://www.igdb.com/games/portal-2", view


def test_url_is_built_from_slug_when_payload_lacks_url(db_url):
    """Payloads fetched before `url` was requested still yield a working link."""
    conn = connect(db_url)
    init_db(conn)
    _seed_owned(conn, title="Bloodborne", igdb_id=7334, ref="s2")
    _seed_games(conn, title="Bloodborne", igdb_id=7334)
    _metadata(conn, 7334, {"id": 7334, "name": "Bloodborne", "slug": "bloodborne"})
    conn.commit()

    for view in ("catalog_views", "catalog_games"):
        row = _view_row(conn, view, 7334)
        assert row["igdb_slug"] == "bloodborne", view
        assert row["igdb_url"] == "https://www.igdb.com/games/bloodborne", view


def test_unmatched_game_has_no_link(db_url):
    """No IGDB match -> no slug/url, so the UI hides the link."""
    conn = connect(db_url)
    init_db(conn)
    _seed_owned(conn, title="Unmatched Game", igdb_id=None, ref="s3")
    _seed_games(conn, title="Unmatched Game", igdb_id=None)
    conn.commit()

    for view in ("catalog_views", "catalog_games"):
        rows = conn.execute(
            f"SELECT igdb_slug, igdb_url FROM {view} WHERE title = 'Unmatched Game'"
        ).fetchall()
        assert len(rows) == 1, view
        assert rows[0]["igdb_slug"] is None, view
        assert rows[0]["igdb_url"] is None, view
