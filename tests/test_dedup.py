"""owned_games dedup tests (memos/catalog-dedup-fix): merge rules, retire-
not-delete, provenance list, review flags, unique-index guard, upsert on
igdb_id, enrichment-time collapse, manual-match collision."""

from __future__ import annotations

import json
import sqlite3
import tempfile

import pytest
from dagster import build_op_context

from mailroom.db import connect, init_db, upsert_owned_game
from mailroom.verticals.game_catalog import assets
from mailroom.verticals.game_catalog.dedup import dedupe_owned_games


def _db():
    path = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{path}")
    init_db(conn)
    return conn, path


def _db_legacy():
    """Pre-fix store: schema WITHOUT the dedup index (like the live DB the bug
    shipped in) — duplicate rows can be inserted, then dedupe_owned_games
    merges them and init_db creates the guard index."""
    conn, path = _db()
    conn.execute("DROP INDEX IF EXISTS idx_owned_games_dedup")
    conn.commit()
    return conn, path


def _seed(conn, **kw) -> int:
    """Raw INSERT of an owned_games row, bypassing upsert merge logic.

    The live duplicates were created by separate ingest paths inserting rows
    that only later shared an igdb_id — the upsert-on-igdb merge would collapse
    them at seed time, so dedup tests insert directly (the merge behavior of
    upsert_owned_game is covered by test_upsert_merges_on_igdb_id)."""
    source = kw.get("source", "psn_receipt")
    title = kw.get("title", "God of War Ragnarök")
    order = kw.get("order_number")
    source_ref = kw.get("source_ref") or f"{source}:{order or 'ref'}"
    cur = conn.execute(
        """INSERT INTO owned_games
           (title, normalized_title, platform, format, ownership_class, retailer,
            order_number, item_id, condition, psn_content_id, igdb_id,
            acquisition_date, price, source, source_ref, status, is_owned, provenance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            title,
            assets.normalize_title(title),
            kw.get("platform", "playstation 5"),
            kw.get("format", "digital"),
            kw.get("ownership_class", "purchased"),
            kw.get("retailer"),
            order,
            None,
            None,
            kw.get("psn_content_id"),
            kw.get("igdb_id"),
            kw.get("acquisition_date"),
            kw.get("price"),
            source,
            source_ref,
            "owned",
            1,
            kw.get("provenance") or f"{source}:{source_ref}",
        ),
    )
    conn.commit()
    return cur.lastrowid


def _owned(conn):
    return conn.execute("SELECT * FROM owned_games WHERE is_owned = 1").fetchall()


def _all(conn):
    return conn.execute("SELECT * FROM owned_games").fetchall()


# --- merge rules ---


def test_dedupe_merges_multi_source_and_retires_losers():
    """Ragnarök case: psn_api PS5 + psn_receipt + cdkeys, same (igdb, format) —
    one owned row (the API row wins), losers retired (never deleted), and the
    winner's provenance is the JSON list of every source."""
    conn, _ = _db_legacy()
    api = _seed(conn, source="psn_api", psn_content_id="PPSA08329", igdb_id=112875,
                platform="playstation 5", provenance="psn_api:PPSA08329")
    rec = _seed(conn, source="psn_receipt", order_number="252319130093", igdb_id=112875,
                platform="playstation", acquisition_date="2022-11-09",
                provenance="psn_receipt:252319130093:0")
    cd = _seed(conn, source="cdkeys", order_number="0151509331", igdb_id=112875,
               platform="playstation", provenance="cdkeys:0151509331")
    assert len(_owned(conn)) == 3

    report = dedupe_owned_games(conn)
    assert report.groups == 1
    assert report.retired == 2
    assert report.winners[0]["winner_id"] == api

    owned = _owned(conn)
    assert len(owned) == 1
    w = owned[0]
    assert w["id"] == api
    assert w["platform"] == "playstation 5"
    assert w["igdb_id"] == 112875
    assert w["psn_content_id"] == "PPSA08329"
    assert json.loads(w["provenance"]) == ["psn_api:PPSA08329", "psn_receipt:252319130093:0", "cdkeys:0151509331"]

    rows = {r["id"]: r for r in _all(conn)}
    for loser in (rec, cd):
        assert rows[loser]["is_owned"] == 0
        assert rows[loser]["status"] == "retired"
        assert rows[loser]["retire_reason"] == f"dup_merged:game_id={api}"
        assert rows[loser]["provenance"]  # zero data loss — provenance kept
    # idempotent: a second pass has nothing left to merge
    assert dedupe_owned_games(conn).groups == 0
    conn.close()


def test_dedupe_keeps_different_formats_separate():
    """Divinity OS2 case: digital + physical copy of the same game — both kept
    (the memo's open question; resolved: keep both when format differs)."""
    conn, _ = _db_legacy()
    dig = _seed(conn, source="psn_api", psn_content_id="PPSA12345", igdb_id=1821, format="digital",
                platform="playstation 5")
    phy = _seed(conn, source="gamestop", order_number="g1", igdb_id=1821, format="physical",
                platform="playstation 4", retailer="gamestop")
    dedupe_owned_games(conn)
    owned = _owned(conn)
    assert {r["id"] for r in owned} == {dig, phy}
    conn.close()


def test_dedupe_keeps_different_platforms_separate():
    """Ragnarök PS4 + PS5 case: distinct content ids on distinct platforms —
    two rows stay (pipeline principle: keep separate platform rows when
    content IDs differ)."""
    conn, _ = _db_legacy()
    ps4 = _seed(conn, source="psn_api", psn_content_id="CUSA15513", igdb_id=112875,
                platform="playstation 4")
    ps5 = _seed(conn, source="psn_api", psn_content_id="PPSA08329", igdb_id=112875,
                platform="playstation 5")
    dedupe_owned_games(conn)
    owned = _owned(conn)
    assert {r["id"] for r in owned} == {ps4, ps5}
    conn.close()


def test_dedupe_receipt_merges_into_api_across_generic_platform():
    """A receipt row (platform 'playstation' — the parser emits no hint) merges
    into the psn_api row (concrete platform) when they share an igdb match."""
    conn, _ = _db_legacy()
    api = _seed(conn, source="psn_api", psn_content_id="PPSA34970", igdb_id=702,
                platform="playstation 5", provenance="psn_api:PPSA34970")
    _seed(conn, source="psn_receipt", order_number="o-777", igdb_id=702,
          platform="playstation", provenance="psn_receipt:o-777:0")
    dedupe_owned_games(conn)
    owned = _owned(conn)
    assert len(owned) == 1
    assert owned[0]["id"] == api
    assert owned[0]["platform"] == "playstation 5"
    assert json.loads(owned[0]["provenance"]) == ["psn_api:PPSA34970", "psn_receipt:o-777:0"]
    conn.close()


def test_dedupe_merges_psplus_claim_into_purchased_semantics():
    """Same game claimed on PS+ then purchased: one row, ownership_class stays
    'purchased' (survives PS+ cancel), content_id preserved from the claim."""
    conn, _ = _db_legacy()
    plus = _seed(conn, source="ps_plus", psn_content_id="UP9000-CUSA00900_00-BLOODBORNE000000",
                 igdb_id=733, platform="playstation 4", ownership_class="psplus_claimed",
                 provenance="ps_plus:UP9000-CUSA00900_00-BLOODBORNE000000")
    rec = _seed(conn, source="psn_receipt", order_number="bb-1", igdb_id=733,
                platform="playstation", acquisition_date="2021-01-01",
                ownership_class="purchased", provenance="psn_receipt:bb-1:0")
    dedupe_owned_games(conn)
    owned = _owned(conn)
    assert len(owned) == 1
    assert owned[0]["ownership_class"] == "purchased"
    assert owned[0]["psn_content_id"] == "UP9000-CUSA00900_00-BLOODBORNE000000"
    assert owned[0]["id"] in (plus, rec)  # receipt wins the row (purchased), claim's content id kept
    conn.close()


def test_dedupe_flags_possible_double_purchase():
    """Witcher 3 case: two receipts, distinct order numbers — merged (facts
    kept) AND flagged to the review queue for the user to adjudicate."""
    conn, _ = _db_legacy()
    _seed(conn, source="psn_receipt", order_number="263932697921", igdb_id=21477,
          platform="playstation 5", acquisition_date="2021-01-02")
    _seed(conn, source="psn_receipt", order_number="384720390939", igdb_id=21477,
          platform="playstation 5", acquisition_date="2021-01-03")
    report = dedupe_owned_games(conn)
    assert report.retired == 1
    assert len(report.review_flags) == 1
    assert report.review_flags[0]["reason"] == "possible_double_purchase"
    assert set(report.review_flags[0]["orders"]) == {"263932697921", "384720390939"}
    flags = conn.execute(
        "SELECT * FROM review_queue WHERE reason = 'possible_double_purchase'"
    ).fetchall()
    assert len(flags) == 1
    payload = json.loads(flags[0]["payload"])
    assert set(payload["orders"]) == {"263932697921", "384720390939"}
    # idempotent — re-run does not re-flag
    dedupe_owned_games(conn)
    assert conn.execute("SELECT COUNT(*) n FROM review_queue WHERE reason='possible_double_purchase'").fetchone()["n"] == 1
    conn.close()


def test_dedupe_unmatched_by_normalized_title():
    """Un-matched rows (no igdb_id yet) dedupe on (normalized_title, platform,
    format) — a same-purchase re-parse (same order number) merges silently."""
    conn, _ = _db_legacy()
    title = "Hollow Knight Voidheart Edition"
    a = _seed(conn, title=title, source="psn_receipt", order_number="o-same", igdb_id=None,
              platform="playstation", acquisition_date="2021-01-02")
    b = _seed(conn, title=title, source="psn_receipt", order_number="o-same", igdb_id=None,
              platform="playstation", acquisition_date="2021-01-02")
    report = dedupe_owned_games(conn)
    assert report.groups == 1
    assert report.retired == 1
    assert not report.review_flags  # same order = same purchase, no flag
    owned = _owned(conn)
    assert len(owned) == 1
    assert owned[0]["id"] in (a, b)  # earliest receipt wins
    conn.close()


# --- unique-index guard ---


def test_ensure_dedup_index_blocks_duplicate_owned_rows():
    conn, _ = _db()  # init_db created idx_owned_games_dedup (empty store: no-op dedup)
    idx = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_owned_games_dedup'"
    ).fetchone()
    assert idx is not None
    _seed(conn, source="psn_api", psn_content_id="C1", igdb_id=100, platform="playstation 5", format="digital")
    # duplicate (igdb_id, platform, format) owned row -> hard error
    with pytest.raises(sqlite3.IntegrityError):
        _seed(conn, source="psn_receipt", order_number="o1", igdb_id=100, platform="playstation 5", format="digital")
    # same key but different format / different platform / retired row -> allowed
    _seed(conn, source="gamestop", order_number="g1", igdb_id=100, platform="playstation 5", format="physical")
    _seed(conn, source="psn_api", psn_content_id="C2", igdb_id=100, platform="playstation 4", format="digital")
    conn.execute("UPDATE owned_games SET is_owned=0, retire_reason='x' WHERE psn_content_id='C2'")
    conn.commit()
    _seed(conn, source="psn_receipt", order_number="o2", igdb_id=100, platform="playstation 4", format="digital")
    assert len(_owned(conn)) == 3
    conn.close()


