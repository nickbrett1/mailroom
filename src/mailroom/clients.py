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
        """Match a PSN content id / title id to an IGDB game id via external_games.

        NOTE (verified 2026-08-17): IGDB's external_games has ~no PlayStation
        Network entries (category 18 is empty; 0/30 sampled content-ids match),
        so this is a rare fallback, not the primary digital path.
        """
        rows = self._apicalypse(
            "external_games",
            f'fields game,uid; where uid = "{psn_uid}" & category = 18; limit 1;',
        )
        return rows[0]["game"] if rows else None

    # IGDB platform ids: 7 PS1 · 8 PS2 · 9 PS3 · 38 PSP · 46 Vita · 48 PS4 · 167 PS5
    PS_PLATFORM_IDS = (7, 8, 9, 38, 46, 48, 167)

    def search_game(self, name: str) -> list[dict[str, Any]]:
        """Search IGDB games by name, preferring PlayStation platforms.

        IGDB's `search` clause is unreliable for short/common words ('below',
        'islanders', 'invisible' -> 0 results), so when it comes back empty we
        fall back to a case-insensitive name-substring match
        (`where name ~ *"term"*`). The exact-name/gate picker in igdb_matches
        then chooses the right entry (a `& category = 0` filter breaks the
        regex query — returns 0 rows — so DLC shadowing is handled by the
        picker's exact-name preference, not here).
        """
        safe = re.sub(r'["\\\n\r\t]', "", name or "")

        def _ps_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            ps = [r for r in rows if any(p in (r.get("platforms") or []) for p in self.PS_PLATFORM_IDS)]
            return ps or rows

        # Single-token terms go to the EXACT-NAME query first: 'Endling' must
        # resolve to 'Endling' (187734), not the search clause's
        # 'Endling: Extinction is Forever' (which the single-token guard then
        # rejects), and 'Journey' must not land on 'The Sims 4: Journey to
        # Batuu'. `name ~ "term"` (case-insensitive equality, no stars) returns
        # every game whose name IS the term — the right answer for a short
        # title whenever one exists.
        single = len(re.findall(r"[a-z0-9]+", safe)) == 1
        if single:
            rows_eq = self._apicalypse(
                "games",
                f'fields id,name,platforms,first_release_date; where name ~ "{safe}"; limit 10;',
            )
            if rows_eq:
                return rows_eq
        rows = self._apicalypse(
            "games",
            f'fields id,name,platforms,first_release_date; search "{safe}"; limit 10;',
        )
        # 'search' can return a single fuzzy hit with no name overlap
        # ('islanders' -> 'Escape from Monkey Island') — only accept it when a
        # result contains the term as a whole WORD, otherwise fall through to
        # the reliable queries below ('dreams' -> 'The House in Fata Morgana:
        # ... of Dreams' has 'dreams' only inside a longer word/phrase, so the
        # exact-name query gets a chance to surface the actual 'Dreams').
        if rows and any(safe in re.split(r"\W+", (r.get("name") or "").lower()) for r in rows):
            return _ps_first(rows)
        # 1) Exact-name for multi-token terms (usually empty — names rarely
        #    equal the stripped phrase).
        if not single:
            rows_eq = self._apicalypse(
                "games",
                f'fields id,name,platforms,first_release_date; where name ~ "{safe}"; limit 10;',
            )
            if rows_eq:
                return rows_eq
        # 2) Substring as a last resort ('The Gardens Between' for the term
        #    'gardens between'); return ALL rows so the exact-name check in the
        #    matcher is never starved.
        rows_sub = self._apicalypse(
            "games",
            f'fields id,name,platforms,first_release_date; where name ~ *"{safe}"*; sort name asc; limit 10;',
        )
        return rows_sub or _ps_first(rows)

    def game_details(self, game_id: int) -> dict[str, Any]:
        """Fetch metadata for one game (covers, genres, themes, rating, release)."""
        rows = self._apicalypse(
            "games",
            "fields id,name,slug,url,cover.url,genres.name,themes.name,game_modes.name,"
            "player_perspectives.name,total_rating,aggregated_rating,first_release_date,"
            "age_ratings.rating; "
            f"where id = {game_id}; limit 1;",
        )
        return rows[0] if rows else {}


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

    TROPHY_URL = "https://m.np.playstation.com/api/trophy/v1/users/me/trophyTitles"
    GAME_LIST_URL = "https://m.np.playstation.com/api/gameLibraryService/v3/users/me/gameList"
    GAME_LIST_URL_V2 = "https://m.np.playstation.com/api/gameLibraryService/v2/users/me/gameList"

    def game_list(self, cookies: dict[str, str], limit: int = 500, access_token: str | None = None) -> list[dict[str, Any]]:
        """Playtime pull from the PS App Game Library Service.

        Auth is finicky (verified live 2026-08-20: the refresh-derived Bearer
        scope 403s): try v3 then v2 with (a) cookies only and (b) cookies +
        Bearer, using the freshest access token available. Every attempt's
        status is recorded in `self.last_game_list_probe` for diagnostics.
        Item fields expected: titleId (NPWR), name, playDuration (ISO-8601).
        """
        self.last_game_list_probe: dict[str, Any] = {}
        cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items())
        bearer = access_token
        if not bearer:
            try:
                bearer = self._access_token()
            except PsnAuthError:
                bearer = None
        for url in (self.GAME_LIST_URL, self.GAME_LIST_URL_V2):
            variant = url.rsplit("/", 1)[-1]
            for auth, headers in (
                ("cookies-only", {"Cookie": cookie_hdr, "User-Agent": self.USER_AGENT}),
                ("cookies+bearer", {"Cookie": cookie_hdr, "User-Agent": self.USER_AGENT,
                                    **({"Authorization": f"Bearer {bearer}"} if bearer else {})}),
            ):
                try:
                    resp = self._client.get(url, headers=headers, params={"limit": min(limit, 100), "offset": 0})
                    self.last_game_list_probe[f"{variant}/{auth}"] = {"status": resp.status_code, "body": resp.text[:100]}
                    if resp.status_code != 200:
                        continue
                    data = resp.json() or {}
                    items = data.get("games") or []
                    if items:
                        return items
                except Exception as exc:  # noqa: BLE001 — diagnostics
                    self.last_game_list_probe[f"{variant}/{auth}"] = {"error": str(exc)[:100]}
        return []

    def trophy_titles(self, limit: int = 800) -> list[dict[str, Any]]:
        """Per-title trophy pull from the Trophy API (paginated).

        Same Bearer token as the library pull. NOTE (verified live 2026-08-20):
        the trophy titles response carries NO playtime — `playDuration` is
        absent even when requested, and the PS App gameList endpoints that DO
        hold playtime return 403 with the PS App OAuth scope. What we get:
        earnedTrophies/definedTrophies/progress + trophyTitleName, keyed on
        npCommunicationId (the NPWR trophy-set id — NOT the content id).
        Response: {totalResults, trophyTitles: [{npCommunicationId,
        trophyTitleName, trophyTitlePlatform, earnedTrophies,
        definedTrophies, progress, lastUpdateDate, ...}]}.
        """
        token = self._access_token()
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            resp = self._client.get(
                self.TROPHY_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "limit": min(limit, 800),
                    "offset": offset,
                    "fields": (
                        "npCommunicationId,trophyTitleName,trophyTitleDetail,trophyTitleIconUrl,"
                        "trophyTitlePlatform,hasTrophyGroups,definedTrophies,earnedTrophies,"
                        "progress,hiddenFlag,lastUpdateDate"
                    ),
                    "sortBy": "titleName",
                },
            )
            if resp.status_code in (400, 401):
                raise PsnAuthError(f"PSN trophy request rejected (HTTP {resp.status_code})")
            resp.raise_for_status()
            data = resp.json()
            items = data.get("trophyTitles") or []
            out.extend(items)
            total = data.get("totalResults") or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

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
# Content entitlements that are NOT games (word-bounded so 'demo' doesn't hit
# Demon's Souls, 'ost' doesn't hit Lost, and Theme Hospital stays a game).
# 'the music of' is the Dreams OST add-on title ('The Music of Dreams') — the
# name check alone can't see DREAMSOST in the content id, so _CONTENT_ID_RE
# below catches the add-on ids directly.
_CONTENT_RE = re.compile(r"\b(?:demo|ost)\b|soundtrack|artbook|art book|the music of", re.IGNORECASE)
# Add-on content ids that are not games: Dreams OST (DREAMSOST0000001…) and
# Dreams Art Book (DREAMSARTBOOK001…) end up in the PSN title library under
# names like 'The Music of Dreams' / 'The Art of Dreams' — no IGDB game exists
# for them. The trailing digits are required so 'LOSTGAME…' / 'GHOST…' /
# 'COSTUME…' (letters after OST) never match.
_CONTENT_ID_RE = re.compile(r"(?:OST|ARTBOOK)\d{2,}", re.IGNORECASE)


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
    """Normalize a PSN entitlement into an owned_games row (digital).

    normalized_title uses the same psn.normalize_title as the receipt parser so
    API rows merge into receipt-derived rows (same game, same key)."""
    from mailroom.verticals.game_catalog.parsers.psn import normalize_title

    meta = item.get("gameMeta") or item.get("titleMeta") or item
    name = meta.get("name") or item.get("name") or item.get("localizedName")
    content_id = item.get("productId") or item.get("id") or item.get("titleId")
    if not name or not content_id:
        return None
    low = name.lower()
    for marker in _APP_MARKERS:
        if marker in low:
            return None  # apps / subscriptions — keep the catalog to games
    if _CONTENT_RE.search(name) or _CONTENT_ID_RE.search(content_id):
        return None  # demos / OSTs / artbooks — not games
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
        "normalized_title": normalize_title(name),
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


