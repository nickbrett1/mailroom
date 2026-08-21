"""Cover-caching tests: URL helpers + the game_covers asset (fetch to the
filesystem cache, idempotent skip, re-fetch on cover change, 404 handling,
and the catalog_views cover_local read-model column)."""

from __future__ import annotations

import json
import tempfile

import pytest
from dagster import build_op_context

from mailroom.db import connect, init_db, upsert_owned_game
from mailroom.verticals.game_catalog import assets


class _StubIgdb:
    def __init__(self, images: dict[str, bytes | None]):
        self.images = images
        self.calls: list[str] = []

    def fetch_image(self, url: str) -> bytes | None:
        self.calls.append(url)
        for key, data in self.images.items():
            if key in url:
                return data
        return None


def _seed_metadata(conn) -> None:
    """One matched game with a cover URL, one with no cover."""
    payloads = {
        1942: {"id": 1942, "name": "God of War", "cover": {"url": "//images.igdb.com/igdb/image/upload/t_cover_big/co1x9c.jpg"}},
        41494: {"id": 41494, "name": "Cyberpunk 2077"},
    }
    for gid, payload in payloads.items():
        conn.execute("INSERT OR REPLACE INTO game_metadata(igdb_id, payload) VALUES (?, ?)", (gid, json.dumps(payload)))
    conn.commit()


def test_cover_url_helpers():
    url = "//images.igdb.com/igdb/image/upload/t_cover_big/co1x9c.jpg"
    assert assets.igdb_cover_image_id(url) == "co1x9c"
    assert assets.igdb_cover_big2x_url(url) == "https://images.igdb.com/igdb/image/upload/t_cover_big_2x/co1x9c.jpg"
    # No cover / malformed -> None
    assert assets.igdb_cover_image_id(None) is None
    assert assets.igdb_cover_image_id("not-a-cover") is None
    assert assets.igdb_cover_big2x_url("junk") is None


def test_game_covers_fetches_writes_and_is_idempotent(tmp_path):
    covers_dir = tmp_path / "covers"
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_metadata(conn)
    conn.close()

    stub = _StubIgdb({"co1x9c.jpg": b"cover-bytes"})
    ctx = build_op_context(resources={"db_url": f"sqlite:///{db}", "igdb": stub})
    assets.game_covers(ctx)

    # File written and DB row recorded with the serve-root path.
    img = covers_dir / "co1x9c.jpg"
    assert img.exists()
    assert img.read_bytes() == b"cover-bytes"
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM game_covers WHERE igdb_id = 1942").fetchone()
    assert row["image_id"] == "co1x9c"
    assert row["local_path"] == "/covers/co1x9c.jpg"
    assert row["status"] == "ok"
    # Game with no cover is untouched.
    assert conn.execute("SELECT COUNT(*) n FROM game_covers").fetchone()["n"] == 1
    conn.close()

    # Second run: file exists + URL unchanged -> skipped (no extra fetches).
    before = len(stub.calls)
    assets.game_covers(ctx)
    assert len(stub.calls) == before  # no re-fetch
    assert img.exists()


def test_game_covers_refetches_on_cover_change(tmp_path, monkeypatch):
    covers_dir = tmp_path / "covers"
    monkeypatch.setenv("MAILROOM_COVERS_DIR", str(covers_dir))
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_metadata(conn)
    conn.close()

    stub = _StubIgdb({"co1x9c.jpg": b"old"})
    ctx = build_op_context(resources={"db_url": f"sqlite:///{db}", "igdb": stub})
    assets.game_covers(ctx)
    assert (covers_dir / "co1x9c.jpg").read_bytes() == b"old"

    # Cover URL changes to a new image -> re-fetch writes the new file.
    conn = connect(f"sqlite:///{db}")
    conn.execute(
        "UPDATE game_metadata SET payload = ? WHERE igdb_id = 1942",
        (json.dumps({"id": 1942, "name": "God of War", "cover": {"url": "//images.igdb.com/igdb/image/upload/t_cover_big/co2abc.jpg"}}),),
    )
    conn.commit()
    conn.close()
    stub.images = {"co2abc.jpg": b"new"}
    assets.game_covers(ctx)
    assert (covers_dir / "co2abc.jpg").read_bytes() == b"new"
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT image_id, local_path, cover_url FROM game_covers WHERE igdb_id = 1942").fetchone()
    assert row["image_id"] == "co2abc"
    assert row["local_path"] == "/covers/co2abc.jpg"
    assert "co2abc" in row["cover_url"]
    conn.close()


def test_game_covers_404_leaves_file_and_marks_missing(tmp_path):
    covers_dir = tmp_path / "covers"
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_metadata(conn)
    conn.close()

    stub = _StubIgdb({})  # returns None (404) for everything
    ctx = build_op_context(resources={"db_url": f"sqlite:///{db}", "igdb": stub})
    assets.game_covers(ctx)

    # No file written; row recorded as missing with NULL local_path.
    assert not (covers_dir / "co1x9c.jpg").exists()
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM game_covers WHERE igdb_id = 1942").fetchone()
    assert row["status"] == "missing"
    assert row["local_path"] is None
    conn.close()


def test_catalog_views_exposes_cover_local(tmp_path):
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_metadata(conn)
    upsert_owned_game(
        conn,
        {
            "title": "God of War", "normalized_title": "god of war",
            "platform": "playstation 4", "format": "digital", "ownership_class": "purchased",
            "retailer": None, "order_number": None, "item_id": None, "condition": None,
            "psn_content_id": None, "igdb_id": 1942, "acquisition_date": None, "price": None,
            "source": "psn_api", "source_ref": "s1", "status": "owned", "is_owned": 1,
            "provenance": "psn_api:s1",
        },
    )
    # Simulate a cached cover (the game_covers asset writes this row).
    conn.execute(
        "INSERT INTO game_covers(igdb_id, image_id, cover_url, local_path, status) VALUES (1942, 'co1x9c', ?, '/covers/co1x9c.jpg', 'ok')",
        ("//images.igdb.com/igdb/image/upload/t_cover_big/co1x9c.jpg",),
    )
    conn.commit()
    row = conn.execute("SELECT cover_url, cover_local FROM catalog_views WHERE game_id = ?", (1,)).fetchone()
    assert row["cover_url"]
    assert row["cover_local"] == "/covers/co1x9c.jpg"
    conn.close()


@pytest.fixture(autouse=True)
def _isolate_covers_dir(tmp_path, monkeypatch):
    # Never write to the real /data/covers during tests.
    monkeypatch.setenv("MAILROOM_COVERS_DIR", str(tmp_path / "covers"))
    yield
