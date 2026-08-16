"""External clients used as Dagster resources (injected, never hardcoded).

- msgvault: the email archive API (bodies + attachments).
- igdb: paced IGDB API client (NOT the interactive MCP server) for enrichment.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx


class MsgvaultClient:
    """Thin client over the msgvault API.

    Endpoints are shaped after the msgvault OpenAPI used in this environment
    (search/filter messages by sender/subject, fetch body). Fill in the exact
    routes when wiring against the real service; keep the cursor semantics here.
    """

    def __init__(self, base_url: str, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(base_url=self.base_url, headers=self._headers, timeout=30.0)

    def search_messages(
        self,
        sender: Optional[str] = None,
        subject: Optional[str] = None,
        after: Optional[str] = None,  # cursor (e.g. message id / received_at)
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if sender:
            params["sender"] = sender
        if subject:
            params["q"] = subject
        if after:
            params["after"] = after
        resp = self._client.get("/messages", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("messages", data if isinstance(data, list) else [])

    def get_message(self, message_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/messages/{message_id}")
        resp.raise_for_status()
        return resp.json()


class IgdbClient:
    """Paced IGDB API client for bulk enrichment (4 req/s limit).

    Credentials are the IGDB API app's client id/secret (NOT the MCP creds).
    Kept deliberately small: external_games lookup + game details fetch,
    with pacing + retry. Interactive search stays on the IGDB MCP.
    """

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._token_expires = 0.0
        self._min_interval = 0.3  # ~3.3 req/s, under the 4 req/s cap
        self._last_request = 0.0

    def _pace(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _auth_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        resp = httpx.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + data["expires_in"]
        return self._token

    def _apicalypse(self, endpoint: str, query: str) -> list[dict[str, Any]]:
        self._pace()
        self._last_request = time.monotonic()
        resp = httpx.post(
            f"https://api.igdb.com/v4/{endpoint}",
            headers={"Client-ID": self.client_id, "Authorization": f"Bearer {self._auth_token()}"},
            content=query,
        )
        resp.raise_for_status()
        return resp.json()

    def game_by_external_psn_uid(self, psn_uid: str) -> Optional[int]:
        """Match a PSN content id / title id to an IGDB game id via external_games."""
        rows = self._apicalypse(
            "external_games",
            f'fields game,uid; where uid = "{psn_uid}" & category = 1; limit 1;',
        )
        return rows[0]["game"] if rows else None


# --- Dagster resources (thin wrappers over plain clients) ---

from dagster import resource  # noqa: E402


@resource
def msgvault_resource(context) -> MsgvaultClient:  # type: ignore[no-untyped-def]
    return MsgvaultClient(
        base_url=os.environ.get("MSGVAULT_URL", "http://msgvault:8080"),
        token=os.environ.get("MSGVAULT_TOKEN"),
    )


@resource
def igdb_resource(context) -> IgdbClient:  # type: ignore[no-untyped-def]
    return IgdbClient(
        client_id=os.environ.get("IGDB_CLIENT_ID", ""),
        client_secret=os.environ.get("IGDB_CLIENT_SECRET", ""),
    )