_ISO8601_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso8601_duration_minutes(duration: str | None) -> int | None:
    """'PT24H15M' -> 1455; 'PT0M' -> 0; None/'' -> None (no playtime)."""
    if not duration:
        return None
    m = _ISO8601_DURATION_RE.fullmatch(duration.strip())
    if not m:
        return None
    hours, minutes, seconds = (int(x or 0) for x in m.groups())
    return hours * 60 + minutes + (1 if seconds else 0)


def _trophy_total(counts: Any) -> int:
    """Trophy count from the API's earned/defined object.

    Shape is {bronze, silver, gold, platinum} (no 'total' key on the live
    trophy API); tolerate a {total: N} variant too.
    """
    if not counts:
        return 0
    if isinstance(counts, dict):
        if counts.get("total") is not None:
            return int(counts["total"] or 0)
        return sum(int(v or 0) for k, v in counts.items() if k in ("bronze", "silver", "gold", "platinum"))
    return int(counts or 0)


def psn_game_list_item_to_stats(item: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a Game Library Service entry into a game_stats row.

    Same NPWR key as the trophy stats (titleId == npCommunicationId), so
    playtime from gameList fills game_stats.playtime_minutes on the rows the
    trophy pass created. playDuration is ISO-8601 ('PT47H20M') -> minutes;
    absent -> None (no playtime recorded by PSN).
    """
    from mailroom.verticals.game_catalog.parsers.psn import normalize_title

    npid = item.get("titleId") or item.get("npCommunicationId")
    name = item.get("name") or item.get("title")
    if not npid or not name:
        return None
    return {
        "trophy_title_id": npid,
        "title": name,
        "normalized_title": normalize_title(name).replace("™", "").replace("®", "").replace("©", ""),
        "playtime_minutes": iso8601_duration_minutes(item.get("playDuration")),
    }


def psn_trophy_item_to_stats(item: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a trophy-title entry into a game_stats row.

    Keyed on npCommunicationId (the NPWR trophy-set id — the ONLY stable key
    the trophy API exposes; the content id is NOT in the response). Joined to
    owned_games by normalized_title with ™/®/© stripped (store titles carry
    '™' — 'ELDEN RING™' vs the trophy set's 'ELDEN RING'). playDuration is
    ISO-8601 ('PT24H15M') -> minutes; the trophy API does not return it today,
    so playtime_minutes stays None until a playtime-capable auth path exists.
    """
    from mailroom.verticals.game_catalog.parsers.psn import normalize_title

    npid = item.get("npCommunicationId")
    name = item.get("trophyTitleName")
    if not npid or not name:
        return None
    return {
        "trophy_title_id": npid,
        "title": name,
        "normalized_title": normalize_title(name).replace("™", "").replace("®", "").replace("©", ""),
        "playtime_minutes": iso8601_duration_minutes(item.get("playDuration")),
        "trophies_earned": _trophy_total(item.get("earnedTrophies")),
        "trophies_defined": _trophy_total(item.get("definedTrophies")),
        "progress": item.get("progress"),
        "last_update": item.get("lastUpdateDate"),
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


@resource(required_resource_keys={"db_url"})
def psn_api_resource(context) -> PsnApiClient:  # type: ignore[no-untyped-def]
    """PSN client built from the refresh token in the credentials table.

    The token lives in the DB (memos/game-catalog-pipeline §PSN sync), not
    .env — a UI refresh writes through mailroom without container edits.
    Declares its db_url dependency so Dagster injects it at resource init
    (BUG-1: without required_resource_keys, cross-resource access fails).
    """
    from mailroom.db import connect, get_credential, init_db

    conn = connect(context.resources.db_url)
    init_db(conn)
    cred = get_credential(conn, "psn") or {}
    conn.close()
    return PsnApiClient(refresh_token=cred.get("token"))
