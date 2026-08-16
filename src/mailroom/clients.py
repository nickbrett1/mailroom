"""External clients used as Dagster resources (injected, never hardcoded).

- msgvault: the email archive API (bodies + attachments).
- igdb: paced IGDB API client (NOT the interactive MCP server) for enrichment.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx


class MsgvaultClient:
    """Client over the real msgvault endpoint (verified 2026-08-16).

    msgvault exposes its archive through a streamable-HTTP MCP server at
    `<base_url>/mcp` (e.g. `http://msgvault:8082/mcp` on the NAS; reachable
    from this devcontainer as `http://nas:8082/mcp`). There is no separate
    REST OpenAPI on that port — the MCP JSON-RPC interface is the API.

    This client speaks MCP JSON-RPC directly (initialize + tools/call) so the
    pipeline does not depend on a running MCP client. It implements the
    semantics the assets need:
      - search_messages: sender/subject filters + cursor paging (newest-first,
        skips ids <= cursor) via `list_messages` / `search_metadata`.
      - get_message: fetches the FULL body by paging the API's body slice
        window (max_chars/offset/has_more) and returns `body_text`/`body_html`.
      - get_stats / get_attachment: thin wrappers for later use.
    """

    _MAX_BODY_CHARS = 4000  # msgvault caps max_chars at 4000
    _MAX_BODY_BYTES = 2_000_000  # hard safety cap on paged body size

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 60.0, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    # --- MCP transport ---

    def _call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke an MCP tool and unwrap its first text content (parsed as JSON)."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}
        if arguments:
            payload["params"]["arguments"] = arguments
        resp = self._client.post(self.base_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"msgvault MCP error: {data['error']}")
        result = data.get("result", {})
        content = result.get("content") or []
        for item in content:
            if item.get("type") == "text":
                text = item.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return result.get("structuredContent") or result

    # --- public API (matches what the assets expect) ---

    def search_messages(
        self,
        sender: str | None = None,
        subject: str | None = None,
        after: str | None = None,  # cursor: message id (int/str digits) or ISO date
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Find messages newest-first, optionally filtered by sender/subject.

        Cursor semantics: when `after` is a message id, paging stops as soon as
        a message with id <= cursor is seen (ids are monotonic, results are
        newest-first), so each run fetches only messages newer than the cursor.
        """
        if subject is not None:
            query = f'from:{sender} ' if sender else ""
            query += f'subject:"{subject}"'
            return self._page_cursor("search_metadata", {"query": query}, after=after, limit=limit, offset=offset)

        args: dict[str, Any] = {}
        if sender:
            args["from"] = sender
        return self._page_cursor("list_messages", args, after=after, limit=limit, offset=offset)

    def get_message(self, message_id: int, body_format: str = "auto") -> dict[str, Any]:
        """Fetch one message with its FULL body (pages the slice window)."""
        msg: dict[str, Any] = {}
        text_chunks: list[str] = []
        html_chunks: list[str] = []
        offset = 0
        last_format = "auto"
        while True:
            d = self._call_tool(
                "get_message",
                {"id": message_id, "body_format": body_format, "offset": offset, "max_chars": self._MAX_BODY_CHARS},
            )
            if not msg:
                msg = {k: v for k, v in d.items() if k not in ("body_text", "body_html", "body_returned", "offset", "has_more")}
            last_format = d.get("body_format") or last_format
            text_chunks.append(d.get("body_text") or "")
            html_chunks.append(d.get("body_html") or "")
            returned = d.get("body_returned") or 0
            if not d.get("has_more") or returned <= 0:
                break
            offset += returned
            if offset > self._MAX_BODY_BYTES:
                break
        msg["body_text"] = "".join(text_chunks)
        msg["body_html"] = "".join(html_chunks)
        msg["body_format"] = last_format
        return msg

    def search_message_bodies(self, query: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """Keyword search over message bodies (supports from:/subject: filters)."""
        page = self._call_tool("search_message_bodies", {"query": query, "limit": limit, "offset": offset})
        return page.get("data") or []

    def get_stats(self) -> dict[str, Any]:
        return self._call_tool("get_stats")

    def get_attachment(self, attachment_id: int) -> Any:
        return self._call_tool("get_attachment", {"attachment_id": attachment_id})

    # --- internals ---

    def fetch_webview(self, url: str) -> str:
        """Fetch a 'View as a Web page' URL (Best Buy etc.) with a browser UA.

        click.emailinfo2.bestbuy.com 302-redirects to the real content on
        view.emailinfo2.bestbuy.com, so redirects are followed.
        """
        resp = self._client.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    def _page_cursor(
        self,
        tool: str,
        args: dict[str, Any],
        after: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """Page newest-first through a list/search tool, stopping at the cursor."""
        cursor: int | None = None
        date_after: str | None = None
        if after is not None:
            if isinstance(after, int) or str(after).isdigit():
                cursor = int(after)
            else:
                date_after = after
        if date_after:
            args = {**args, "after": date_after}

        out: list[dict[str, Any]] = []
        remaining = limit
        page_offset = offset
        while remaining > 0:
            page = self._call_tool(tool, {**args, "offset": page_offset, "limit": min(remaining, 100)})
            data = page.get("data") or []
            has_more = bool(page.get("has_more", False))
            if not data:
                break
            for m in data:
                if cursor is not None and int(m["id"]) <= cursor:
                    return out  # reached the cursor; everything after is older
                out.append(m)
                remaining -= 1
                if remaining <= 0:
                    return out
            if not has_more:
                break
            page_offset += len(data)
        return out


_WEBVIEW_CLICK_RE = re.compile(r"https?://click\.emailinfo2?\.bestbuy\.com/[^\s\r\n]+")
_WEBVIEW_VIEW_RE = re.compile(r"https?://view\.emailinfo2?\.bestbuy\.com/[^\s\r\n]+")


def recover_webview_html(body_text: str, client: MsgvaultClient | None = None) -> str | None:
    """Best Buy bodies archive as a stub ('View as a Web page' + click link).

    Fetch the web-view URL to recover the real HTML content (items, prices,
    order number). Returns the fetched HTML or None when no recoverable URL.
    """
    # Prefer 'click.' links (they 302 to the real content); bare 'view.' links
    # require a token and return a maintenance page.
    m = _WEBVIEW_CLICK_RE.search(body_text) or _WEBVIEW_VIEW_RE.search(body_text)
    if not m:
        return None
    client = client or MsgvaultClient("http://msgvault:8082/mcp")
    try:
        return client.fetch_webview(m.group(0))
    except httpx.HTTPError:
        return None


class IgdbClient:
    """Paced IGDB API client for bulk enrichment (4 req/s limit).

    Credentials are the IGDB API app's client id/secret (NOT the MCP creds).
    Kept deliberately small: external_games lookup + game details fetch,
    with pacing + retry. Interactive search stays on the IGDB MCP.
    """

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
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

    def game_by_external_psn_uid(self, psn_uid: str) -> int | None:
        """Match a PSN content id / title id to an IGDB game id via external_games."""
        rows = self._apicalypse(
            "external_games",
            f'fields game,uid; where uid = "{psn_uid}" & category = 1; limit 1;',
        )
        return rows[0]["game"] if rows else None


# --- Dagster resources (thin wrappers over plain clients) ---

from dagster import resource


@resource
def msgvault_resource(context) -> MsgvaultClient:  # type: ignore[no-untyped-def]
    # MSGVAULT_URL points at the msgvault MCP endpoint, e.g.
    # "http://msgvault:8082/mcp" on the NAS (see MsgvaultClient docstring).
    return MsgvaultClient(
        base_url=os.environ.get("MSGVAULT_URL", "http://msgvault:8082/mcp"),
        token=os.environ.get("MSGVAULT_TOKEN"),
    )


@resource
def igdb_resource(context) -> IgdbClient:  # type: ignore[no-untyped-def]
    return IgdbClient(
        client_id=os.environ.get("IGDB_CLIENT_ID", ""),
        client_secret=os.environ.get("IGDB_CLIENT_SECRET", ""),
    )
