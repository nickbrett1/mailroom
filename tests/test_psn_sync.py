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
            return httpx.Response(302, headers={"location": f"{mint.PsnApiClient.REDIRECT_URI}?code=AUTHCODE1"})
        assert "oauth/token" in request.url.path
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

    import os

    os.environ["PSN_CLIENT_SECRET"] = "test-secret"
    try:
        transport = httpx.MockTransport(handler)
        tokens = mint.exchange_npsso("abc123", client=httpx.Client(transport=transport))
        assert tokens["refresh_token"] == "rt"
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
    def __init__(self, titles, error=None):
        self.titles = titles
        self.error = error

    def library_titles(self):
        if self.error:
            raise self.error
        return self.titles


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


def test_psn_api_owned_merges_and_marks_valid():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    set_credential(conn, "psn", token="rt")
    ctx = _ctx(f"sqlite:///{db}", _StubPsn(LIB_ITEMS[:3]))
    assets.psn_api_owned(ctx)
    cred = get_credential(conn, "psn")
    assert cred["status"] == "valid"
    assert cred["last_success"]
    rows = {r["psn_content_id"]: r for r in conn.execute("SELECT * FROM owned_games").fetchall()}
    assert len(rows) == 3  # add-on excluded
    assert rows["UP9000-CUSA07408_00-00000000GODOFWAR"]["ownership_class"] == "purchased"
    assert rows["UP9000-CUSA00900_00-BLOODBORNE000000"]["ownership_class"] == "psplus_claimed"
    assert rows["UP9000-CUSA00900_00-BLOODBORNE000000"]["source"] == "ps_plus"
    assert rows["UP9000-CUSA12345_00-SOMEEXTRAGAME"]["ownership_class"] == "psplus_claimed"
    # idempotent: re-run adds nothing new
    assets.psn_api_owned(ctx)
    assert conn.execute("SELECT COUNT(*) n FROM owned_games").fetchone()["n"] == 3
    conn.close()
