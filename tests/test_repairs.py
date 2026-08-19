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
