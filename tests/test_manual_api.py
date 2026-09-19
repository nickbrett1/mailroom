"""Manual-edit API tests: the review workflow (needs-match list, apply match)."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from mailroom.db import connect, init_db, upsert_owned_game


@pytest.fixture()
def client(monkeypatch):
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    upsert_owned_game(
        conn,
        {
            "title": "BELOW",
            "normalized_title": "below",
            "platform": "playstation 4",
            "format": "digital",
            "ownership_class": "purchased",
            "retailer": None,
            "order_number": None,
            "item_id": None,
            "condition": None,
            "psn_content_id": "UP1",
            "igdb_id": None,
            "acquisition_date": None,
            "price": None,
            "source": "psn_api",
            "source_ref": "UP1",
            "status": "owned",
            "is_owned": 1,
            "provenance": "psn_api:UP1",
        },
    )
    conn.close()
    monkeypatch.setenv("MAILROOM_DB_URL", f"sqlite:///{db}")
    from mailroom import manual_api

    return TestClient(manual_api.app)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_needs_match_lists_unmatched(client):
    res = client.get("/manual/needs-match")
    assert res.status_code == 200
    games = res.json()
    assert len(games) == 1
    assert games[0]["title"] == "BELOW"


def test_apply_match_then_no_longer_needed(client):
    game_id = client.get("/manual/needs-match").json()[0]["owned_game_id"]
    res = client.post("/manual/igdb-match", json={"owned_game_id": game_id, "igdb_id": 383834})
    assert res.status_code == 200
    assert res.json()["applied"] is True
    assert client.get("/manual/needs-match").json() == []
    # review queue records the resolution
    from mailroom.db import connect

    conn = connect(os.environ["MAILROOM_DB_URL"])
    row = conn.execute("SELECT reason, status FROM review_queue").fetchone()
    assert row["status"] == "resolved"
    conn.close()


def test_apply_match_updates_catalog_games_row(client):
    """A manual match must propagate onto the materialized `games` row that the
    front-end (catalog_games view) reads, so pshelf's shelf stops showing the
    title as unmatched without waiting for the next materialization."""
    from mailroom.db import connect

    conn = connect(os.environ["MAILROOM_DB_URL"])
    game_id = conn.execute("SELECT id FROM owned_games WHERE title = 'BELOW'").fetchone()["id"]
    # Simulate what the catalog_games materialization does: group BELOW under a
    # canonical `games` row with igdb_id NULL (still unmatched on the shelf).
    cur = conn.execute(
        """INSERT INTO games(title, normalized_title, igdb_id, platform, platforms,
               formats, ownership_classes, num_editions, purchased, editions)
           VALUES ('BELOW', 'below', NULL, 'playstation 4', 'playstation 4',
                   'digital', 'purchased', 1, 1, '[{"id":' || ? || ',"title":"BELOW"}]')""",
        (game_id,),
    )
    gid = cur.lastrowid
    conn.execute("UPDATE owned_games SET game_id = ? WHERE id = ?", (gid, game_id))
    conn.commit()
    conn.close()

    res = client.post("/manual/igdb-match", json={"owned_game_id": game_id, "igdb_id": 383834})
    assert res.status_code == 200
    assert res.json()["applied"] is True

    conn = connect(os.environ["MAILROOM_DB_URL"])
    row = conn.execute("SELECT igdb_id FROM games WHERE id = ?", (gid,)).fetchone()
    assert row["igdb_id"] == 383834
    conn.close()


def test_review_queue_lists_and_resolves_flags(client):
    """Dedup review flags (possible double purchases) are visible and
    adjudicable via the manual API; resolution is recorded on the flag."""
    from mailroom.db import connect

    conn = connect(os.environ["MAILROOM_DB_URL"])
    conn.execute(
        """INSERT INTO review_queue(source, order_number, title, reason, payload, status)
           VALUES ('dedup', '263932697921', 'The Witcher 3: Wild Hunt', 'possible_double_purchase',
                   '{"orders": ["263932697921", "384720390939"], "winner_id": 5}', 'open')"""
    )
    conn.commit()
    conn.close()

    res = client.get("/manual/review-queue")
    assert res.status_code == 200
    flags = res.json()
    assert len(flags) == 1
    assert flags[0]["reason"] == "possible_double_purchase"
    assert flags[0]["status"] == "open"
    assert flags[0]["payload"]["orders"] == ["263932697921", "384720390939"]

    flag_id = flags[0]["id"]
    res = client.post(f"/manual/review-queue/{flag_id}/resolve", json={"decision": "same_purchase", "note": "re-parse"})
    assert res.status_code == 200
    assert res.json()["status"] == "resolved"
    assert res.json()["payload"]["decision"] == "same_purchase"

    assert client.get("/manual/review-queue").json() == []  # open list empty
    assert len(client.get("/manual/review-queue", params={"status": "all"}).json()) == 1
    assert client.post("/manual/review-queue/9999/resolve", json={"decision": "x"}).status_code == 404


def test_psn_cookies_store_and_status(client):
    """Browser session cookies (dict or DevTools array JSON) store for playtime;
    status returns keys only, never values."""
    import json

    from mailroom.db import connect

    # dict form
    res = client.post("/manual/psn-cookies", json={"cookies": {"_exp": "jwt", "_sk": "sk", "_to": "rt"}})
    assert res.status_code == 200
    assert res.json()["stored"] == 3
    assert res.json()["keys"] == ["_exp", "_sk", "_to"]

    conn = connect(os.environ["MAILROOM_DB_URL"])
    cred = conn.execute("SELECT * FROM credentials WHERE source = 'psn_cookies'").fetchone()
    assert cred["status"] == "valid"
    assert "_sk" in cred["token"]
    assert "jwt" in cred["token"]
    conn.close()

    # DevTools array form replaces the set
    res = client.post("/manual/psn-cookies", json={"cookies": [
        {"name": "_exp", "value": "new-jwt", "domain": ".playstation.com"},
        {"name": "_sk", "value": "new-sk", "domain": ".playstation.com"},
        {"name": "some", "value": None},  # ignored
    ]})
    assert res.status_code == 200
    assert res.json()["stored"] == 2

    # status shows keys, not values
    st = client.get("/manual/psn-cookies").json()
    assert st["status"] == "valid"
    assert st["keys"] == ["_exp", "_sk"]
    assert "new-jwt" not in json.dumps(st)  # values never leak

    res = client.post("/manual/psn-cookies", json={"cookies": {}})
    assert res.status_code == 400


def test_apply_unknown_game_404(client):
    res = client.post("/manual/igdb-match", json={"owned_game_id": 9999, "igdb_id": 1})
    assert res.status_code == 404


def test_exclude_removes_from_needs_match(client):
    """Excluding a non-game (artbook/beta) retires the row so it leaves the
    needs-match list and the catalog, and audits to review_queue."""
    from mailroom.db import connect

    game_id = client.get("/manual/needs-match").json()[0]["owned_game_id"]
    res = client.post("/manual/needs-match/exclude", json={"owned_game_id": game_id, "reason": "artbook"})
    assert res.status_code == 200
    assert res.json()["excluded"] is True
    assert res.json()["retire_reason"] == "excluded:artbook"
    assert client.get("/manual/needs-match").json() == []

    conn = connect(os.environ["MAILROOM_DB_URL"])
    row = conn.execute("SELECT is_owned, status, retire_reason FROM owned_games WHERE id = ?", (game_id,)).fetchone()
    assert row["is_owned"] == 0
    assert row["status"] == "retired"
    assert row["retire_reason"] == "excluded:artbook"
    audit = conn.execute("SELECT source, status FROM review_queue WHERE source = 'manual_exclude'").fetchone()
    assert audit is not None and audit["status"] == "resolved"
    conn.close()


def test_rename_owned_game_updates_title(client):
    """Cleaning a raw listing title updates the owned row title + normalized."""
    from mailroom.db import connect

    game_id = client.get("/manual/needs-match").json()[0]["owned_game_id"]
    res = client.post("/manual/owned-game/rename", json={"owned_game_id": game_id, "title": "Wildermyth"})
    assert res.status_code == 200
    assert res.json()["renamed"] is True
    conn = connect(os.environ["MAILROOM_DB_URL"])
    row = conn.execute("SELECT title, normalized_title FROM owned_games WHERE id = ?", (game_id,)).fetchone()
    assert row["title"] == "Wildermyth"
    assert row["normalized_title"] == "wildermyth"
    conn.close()
    # audit recorded
    conn = connect(os.environ["MAILROOM_DB_URL"])
    audit = conn.execute("SELECT source, status FROM review_queue WHERE source = 'manual_rename'").fetchone()
    assert audit is not None and audit["status"] == "resolved"
    conn.close()


def test_rename_unknown_404_and_blank_400(client):
    assert client.post("/manual/owned-game/rename", json={"owned_game_id": 9999, "title": "X"}).status_code == 404
    gid = client.get("/manual/needs-match").json()[0]["owned_game_id"]
    assert client.post("/manual/owned-game/rename", json={"owned_game_id": gid, "title": "  "}).status_code == 400


def test_rename_can_override_platform(client):
    """The rename endpoint can also correct a stored platform (e.g. Vita)."""
    gid = client.get("/manual/needs-match").json()[0]["owned_game_id"]
    res = client.post("/manual/owned-game/rename", json={"owned_game_id": gid, "title": "Rayman Origins", "platform": "ps vita"})
    assert res.status_code == 200
    conn = connect(os.environ["MAILROOM_DB_URL"])
    row = conn.execute("SELECT platform FROM owned_games WHERE id = ?", (gid,)).fetchone()
    assert row["platform"] == "ps vita"
    conn.close()


def test_exclude_unknown_and_already_retired(client):
    assert client.post("/manual/needs-match/exclude", json={"owned_game_id": 9999}).status_code == 404
    game_id = client.get("/manual/needs-match").json()[0]["owned_game_id"]
    assert client.post("/manual/needs-match/exclude", json={"owned_game_id": game_id}).status_code == 200
    # already retired -> 409
    assert client.post("/manual/needs-match/exclude", json={"owned_game_id": game_id}).status_code == 409


def test_igdb_match_reapply_is_idempotent(monkeypatch, tmp_path):
    """Re-applying a match for the same (source, order, title, reason) that was
    already audited must not 500 on the review_queue unique index."""
    db = tmp_path / "t.db"
    from mailroom.db import connect, init_db, upsert_owned_game

    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    base = {
        "title": 'Diablo IV - PlayStation 5"', "normalized_title": 'diablo iv - "',
        "platform": "playstation 5", "format": "physical", "ownership_class": "purchased",
        "retailer": "amazon", "order_number": "111-8148964-1449822", "item_id": None,
        "condition": None, "psn_content_id": None, "acquisition_date": "2025-01-12T22:20:54Z",
        "price": "$25.04", "source": "amazon", "source_ref": "X", "status": "owned", "is_owned": 1,
    }
    upsert_owned_game(conn, {**base, "igdb_id": 125165, "provenance": "amazon:o:Diablo"})
    # an audit row already exists (from the first manual match)
    conn.execute(
        """INSERT INTO review_queue(source, order_number, title, reason, payload, status)
           VALUES ('manual_igdb_match', '111-8148964-1449822', 'Diablo IV - PlayStation 5"',
                   'manual IGDB match applied (igdb 125165)', '', 'resolved')"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MAILROOM_DB_URL", f"sqlite:///{db}")
    from mailroom import manual_api

    c = TestClient(manual_api.app)
    # re-applying to a duplicate row with an already-audited reason must not 500
    res = c.post("/manual/igdb-match", json={"owned_game_id": 1, "igdb_id": 125165})
    assert res.status_code in (200, 409)
    assert res.status_code != 500


