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


def test_apply_unknown_game_404(client):
    res = client.post("/manual/igdb-match", json={"owned_game_id": 9999, "igdb_id": 1})
    assert res.status_code == 404


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
    conn.close()


def get_credential_for_test(conn):
    from mailroom.db import get_credential

    return get_credential(conn, "psn")
