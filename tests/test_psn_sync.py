"""PSN sync tests: client (OAuth/library, mocked), normalization, credentials
lifecycle, and the psn_api_owned asset merge (memos/game-catalog-pipeline
§PSN sync redesign)."""

from __future__ import annotations

import tempfile

import httpx
import pytest
from dagster import build_op_context

from mailroom.clients import (
    PsnApiClient,
    PsnAuthError,
    psn_library_item_to_game,
)
from mailroom.db import connect, get_credential, init_db, set_credential
from mailroom.verticals.game_catalog import assets

REFRESH_BODY = {"access_token": "jwt-token-123", "expires_in": 3600, "token_type": "bearer"}

LIB_ITEMS = [
    {
        "id": "UP9000-CUSA07408_00-00000000GODOFWAR",
        "productId": "UP9000-CUSA07408_00-00000000GODOFWAR",
        "titleId": "CUSA07408",
        "gameMeta": {"name": "God of War", "type": "PS4GD"},
        "rewardMeta": {"rewardServiceType": 0, "retentionPolicy": 0},
    },
    {
        "id": "UP9000-CUSA00900_00-BLOODBORNE000000",
        "productId": "UP9000-CUSA00900_00-BLOODBORNE000000",
        "gameMeta": {"name": "Bloodborne", "type": "PS4GD"},
        "rewardMeta": {"rewardMembershipType": "PS_PLUS", "rewardServiceType": 2, "retentionPolicy": 4},
    },
    {
        "id": "UP9000-CUSA12345_00-SOMEEXTRAGAME",
        "productId": "UP9000-CUSA12345_00-SOMEEXTRAGAME",
        "gameMeta": {"name": "Some Extra Catalog Game", "type": "PSGD"},
        "rewardMeta": {"rewardMembershipType": "PS_PLUS", "rewardServiceType": 2, "retentionPolicy": 4},
    },
    {
        "id": "UP0001-CUSA00001_00-DISNEYPLUS",
        "productId": "UP0001-CUSA00001_00-DISNEYPLUS",
        "gameMeta": {"name": "Disney+", "type": "PS4GD"},
        "rewardMeta": {"rewardServiceType": 0, "retentionPolicy": 0},
    },
]


def _psn_client(handler) -> PsnApiClient:
    transport = httpx.MockTransport(handler)
    return PsnApiClient(refresh_token="rt-123", client_secret="test-secret", client=httpx.Client(transport=transport))


def test_library_titles_exchanges_token_and_paginates():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth/token" in request.url.path:
            form = httpx.QueryParams(request.content.decode())
            assert form["grant_type"] == "refresh_token"
            assert form["refresh_token"] == "rt-123"
            return httpx.Response(200, json=REFRESH_BODY)
        assert request.url.path.endswith("/api/entitlement/v2/users/me/internal/entitlements")
        assert request.headers["Authorization"] == "Bearer jwt-token-123"
        calls["n"] += 1
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json={"entitlements": [LIB_ITEMS[0]], "totalResults": 2, "start": 0})
        return httpx.Response(200, json={"entitlements": [LIB_ITEMS[1]], "totalResults": 2, "start": 1})

    titles = _psn_client(handler).library_titles()
    assert len(titles) == 2
    assert calls["n"] == 2


def test_auth_error_typed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(PsnAuthError):
        _psn_client(handler).library_titles()


def test_missing_client_secret_degrades():
    from mailroom.clients import psn_basic_auth_header

    with pytest.raises(PsnAuthError):
        psn_basic_auth_header()  # no env secret, none passed