# --- upsert / enrichment-time collapse ---


def test_upsert_merges_on_igdb_id():
    """Re-ingesting a matched game (igdb_id set) collapses into the existing
    row instead of inserting — even when the title variant differs."""
    conn, _ = _db_legacy()
    first = _seed(conn, title="Elden Ring", source="psn_api", psn_content_id="UP1", igdb_id=119133,
                  platform="playstation 5", provenance="psn_api:UP1")
    again = upsert_owned_game(
        conn,
        {
            "title": "ELDEN RING™ - PS5",
            "normalized_title": assets.normalize_title("ELDEN RING™ - PS5"),
            "platform": "playstation 5",
            "format": "digital",
            "ownership_class": "purchased",
            "retailer": None,
            "order_number": None,
            "item_id": None,
            "condition": None,
            "psn_content_id": None,
            "igdb_id": 119133,
            "acquisition_date": None,
            "price": None,
            "source": "cdkeys",
            "source_ref": "cdkeys:o9",
            "status": "owned",
            "is_owned": 1,
            "provenance": "cdkeys:o9",
        },
    )
    assert again == first
    assert conn.execute("SELECT COUNT(*) n FROM owned_games").fetchone()["n"] == 1
    # generic platform also matches the igdb key
    third = upsert_owned_game(
        conn,
        {
            "title": "Elden Ring",
            "normalized_title": "elden ring",
            "platform": "playstation",
            "format": "digital",
            "ownership_class": "purchased",
            "retailer": None,
            "order_number": None,
            "item_id": None,
            "condition": None,
            "psn_content_id": None,
            "igdb_id": 119133,
            "acquisition_date": None,
            "price": None,
            "source": "psn_receipt",
            "source_ref": "psn_receipt:o10",
            "status": "owned",
            "is_owned": 1,
            "provenance": "psn_receipt:o10",
        },
    )
    assert third == first
    assert conn.execute("SELECT COUNT(*) n FROM owned_games").fetchone()["n"] == 1
    prov = json.loads(conn.execute("SELECT provenance FROM owned_games").fetchone()["provenance"])
    assert "psn_api:UP1" in prov and "cdkeys:o9" in prov and "psn_receipt:o10" in prov
    conn.close()