def test_psn_credential_status_and_refresh(client, monkeypatch):
    from mailroom.db import connect, set_credential

    conn = connect(os.environ["MAILROOM_DB_URL"])
    set_credential(conn, "psn", token="rt-old", status="needs_refresh", last_error="expired")
    conn.close()

    status = client.get("/manual/psn-credential").json()
    assert status["status"] == "needs_refresh"
    assert status["last_error"] == "expired"

    # stub the NPSSO exchange (avoids live Sony + PSN_CLIENT_SECRET in tests)
    monkeypatch.setattr(
        "scripts.psn_mint_token.exchange_npsso",
        lambda npsso: {"refresh_token": "rt-new-1234567890", "access_token": "at"},
    )
    res = client.post("/manual/psn-credential", json={"npsso": "v3.test"})
    assert res.status_code == 200
    assert res.json()["status"] == "valid"

    conn = connect(os.environ["MAILROOM_DB_URL"])
    cred = get_credential_for_test(conn)
    assert cred["status"] == "valid"
    assert cred["token"] == "rt-new-1234567890"
    # A successful refresh clears the previous error (was stuck via COALESCE).
    assert cred["last_error"] is None
    conn.close()


def get_credential_for_test(conn):
    from mailroom.db import get_credential

    return get_credential(conn, "psn")