def test_exchange_npsso_flow():
    """NPSSO -> authorize Location code -> token exchange (no browser redirect)."""
    import importlib.util
    from pathlib import Path

    _spec = importlib.util.spec_from_file_location("psn_mint_token", Path(__file__).parent.parent / "scripts" / "psn_mint_token.py")
    mint = importlib.util.module_from_spec(_spec)
    assert _spec.loader is not None
    _spec.loader.exec_module(mint)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth/authorize" in request.url.path:
            assert "npsso=abc123" in request.headers.get("Cookie", "")
            assert request.url.params["response_type"] == "code"
            return httpx.Response(
                302,
                headers={
                    "location": f"{mint.PsnApiClient.REDIRECT_URI}?code=AUTHCODE1",
                    "set-cookie": "_exp=eyJ0; _to=abc; _sid=xyz",
                },
            )
        assert "oauth/token" in request.url.path
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

    import os

    os.environ["PSN_CLIENT_SECRET"] = "test-secret"
    try:
        transport = httpx.MockTransport(handler)
        tokens = mint.exchange_npsso("abc123", client=httpx.Client(transport=transport))
        assert tokens["refresh_token"] == "rt"
        assert tokens["cookies"] and tokens["cookies"].get("_exp") == "eyJ0"
    finally:
        os.environ.pop("PSN_CLIENT_SECRET", None)


def test_library_item_normalization():
    game = psn_library_item_to_game(LIB_ITEMS[0])
    assert game["psn_content_id"] == "UP9000-CUSA07408_00-00000000GODOFWAR"
    assert game["platform"] == "playstation 4"
    assert game["ownership_class"] == "purchased"
    assert game["source"] == "psn_api"

    monthly = psn_library_item_to_game(LIB_ITEMS[1])
    assert monthly["ownership_class"] == "psplus_claimed"
    assert monthly["source"] == "ps_plus"

    extra = psn_library_item_to_game(LIB_ITEMS[2])
    assert extra["ownership_class"] == "psplus_claimed"  # claimed-vs-extra: TODO
    assert extra["platform"] == "playstation 5"

    assert psn_library_item_to_game(LIB_ITEMS[3]) is None  # app skipped
    # content entitlements (demos/OST/artbooks) excluded, but word-bounded:
    from mailroom.clients import psn_library_item_to_game as _to_game

    assert _to_game({"id": "UP1", "productId": "UP1", "gameMeta": {"name": "FINAL FANTASY XVI DEMO", "type": "PS4GD"}, "rewardMeta": {"rewardServiceType": 0}}) is None
    assert _to_game({"id": "UP2", "productId": "UP2", "gameMeta": {"name": "Horizon Zero Dawn Artbook", "type": "PS4GD"}, "rewardMeta": {"rewardServiceType": 0}}) is None
    assert _to_game({"id": "UP3", "productId": "UP3", "gameMeta": {"name": "Demon's Souls", "type": "PSGD"}, "rewardMeta": {"rewardServiceType": 0}}) is not None  # 'demo' must NOT hit Demon's
    assert _to_game({"id": "UP4", "productId": "UP4", "gameMeta": {"name": "Theme Hospital", "type": "PS4GD"}, "rewardMeta": {"rewardServiceType": 0}}) is not None  # not an artbook/theme