def test_igdb_matches_collapses_duplicate_at_enrichment():
    """Two unmatched receipts of the same game: when the second gains the same
    IGDB match it collapses into the first (index guard + merge) instead of
    erroring or leaving a duplicate."""
    conn, path = _db()
    title = "The Witcher 3: Wild Hunt - Complete Edition"
    a = _seed(conn, title=title, source="psn_receipt", order_number="263932697921",
              platform="playstation 5", acquisition_date="2021-01-02")
    b = _seed(conn, title=title, source="psn_receipt", order_number="384720390939",
              platform="playstation 5", acquisition_date="2021-01-03")

    class _Stub:
        def game_by_external_psn_uid(self, uid):
            return None

        def search_game(self, name):
            return [{"id": 21477, "name": "The Witcher 3: Wild Hunt", "platforms": [167]}]

    assets.igdb_matches(build_op_context(resources={"db_url": f"sqlite:///{path}", "igdb": _Stub()}))
    conn = connect(f"sqlite:///{path}")
    owned = _owned(conn)
    assert len(owned) == 1
    assert owned[0]["igdb_id"] == 21477
    assert owned[0]["id"] == a  # earliest receipt wins
    retired = conn.execute("SELECT * FROM owned_games WHERE is_owned = 0").fetchone()
    assert retired["id"] == b
    assert retired["retire_reason"] == f"dup_merged:game_id={a}"
    conn.close()


