"""External clients used as Dagster resources (injected, never hardcoded).

- msgvault: the email archive API (bodies + attachments).
- igdb: paced IGDB API client (NOT the interactive MCP server) for enrichment.
"""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any

import httpx


class MsgvaultClient:
    """Client over the real msgvault REST API (verified 2026-08-16).

    msgvault serves its archive over REST at `<base_url>` (e.g.
    `http://msgvault:8080` on the NAS; reachable from this devcontainer as
    `http://nas:8080`). The MCP server (:8082) is NOT used: its daemon-client
    adapter drops body_html (memos/ZgBU8cXUQ7PkyRFKHGrZ8e), while the REST
    API returns body + body_html correctly.

    API (see GET /openapi.json):
      - GET /api/v1/messages/filter?sender=..&offset=..&limit=500&sort=date&direction=desc
          -> {count, has_more, offset, limit, messages: [summaries]}
      - GET /api/v1/messages/{id} -> full message with `body` + `body_html`
      - GET /api/v1/stats -> archive totals
    """

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 60.0, client: httpx.Client | None = None, retries: int = 3):
        # msgvault is flaky under load (observed 2026-08-16) — retry transient
        # 5xx / transport errors with backoff.
        self.retries = retries
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    # --- transport ---

    def _get(self, path: str, **params: Any) -> Any:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self._client.get(path, params=params or None)
                if resp.status_code in (429, *range(500, 600)) and attempt < self.retries:
                    time.sleep(2.0 * attempt)  # rate limit / transient — back off
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if attempt == self.retries or (status is not None and status < 500 and status != 429):
                    raise
                last = exc
                time.sleep(2.0 * attempt)
        raise RuntimeError(f"msgvault request failed: {last}")

    # --- public API (matches what the assets expect) ---

    def search_messages(
        self,
        sender: str | None = None,
        subject: str | None = None,
        after: str | None = None,  # cursor: message id
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Newest-first messages from `sender` with an optional client-side
        subject filter, paging until `limit` or the cursor id is reached."""
        cursor: int | None = (
            int(after) if after is not None and (isinstance(after, int) or str(after).isdigit()) else None
        )
        out: list[dict[str, Any]] = []
        page_offset = offset
        while True:
            params: dict[str, Any] = {"sort": "date", "direction": "desc", "offset": page_offset, "limit": 500}
            if sender:
                params["sender"] = sender
            page = self._get("/api/v1/messages/filter", **params)
            msgs = page.get("messages") or []
            for m in msgs:
                if cursor is not None and m.get("id") is not None and int(m["id"]) <= cursor:
                    return out  # reached the cursor; everything after is older
                if subject and subject.lower() not in (m.get("subject") or "").lower():
                    continue
                out.append(m)
                if len(out) >= limit:
                    return out
            if not page.get("has_more") or not msgs:
                return out
            page_offset += len(msgs)

    def get_message(self, message_id: int) -> dict[str, Any]:
        """Fetch one message with its full body + body_html (single GET)."""
        d = self._get(f"/api/v1/messages/{message_id}")
        return {
            "id": d.get("id"),
            "subject": d.get("subject"),
            "from_email": d.get("from_email"),
            "sent_at": d.get("sent_at"),
            "has_attachments": d.get("has_attachments"),
            "body_text": d.get("body") or "",
            "body_html": d.get("body_html") or "",
        }

    def get_stats(self) -> dict[str, Any]:
        return self._get("/api/v1/stats")

    # --- internals ---

    def fetch_webview(self, url: str) -> str:
        """Fetch a 'View as a Web page' URL (Best Buy etc.) with a browser UA.

        click.emailinfo2.bestbuy.com 302-redirects to the real content on
        view.emailinfo2.bestbuy.com, so redirects are followed.
        """
        resp = self._client.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
        resp.raise_for_status()
        return resp.text


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
    client = client or MsgvaultClient("http://msgvault:8080")
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


class PsnAuthError(Exception):
    """PSN auth rejected (refresh token invalid/expired) — degrade, never hard-fail."""


class PsnApiClient:
    """PS App OAuth + Game Library client (undocumented Sony endpoints).

    Auth: a long-lived PS App OAuth **refresh token** (minted once via an
    interactive 2FA login — the flow the PS+ auto-claim bots use) is exchanged
    for a short-lived access token on every run, then the user's title library
    is pulled. Credential lives in the `credentials` table (not .env); auth
    failures raise PsnAuthError so the asset can flip `needs_refresh` and
    retry on the weekly cadence (memos/game-catalog-pipeline §PSN sync).

    Endpoints/field names are the community-documented ones (psnawp-api /
    psn-api projects); verify against a live token the first time.
    """

    OAUTH_TOKEN_URL = "https://ca.account.sony.com/api/authz/v3/oauth/token"
    LEGACY_TOKEN_URL = "https://auth.api.sonyentertainmentnetwork.com/2.0/oauth/token"
    # Current library/entitlements endpoint (verified live 2026-08-17; the old
    # gamelibrary/v1/users/me/titles returns 403 with the PS App scope).
    LIBRARY_URL = "https://m.np.playstation.com/api/entitlement/v2/users/me/internal/entitlements"
    LIBRARY_FIELDS = "titleMeta,gameMeta,conceptMeta,rewardMeta,rewardMeta.retentionPolicy,rewardMeta.rewardMembershipType"
    # Public PS App OAuth client id (appears in the authorize URL; not secret).
    # The client SECRET is NOT stored in the repo — set env PSN_CLIENT_SECRET
    # (GitGuardian alert on c5f3de1; see memos/game-catalog-pipeline §PSN sync).
    CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
    SCOPE = "psn:mobile.v2.core psn:clientapp"
    REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"
    USER_AGENT = "com.sony.snei.np.android.sso.share.oauth.versa.USER_AGENT"

    def __init__(self, refresh_token: str | None = None, client: httpx.Client | None = None, timeout: float = 30.0, client_secret: str | None = None):
        self.refresh_token = refresh_token
        self.client_secret = client_secret
        headers = {"Accept": "application/json", "Accept-Language": "en-US"}
        self._client = client or httpx.Client(timeout=timeout, headers=headers)

    def _access_token(self) -> str:
        resp = self._post_token(self.OAUTH_TOKEN_URL)
        if resp.status_code in (404, 405):  # older accounts/regions: legacy endpoint
            resp = self._post_token(self.LEGACY_TOKEN_URL)
        if resp.status_code in (400, 401):
            raise PsnAuthError(f"PSN refresh token rejected (HTTP {resp.status_code})")
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise PsnAuthError("PSN token response missing access_token")
        return token

    def _post_token(self, url: str) -> httpx.Response:
        headers = {
            **psn_basic_auth_header(self.client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": self.USER_AGENT,
        }
        return self._client.post(
            url,
            headers=headers,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token or "",
                "scope": self.SCOPE,
                "token_format": "jwt",
            },
        )

    def library_titles(self, limit: int = 500) -> list[dict[str, Any]]:
        """Full title-library (entitlements) pull, paginated.

        Response: {totalResults, entitlements: [...]} — each entitlement has
        id/productId (content id), gameMeta/titleMeta (name, packageType),
        rewardMeta (PS+ signal: rewardMembershipType/rewardServiceType).
        """
        token = self._access_token()
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            resp = self._client.get(
                self.LIBRARY_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "entitlementType": "1,2,3,4,5",
                    "fields": self.LIBRARY_FIELDS,
                    "gameMetaPackageType": "PSGD,PS4GD",
                    "limit": min(limit, 500),
                    "offset": offset,
                },
            )
            if resp.status_code in (400, 401):
                raise PsnAuthError(f"PSN library request rejected (HTTP {resp.status_code})")
            resp.raise_for_status()
            data = resp.json()
            items = data.get("entitlements") or []
            out.extend(items)
            total = data.get("totalResults") or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out


def psn_basic_auth_header(client_secret: str | None = None) -> dict[str, str]:
    """Basic auth header for the PS App OAuth client (client_id:secret).

    The secret is NOT committed (GitGuardian alert on c5f3de1) — it comes from
    env PSN_CLIENT_SECRET (or the constructor). Missing secret -> typed
    PsnAuthError so the sync degrades to needs_refresh instead of crashing.
    """
    secret = client_secret or os.environ.get("PSN_CLIENT_SECRET")
    if not secret:
        raise PsnAuthError(
            "PSN_CLIENT_SECRET is not set (PS App OAuth client secret — set it in env or the credentials table)"
        )
    raw = f"{PsnApiClient.CLIENT_ID}:{secret}"
    return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}


_PSPLUS_KEYS = ("psplus", "ps_plus", "ps plus", "playstation plus")


_PSPLUS_KEYS = ("psplus", "ps_plus", "ps plus", "playstation plus")

# Non-game library entries (apps/streaming/services) — never catalogued.
_APP_MARKERS = (
    "netflix", "hulu", "disney+", "disney plus", "youtube", "spotify", "plex",
    "crunchyroll", "amazon prime video", "apple tv", "skype", "twitch",
    "playstation plus", "ps plus", "ps+", "sony pictures core", "d+",
    "playstation stars", "playstation wrap",
)


def _item_is_psplus(item: dict[str, Any]) -> tuple[bool, str | None]:
    """PS+ classification from the entitlement's rewardMeta.

    Verified live 2026-08-17: purchased entries have
    rewardMeta={rewardServiceType: 0, retentionPolicy: 0}; PS+ claims have
    rewardMembershipType="PS_PLUS" / rewardServiceType=2 / retentionPolicy>0.
    TODO: refine claimed-vs-extra (monthly vs catalog) once more samples are in.
    """
    rm = item.get("rewardMeta") or {}
    if rm.get("rewardMembershipType") == "PS_PLUS" or rm.get("rewardServiceType") == 2 or (rm.get("retentionPolicy") or 0) > 0:
        return True, "psplus_claimed"
    return False, None


def psn_library_item_to_game(item: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a PSN entitlement into an owned_games row (digital)."""
    meta = item.get("gameMeta") or item.get("titleMeta") or item
    name = meta.get("name") or item.get("name") or item.get("localizedName")
    content_id = item.get("productId") or item.get("id") or item.get("titleId")
    if not name or not content_id:
        return None
    low = name.lower()
    for marker in _APP_MARKERS:
        if marker in low:
            return None  # apps / subscriptions — keep the catalog to games
    pkg = str(meta.get("type") or meta.get("packageType") or "")
    # Verified live 2026-08-17: PS4GD = PS4 digital; PSGD = PS5 digital
    # (samples: Marvel's Spider-Man Remastered, Maquette, Destruction AllStars).
    if "PS5" in pkg.upper() or "PSGD" in pkg.upper():
        platform_name = "playstation 5"
    elif "PS4" in pkg.upper():
        platform_name = "playstation 4"
    elif "VITA" in pkg.upper() or "PSV" in pkg.upper():
        platform_name = "ps vita"
    elif "PS3" in pkg.upper():
        platform_name = "playstation 3"
    else:
        platform_name = "playstation"
    is_plus, plus_class = _item_is_psplus(item)
    return {
        "title": name,
        "normalized_title": re.sub(r"[^a-z0-9]+", "", name.lower()),
        "platform": platform_name,
        "format": "digital",
        "ownership_class": plus_class if is_plus else "purchased",
        "retailer": None,
        "order_number": None,
        "item_id": None,
        "condition": None,
        "psn_content_id": content_id,
        "igdb_id": None,
        "acquisition_date": None,
        "price": None,
        "source": "ps_plus" if is_plus else "psn_api",
        "source_ref": content_id,
        "status": "owned",
        "is_owned": 1,
        "provenance": f"psn_api:{content_id}",
    }


# --- Dagster resources (thin wrappers over plain clients) ---

from dagster import resource


@resource
def msgvault_resource(context) -> MsgvaultClient:  # type: ignore[no-untyped-def]
    # MSGVAULT_URL points at the msgvault REST API, e.g.
    # "http://msgvault:8080" on the NAS (see MsgvaultClient docstring).
    # The MCP path (:8082) drops body_html — do not use it.
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


@resource
def psn_api_resource(context) -> PsnApiClient:  # type: ignore[no-untyped-def]
    """PSN client built from the refresh token in the credentials table.

    The token lives in the DB (memos/game-catalog-pipeline §PSN sync), not
    .env — a UI refresh writes through mailroom without container edits.
    """
    from mailroom.db import connect, get_credential, init_db

    conn = connect(context.resources.db_url)
    init_db(conn)
    cred = get_credential(conn, "psn") or {}
    conn.close()
    return PsnApiClient(refresh_token=cred.get("token"))