def test_connect_sets_busy_timeout_for_concurrent_writers():
    """Two Dagster runs landing at once must not fail with 'database is
    locked' — WAL allows one writer, but busy_timeout makes the second wait
    (seen live 2026-08-19 after the restart backlog)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    conn.close()


def test_credential_helpers_and_owned_games_migration():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    set_credential(conn, "psn", token="rt-999", status="valid")
    cred = get_credential(conn, "psn")
    assert cred["token"] == "rt-999"
    assert cred["status"] == "valid"
    set_credential(conn, "psn", status="needs_refresh", last_error="boom")
    cred = get_credential(conn, "psn")
    assert cred["status"] == "needs_refresh"
    assert cred["token"] == "rt-999"  # unchanged
    assert cred["last_error"] == "boom"
    # ownership_class column present (fresh + migration path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(owned_games)").fetchall()}
    assert "ownership_class" in cols
    conn.close()


class _StubPsn:
    def __init__(self, titles, error=None, trophies=None, games=None):
        self.titles = titles
        self.error = error
        self.trophies = trophies if trophies is not None else []
        self.games = games if games is not None else []

    def library_titles(self):
        if self.error:
            raise self.error
        return self.titles

    def trophy_titles(self):
        if self.error:
            raise self.error
        return self.trophies

    def game_list(self, cookies):
        if self.error:
            raise self.error
        return self.games


def _ctx(db_url, stub):
    return build_op_context(resources={"db_url": db_url, "psn_api": stub})


def test_psn_api_owned_no_token_degrades():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    ctx = _ctx(f"sqlite:///{db}", _StubPsn([]))
    assets.psn_api_owned(ctx)
    assert get_credential(conn, "psn")["status"] == "needs_refresh"
    conn.close()


def test_psn_api_owned_auth_error_degrades():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    set_credential(conn, "psn", token="rt")
    ctx = _ctx(f"sqlite:///{db}", _StubPsn([], error=PsnAuthError("rejected")))
    assets.psn_api_owned(ctx)
    cred = get_credential(conn, "psn")
    assert cred["status"] == "needs_refresh"
    assert "rejected" in (cred["last_error"] or "")
    conn.close()


TROPHY_ITEMS = [
    {
        "npCommunicationId": "NPWR11111_00",  # same NPWR id as GAME_LIST_ITEMS[0]
        "trophyTitleName": "God of War",
        "trophyTitlePlatform": "PS4",
        # no playDuration — the trophy API does not return playtime (verified)
        "earnedTrophies": {"bronze": 24, "silver": 7, "gold": 5, "platinum": 1, "total": 37},
        "definedTrophies": {"bronze": 24, "silver": 7, "gold": 5, "platinum": 1, "total": 37},
        "progress": 100,
        "lastUpdateDate": "2023-01-15T00:00:00Z",
    },
    {
        "npCommunicationId": "UP9000-CUSA12345_00-SOMEEXTRAGAME",
        "trophyTitleName": "Some Extra Catalog Game",
        "trophyTitlePlatform": "PS5",
        "earnedTrophies": {"bronze": 0, "silver": 0, "gold": 0, "platinum": 0, "total": 0},
        "definedTrophies": {"bronze": 10, "silver": 5, "gold": 3, "platinum": 1, "total": 19},
        "progress": 0,
    },
]


def test_trophy_titles_exchanges_token_and_paginates():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json=REFRESH_BODY)
        if "trophyTitles" in request.url.path:
            calls["n"] += 1
            offset = int(request.url.params["offset"])
            page = TROPHY_ITEMS[offset : offset + 1]
            return httpx.Response(200, json={"totalResults": 2, "trophyTitles": page})
        return httpx.Response(404)

    client = _psn_client(handler)
    titles = client.trophy_titles()
    assert calls["n"] == 2  # paginated 1 at a time
    assert len(titles) == 2
    assert titles[0]["npCommunicationId"] == "NPWR11111_00"


def test_trophy_item_normalization():
    from mailroom.clients import iso8601_duration_minutes, psn_trophy_item_to_stats

    assert iso8601_duration_minutes("PT24H15M") == 24 * 60 + 15
    assert iso8601_duration_minutes("PT0M") == 0
    assert iso8601_duration_minutes(None) is None
    assert iso8601_duration_minutes("") is None
    assert iso8601_duration_minutes("garbage") is None

    s = psn_trophy_item_to_stats(TROPHY_ITEMS[0])
    assert s["trophy_title_id"] == "NPWR11111_00"
    assert s["normalized_title"] == "god of war"
    assert s["playtime_minutes"] is None  # trophy API has no playtime
    assert s["trophies_earned"] == 37
    assert s["trophies_defined"] == 37
    assert s["progress"] == 100
    assert psn_trophy_item_to_stats({"no": "id"}) is None
    # medal-only shape (no 'total' key) — the live trophy API shape
    medal = {"earnedTrophies": {"bronze": 3, "silver": 1, "gold": 0, "platinum": 0},
             "definedTrophies": {"bronze": 10, "silver": 5, "gold": 3, "platinum": 1},
             "npCommunicationId": "NPWR1_00", "trophyTitleName": "Test™"}
    s2 = psn_trophy_item_to_stats(medal)
    assert s2["trophies_earned"] == 4
    assert s2["trophies_defined"] == 19
    assert s2["normalized_title"] == "test"  # ™ stripped


GAME_LIST_ITEMS = [
    {
        "titleId": "NPWR11111_00",
        "name": "God of War",
        "playDuration": "PT47H20M",
        "category": "full_game",
    },
    {
        "titleId": "NPWR22222_00",
        "name": "ELDEN RING",
        "playDuration": "PT0M",
        "category": "full_game",
    },
]


def test_game_list_cookie_auth_and_parse():
    from mailroom.clients import psn_game_list_item_to_stats

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json=REFRESH_BODY)
        assert "_exp=eyJ0; _to=abc" in request.headers.get("Cookie", "")
        if "gameList" in request.url.path:
            return httpx.Response(200, json={"games": GAME_LIST_ITEMS})
        return httpx.Response(404)

    client = _psn_client(handler)
    games = client.game_list({"_exp": "eyJ0", "_to": "abc"})
    assert len(games) == 2
    s = psn_game_list_item_to_stats(games[0])
    assert s["trophy_title_id"] == "NPWR11111_00"
    assert s["playtime_minutes"] == 47 * 60 + 20
    assert s["normalized_title"] == "god of war"
    assert psn_game_list_item_to_stats({"no": "key"}) is None


def test_psn_playtime_asset_upserts_stats_and_view_shows_hours():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    from mailroom.db import set_credential, upsert_owned_game

    set_credential(conn, "psn", token="rt-123")
    set_credential(conn, "psn_cookies", token='{"_exp": "eyJ0"}', status="valid")
    upsert_owned_game(
        conn,
        {
            "title": "God of War", "normalized_title": "god of war",
            "platform": "playstation 4", "format": "digital",
            "ownership_class": "purchased", "retailer": None,
            "order_number": None, "item_id": None, "condition": None,
            "psn_content_id": "UP9000-CUSA07408_00-00000000GODOFWAR",
            "igdb_id": 19560, "acquisition_date": None, "price": None,
            "source": "psn_api", "source_ref": "UP9000-CUSA07408_00-00000000GODOFWAR",
            "status": "owned", "is_owned": 1,
            "provenance": "psn_api:UP9000-CUSA07408_00-00000000GODOFWAR",
        },
    )
    conn.close()

    # trophy pass (playtime None) + gameList pass (playtime from cookies)
    assets.psn_playtime(_ctx(f"sqlite:///{db}", _StubPsn([], trophies=TROPHY_ITEMS, games=GAME_LIST_ITEMS)))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM game_stats ORDER BY trophy_title_id").fetchone()
    assert row["trophy_title_id"] == "NPWR11111_00"
    assert row["normalized_title"] == "god of war"
    assert row["playtime_minutes"] == 47 * 60 + 20  # gameList playtime wins
    assert row["trophies_earned"] == 37
    assert row["progress"] == 100

    v = conn.execute("SELECT title, hours_played, playtime_minutes, trophy_progress FROM catalog_views").fetchone()
    assert v["title"] == "God of War"
    assert v["hours_played"] == 47.3  # 2840 / 60 = 47.33 -> ROUND(...,1)
    assert v["playtime_minutes"] == 2840
    assert v["trophy_progress"] == 100

    # re-run is an upsert (idempotent, count stays 2 + the no-trophy gameList game)
    assets.psn_playtime(_ctx(f"sqlite:///{db}", _StubPsn([], trophies=TROPHY_ITEMS, games=GAME_LIST_ITEMS)))
    assert conn.execute("SELECT COUNT(*) n FROM game_stats").fetchone()["n"] == 3
    conn.close()


def test_psn_playtime_falls_back_to_trophies_without_cookies():
    """No psn_cookies credential -> gameList skipped, trophy stats still land."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    from mailroom.db import set_credential

    set_credential(conn, "psn", token="rt-123")
    conn.close()
    assets.psn_playtime(_ctx(f"sqlite:///{db}", _StubPsn([], trophies=TROPHY_ITEMS)))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM game_stats").fetchone()
    assert row["trophies_earned"] == 37
    assert row["playtime_minutes"] is None
    conn.close()