def test_manual_igdb_match_merges_on_collision(monkeypatch):
    """POST /manual/igdb-match for a game whose (igdb_id, platform, format) is
    already owned merges instead of creating a duplicate."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    existing = _seed(conn, title="BELOW", source="psn_api", psn_content_id="UP1", igdb_id=383834,
                     platform="playstation 4", provenance="psn_api:UP1")
    dup = _seed(conn, title="BELOW (Retail)", source="gamestop", order_number="g1", igdb_id=None,
                platform="playstation 4", retailer="gamestop", provenance="gamestop:g1")
    conn.close()
    monkeypatch.setenv("MAILROOM_DB_URL", f"sqlite:///{db}")

    from fastapi.testclient import TestClient

    from mailroom import manual_api

    res = TestClient(manual_api.app).post("/manual/igdb-match", json={"owned_game_id": dup, "igdb_id": 383834})
    assert res.status_code == 200
    body = res.json()
    assert body["merged"] is True
    assert body["owned_game_id"] == existing  # the psn_api row wins the merge
    assert body["retired_owned_game_id"] == dup

    conn = connect(f"sqlite:///{db}")
    owned = _owned(conn)
    assert len(owned) == 1
    assert owned[0]["id"] == existing
    assert owned[0]["igdb_id"] == 383834
    rq = conn.execute("SELECT source, reason, status FROM review_queue ORDER BY id").fetchall()
    assert any(r["source"] == "manual_igdb_match" and "merged into owned game" in r["reason"] and r["status"] == "resolved" for r in rq)
    conn.close()


def test_reingest_updates_winner_not_retired_loser():
    """Hot Wheels case end-to-end: a $0 PSN receipt was merged as 'purchased'
    (wrong) and its receipt row retired. Re-running the fixed owned_games asset
    reclassifies the receipt as psplus_claimed AND the upsert must update the
    OWNED winner row — not the retired loser — flipping the winner to
    psplus_claimed."""
    conn, path = _db_legacy()
    winner = _seed(conn, title="HOT WHEELS UNLEASHED™ (Game)", source="psn_api",
                   psn_content_id="UP1981-PPSA02324_00-HWUPSPLUS0000000", igdb_id=144072,
                   platform="playstation 4", ownership_class="psplus_claimed",
                   provenance="psn_api:UP1981-PPSA02324_00-HWUPSPLUS0000000")
    _seed(conn, title="HOT WHEELS UNLEASHED™ (Game)", source="psn_receipt",
          order_number="369813850135", igdb_id=144072, platform="playstation 4",
          ownership_class="purchased", provenance="psn_receipt:369813850135:0")
    # previous (buggy) dedup: merged, winner row ended up 'purchased' (min rank
    # across the wrongly-purchased receipt) — exactly the live state
    dedupe_owned_games(conn)
    assert len(_owned(conn)) == 1
    assert _owned(conn)[0]["ownership_class"] == "purchased"
    assert _owned(conn)[0]["id"] == winner

    # parsed + classified receipt row for the same purchase ($0 = PS+ claim)
    conn.execute(
        """INSERT INTO parsed_purchases(source, order_number, item_key, purchased_at, title, platform, price)
           VALUES ('psn_receipt', '369813850135', '369813850135:0', '10/04/2022', 'HOT WHEELS UNLEASHED™ (Game)', 'playstation 4', '$0.00')"""
    )
    conn.execute(
        """INSERT INTO classified_game_items(source, order_number, item_key, title, platform, classification, reason)
           VALUES ('psn_receipt', '369813850135', '369813850135:0', 'HOT WHEELS UNLEASHED™ (Game)', 'playstation 4', 'playstation_game', 'PSN receipt (platform implicit)')"""
    )
    conn.commit()
    conn.close()

    assets.owned_games(build_op_context(resources={"db_url": f"sqlite:///{path}"}))
    conn = connect(f"sqlite:///{path}")
    owned = _owned(conn)
    assert len(owned) == 1
    assert owned[0]["id"] == winner  # updated the winner, not the retired loser
    assert owned[0]["ownership_class"] == "psplus_claimed"
    assert owned[0]["price"] == "$0.00"
    conn.close()


def test_dedupe_asset_runs_idempotent():
    conn, path = _db()
    _seed(conn, source="psn_api", psn_content_id="PPSA08329", igdb_id=112875, platform="playstation 5")
    _seed(conn, source="psn_receipt", order_number="252319130093", igdb_id=112875, platform="playstation")
    conn.close()
    ctx = build_op_context(resources={"db_url": f"sqlite:///{path}"})
    assets.dedupe_owned_games(ctx)
    assets.dedupe_owned_games(ctx)
    conn = connect(f"sqlite:///{path}")
    assert len(_owned(conn)) == 1
    conn.close()