def _make_game(igdb_id=None):
    """Insert a canonical `games` row (as the catalog_games asset would) and
    return its id, so the catalog_games view can be read for play_state."""
    from mailroom.db import connect

    conn = connect(os.environ["MAILROOM_DB_URL"])
    cur = conn.execute(
        """INSERT INTO games(title, normalized_title, igdb_id, platform, platforms,
               formats, ownership_classes, num_editions, purchased, editions)
           VALUES ('BELOW', 'below', ?, 'playstation 4', 'playstation 4',
                   'digital', 'purchased', 1, 1, '[]')""",
        (igdb_id,),
    )
    gid = cur.lastrowid
    conn.commit()
    conn.close()
    return gid


def test_play_state_set_by_game_id_and_read_from_view(client):
    """A play state set for a game is exposed on catalog_games.play_state
    (NULL until set — the UI defaults that to 'unplayed')."""
    from mailroom.db import connect

    gid = _make_game()
    conn = connect(os.environ["MAILROOM_DB_URL"])
    before = conn.execute(
        "SELECT play_state FROM catalog_games WHERE game_id = ?", (gid,)
    ).fetchone()
    conn.close()
    assert before["play_state"] is None

    res = client.post("/manual/game/play-state", json={"game_id": gid, "state": "Completed"})
    assert res.status_code == 200
    assert res.json() == {"game_key": "title:below", "play_state": "completed", "applied": True}

    conn = connect(os.environ["MAILROOM_DB_URL"])
    row = conn.execute(
        "SELECT play_state FROM catalog_games WHERE game_id = ?", (gid,)
    ).fetchone()
    conn.close()
    assert row["play_state"] == "completed"