def test_psn_api_owned_merges_and_marks_valid():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    set_credential(conn, "psn", token="rt")
    # Seed a RECEIPT-style digital row for the extra game (generic platform,
    # punctuation-normalized title like the receipt parser produces) — the API
    # item must merge into it (backfill content_id + concrete platform) rather
    # than creating a duplicate.
    from mailroom.db import upsert_owned_game

    upsert_owned_game(
        conn,
        {
            "title": "Some Extra Catalog Game",
            "normalized_title": "some extra catalog game",
            "platform": "playstation",
            "format": "digital",
            "ownership_class": "purchased",
            "retailer": None,
            "order_number": None,
            "item_id": None,
            "condition": None,
            "psn_content_id": None,
            "igdb_id": None,
            "acquisition_date": None,
            "price": None,
            "source": "psn_receipt",
            "source_ref": "order:1",
            "status": "owned",
            "is_owned": 1,
            "provenance": "psn_receipt:order:1",
        },
    )
    ctx = _ctx(f"sqlite:///{db}", _StubPsn(LIB_ITEMS[:3]))
    assets.psn_api_owned(ctx)
    cred = get_credential(conn, "psn")
    assert cred["status"] == "valid"
    assert cred["last_success"]
    rows = {r["psn_content_id"]: r for r in conn.execute("SELECT * FROM owned_games").fetchall()}
    assert len(rows) == 3  # extra game merged into the seeded receipt row — no duplicate
    assert rows["UP9000-CUSA07408_00-00000000GODOFWAR"]["ownership_class"] == "purchased"
    assert rows["UP9000-CUSA00900_00-BLOODBORNE000000"]["ownership_class"] == "psplus_claimed"
    assert rows["UP9000-CUSA00900_00-BLOODBORNE000000"]["source"] == "ps_plus"
    merged = rows["UP9000-CUSA12345_00-SOMEEXTRAGAME"]
    assert merged["ownership_class"] == "psplus_claimed"
    assert merged["platform"] == "playstation 5"  # concrete platform backfilled
    # idempotent: re-run adds nothing new
    assets.psn_api_owned(ctx)
    assert conn.execute("SELECT COUNT(*) n FROM owned_games").fetchone()["n"] == 3
    conn.close()