def test_play_state_prefers_igdb_key(client):
    """A matched game is keyed on its stable igdb_id, not its title."""
    from mailroom.db import connect

    gid = _make_game(igdb_id=383834)
    res = client.post(
        "/manual/game/play-state",
        json={"igdb_id": 383834, "normalized_title": "below", "state": "played"},
    )
    assert res.status_code == 200
    assert res.json()["game_key"] == "igdb:383834"
    conn = connect(os.environ["MAILROOM_DB_URL"])
    row = conn.execute(
        "SELECT play_state FROM catalog_games WHERE game_id = ?", (gid,)
    ).fetchone()
    conn.close()
    assert row["play_state"] == "played"


def test_play_state_set_before_match_survives_later_match(client):
    """State set while a game is unmatched (title key) stays visible after the
    game is later matched (igdb key) — the view falls back to the title key."""
    from mailroom.db import connect

    gid = _make_game()  # unmatched
    res = client.post("/manual/game/play-state", json={"game_id": gid, "state": "played"})
    assert res.json()["game_key"] == "title:below"
    conn = connect(os.environ["MAILROOM_DB_URL"])
    conn.execute("UPDATE games SET igdb_id = 383834 WHERE id = ?", (gid,))
    conn.commit()
    row = conn.execute(
        "SELECT play_state FROM catalog_games WHERE game_id = ?", (gid,)
    ).fetchone()
    conn.close()
    assert row["play_state"] == "played"


def test_play_state_changes_are_upserts(client):
    """Setting again for the same game overwrites the prior state (one row)."""
    from mailroom.db import connect

    _make_game(igdb_id=1)
    client.post("/manual/game/play-state", json={"igdb_id": 1, "state": "played"})
    client.post("/manual/game/play-state", json={"igdb_id": 1, "state": "completed"})
    conn = connect(os.environ["MAILROOM_DB_URL"])
    rows = conn.execute("SELECT play_state FROM game_play_state").fetchall()
    conn.close()
    assert [r["play_state"] for r in rows] == ["completed"]


def test_play_state_unknown_game_404_and_validation(client):
    assert client.post("/manual/game/play-state", json={"game_id": 9999, "state": "played"}).status_code == 404
    assert client.post("/manual/game/play-state", json={"state": "beaten", "igdb_id": 1}).status_code == 400
    assert client.post("/manual/game/play-state", json={"state": "played"}).status_code == 400