def test_psn_library_item_to_game_filters_ost_and_artbook():
    """Dreams OST ('The Music of Dreams', DREAMSOST…) and Dreams Art Book
    ('The Art of Dreams', DREAMSARTBOOK…) are add-ons, not games — the PSN
    API library lists them but they must never enter owned_games."""
    ost = {
        "id": "UP9000-CUSA18544_00-DREAMSOST0000001",
        "productId": "UP9000-CUSA18544_00-DREAMSOST0000001",
        "gameMeta": {"name": "The Music of Dreams", "type": "PS4GD"},
        "rewardMeta": {"rewardServiceType": 0, "retentionPolicy": 0},
    }
    artbook = {
        "id": "UP9000-CUSA18543_00-DREAMSARTBOOK001",
        "productId": "UP9000-CUSA18543_00-DREAMSARTBOOK001",
        "gameMeta": {"name": "The Art of Dreams", "type": "PS4GD"},
        "rewardMeta": {"rewardServiceType": 0, "retentionPolicy": 0},
    }
    assert psn_library_item_to_game(ost) is None
    assert psn_library_item_to_game(artbook) is None


def test_psn_library_item_to_game_keeps_ost_sounding_games():
    """Word-bounded 'ost' must not filter real games (e.g. 'Ghost of a
    Tale'... and a title literally containing 'Lost' stays a game)."""
    game = {
        "id": "UP0001-CUSA00002_00-LOSTGAME0000001",
        "productId": "UP0001-CUSA00002_00-LOSTGAME0000001",
        "gameMeta": {"name": "Lost", "type": "PS4GD"},
        "rewardMeta": {"rewardServiceType": 0, "retentionPolicy": 0},
    }
    g = psn_library_item_to_game(game)
    assert g is not None
    assert g["title"] == "Lost"
