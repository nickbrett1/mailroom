"""Mailroom manual-edit API — the ONLY write path for UI-driven catalog edits.

All writes go through mailroom (single-writer rule): the frontend never
touches the SQLite store directly. This FastAPI service (separate process,
same image, Tailscale-only) exposes the manual-edit endpoints the catalog UI
uses — today: resolving ambiguous/unmatched IGDB matches, the dedup
review queue (possible double purchases, memos/catalog-dedup-fix), and the
PSN credential / web-session stores for the playtime sync.

Endpoints:
  GET  /manual/needs-match           -> owned games without an igdb_id
  POST /manual/igdb-match            -> {owned_game_id, igdb_id, note?} apply a match
  POST /manual/needs-match/exclude   -> {owned_game_id, reason?} retire a non-game from the catalog
  POST /manual/owned-game/rename      -> {owned_game_id, title, platform?} clean a raw listing title / override platform
  GET  /manual/review-queue          -> open (or all) dedup/manual review flags
  POST /manual/review-queue/{id}/resolve -> {decision, note?} adjudicate a flag
  POST /manual/psn-credential        -> {npsso} exchange -> store refresh token + session
  GET  /manual/psn-credential        -> credential status
  POST /manual/psn-cookies           -> store m.np playtime session cookies
  GET  /manual/psn-cookies           -> cookie keys/freshness
  POST /manual/psn-web-session       -> store web store session cookie (GraphQL playtime)
  GET  /manual/psn-web-session       -> web session status
  GET  /health
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mailroom.db import _CLEAR, connect, get_credential, init_db, set_credential
from mailroom.verticals.game_catalog import dedup

app = FastAPI(title="mailroom manual-edit API")


def _db_url() -> str:
    return os.environ.get("MAILROOM_DB_URL", "sqlite:////data/mailroom.db")


class MatchRequest(BaseModel):
    owned_game_id: int
    igdb_id: int
    note: str | None = None


class PsnCredentialRequest(BaseModel):
    npsso: str


class PsnCookiesRequest(BaseModel):
    """m.np.playstation.com session cookies — either a {name: value} dict or
    Chrome DevTools 'Copy as JSON' (a list of {name, value, ...} objects)."""
    cookies: dict[str, str] | list[dict]


class PsnWebSessionRequest(BaseModel):
    """Web store session cookie for GraphQL playtime (getUserGameList).

    Paste the `Cookie:` header value from a signed-in store.playstation.com
    request to web.np.playstation.com (DevTools -> Network -> Copy as cURL).
    """
    session_cookie: str


class ReviewResolveRequest(BaseModel):
    decision: str  # e.g. 'double_purchase' | 'same_purchase' | 'not_a_duplicate'
    note: str | None = None


class KnownOrderItemRequest(BaseModel):
    """A game that never appeared in a receipt (anonymized Amazon confirmations).

    Recorded durably in known_order_items and fed into parsed_purchases so it
    flows to owned_games with the order's acquisition date."""
    source: str
    order_number: str
    title: str
    platform: str | None = None
    price: str | None = None
    acquisition_date: str | None = None
    note: str | None = None


class ExcludeRequest(BaseModel):
    """Mark an owned game as excluded (a non-game) so it leaves the catalog.

    Owned games that slipped past the classifier but aren't really in the
    library — an artbook, a beta, or a reseller-listing mis-parse — shouldn't
    be matched to IGDB or shown in the catalog. Exclusion retires the row
    (is_owned=0, never deleted) and is audited to review_queue."""
    owned_game_id: int
    reason: str | None = None


class RenameRequest(BaseModel):
    """Clean a raw listing title (eBay "SEALED ... w/ ...", bundle wording)
    to the canonical game title. Updates the owned row's title + normalized
    title; catalog_games picks up the new name on its next materialization.
    Optionally overrides `platform` (e.g. 'ps vita' for a "PS Vita" title that
    was stored as a generic 'playstation')."""
    owned_game_id: int
    title: str
    platform: str | None = None


def _conn():
    conn = connect(_db_url())
    init_db(conn)
    return conn


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/manual/needs-match")
def needs_match(limit: int = 100) -> list[dict]:
    """Owned games with no IGDB match yet (the review list for the UI)."""
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id AS owned_game_id, title, platform, format, ownership_class, retailer
               FROM owned_games WHERE is_owned = 1 AND igdb_id IS NULL
               ORDER BY title LIMIT ?""",
            (min(limit, 500),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/manual/review-queue")
def review_queue(status: str = "open", limit: int = 100) -> list[dict]:
    """Review flags for human adjudication — possible double purchases surfaced
    by the dedup pass, plus manual-match resolutions (memos/catalog-dedup-fix).

    status: 'open' (default) | 'resolved' | 'all'.
    """
    conn = _conn()
    try:
        if status == "all":
            where = ""
        elif status == "resolved":
            where = "WHERE status = 'resolved'"
        else:
            where = "WHERE status = 'open'"
        rows = conn.execute(
            f"""SELECT id, source, order_number, title, reason, payload, status, created_at
                FROM review_queue {where}
                ORDER BY status = 'open' DESC, id DESC LIMIT ?""",
            (min(limit, 500),),
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["payload"] = json.loads(r["payload"]) if r["payload"] else {}
            except (ValueError, TypeError):
                item["payload"] = r["payload"] or ""
            out.append(item)
        return out
    finally:
        conn.close()


@app.post("/manual/review-queue/{flag_id}/resolve")
def resolve_review_flag(flag_id: int, req: ReviewResolveRequest) -> dict:
    """Adjudicate a review flag (e.g. 'same_purchase' — the two receipts were
    the same purchase re-parsed, so the merge was correct; 'double_purchase' —
    a genuine duplicate buy, keep as-is). Marks it resolved with the decision
    recorded on the flag (audit trail)."""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (flag_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"no review flag with id {flag_id}")
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (ValueError, TypeError):
            payload = {"raw": row["payload"]}
        payload["decision"] = req.decision
        if req.note:
            payload["note"] = req.note
        conn.execute(
            "UPDATE review_queue SET status = 'resolved', payload = ? WHERE id = ?",
            (json.dumps(payload), flag_id),
        )
        conn.commit()
        return {"id": flag_id, "status": "resolved", "decision": req.decision, "payload": payload}
    finally:
        conn.close()


@app.post("/manual/known-order-item")
def known_order_item(req: KnownOrderItemRequest) -> dict:
    """Record a game that never appeared in its receipt (anonymized Amazon
    order confirmations carry the total but no line items). Stores a durable
    row in known_order_items and feeds it into parsed_purchases so the game
    flows to owned_games with the order's acquisition date. Idempotent on
    (source, order_number, title)."""
    if not (req.source.strip() and req.order_number.strip() and req.title.strip()):
        raise HTTPException(400, "source, order_number and title are required")
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO known_order_items(source, order_number, title, platform, price, acquisition_date, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, order_number, title) DO UPDATE SET
                 platform = COALESCE(?, platform),
                 price = COALESCE(?, price),
                 acquisition_date = COALESCE(?, acquisition_date)""",
            (req.source, req.order_number, req.title, req.platform, req.price,
             req.acquisition_date, req.note, req.platform, req.price, req.acquisition_date),
        )
        conn.execute(
            """INSERT INTO parsed_purchases(source, order_number, item_key, purchased_at, title, platform, price)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, order_number, item_key) DO NOTHING""",
            (req.source, req.order_number, f"{req.order_number}:{req.title}",
             req.acquisition_date, req.title, req.platform, req.price),
        )
        conn.commit()
        return {"status": "recorded", "source": req.source, "order_number": req.order_number, "title": req.title}
    finally:
        conn.close()


@app.get("/manual/known-order-items")
def known_order_items() -> list[dict]:
    """List manually recorded known-order items (receipts that never disclosed
    their games)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM known_order_items ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _normalize_cookies(raw: dict[str, str] | list[dict]) -> dict[str, str]:
    """Accept {name: value} or Chrome DevTools cookie-array JSON."""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if k and v is not None}
    out: dict[str, str] = {}
    for item in raw or []:
        if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
            out[str(item["name"])] = str(item["value"])
    return out


@app.post("/manual/psn-cookies")
def psn_cookies(req: PsnCookiesRequest) -> dict:
    """Store the m.np.playstation.com session cookies for playtime.

    gameList (gameLibraryService) is behind Akamai bot protection that
    rejects script-minted sessions (verified live 2026-08-20) — only cookies
    copied from a REAL browser session on m.np.playstation.com carry the
    validated _sk/_abck fingerprint. Paste them (DevTools -> Application ->
    Cookies -> https://m.np.playstation.com -> Copy as JSON). Valid months.
    """
    cookies = _normalize_cookies(req.cookies)
    if not cookies:
        raise HTTPException(400, "no usable cookies (need name/value pairs)")
    conn = _conn()
    set_credential(conn, "psn_cookies", token=json.dumps(cookies),
                   token_type="session_cookies", status="valid", last_error=_CLEAR)
    conn.close()
    return {"status": "valid", "stored": len(cookies), "keys": sorted(cookies.keys())}


@app.get("/manual/psn-cookies")
def psn_cookies_status() -> dict:
    """Read-only: cookie keys + freshness (never the values)."""
    conn = _conn()
    try:
        cred = get_credential(conn, "psn_cookies")
        keys: list[str] = []
        if cred and cred.get("token"):
            try:
                keys = sorted(json.loads(cred["token"]).keys())
            except (ValueError, TypeError):
                pass
        return {
            "status": (cred or {}).get("status", "missing"),
            "stored": len(keys),
            "keys": keys,
            "updated_at": (cred or {}).get("updated_at"),
        }
    finally:
        conn.close()


@app.post("/manual/psn-web-session")
def psn_web_session(req: PsnWebSessionRequest) -> dict:
    """Store the web store session cookie for GraphQL playtime.

    getUserGameList (web.np.playstation.com GraphQL) returns playDuration and
    authenticates with the store `session` cookie — NOT the PS App cookies the
    old m.np gameList needed (which the modern web store no longer serves).
    Paste the full `Cookie:` header value (session=...; userinfo=...; ...).
    Valid while the web session lasts.
    """
    cookie = req.session_cookie.strip()
    if not cookie:
        raise HTTPException(400, "no session cookie provided")
    conn = _conn()
    set_credential(conn, "psn_web_session", token=cookie,
                   token_type="web_session_cookie", status="valid", last_error=_CLEAR)
    conn.close()
    return {"status": "valid", "source": "psn_web_session"}


@app.get("/manual/psn-web-session")
def psn_web_session_status() -> dict:
    """Read-only: web session presence + freshness (never the value)."""
    conn = _conn()
    try:
        cred = get_credential(conn, "psn_web_session")
        return {
            "source": "psn_web_session",
            "status": (cred or {}).get("status", "missing"),
            "token_type": (cred or {}).get("token_type"),
            "updated_at": (cred or {}).get("updated_at"),
        }
    finally:
        conn.close()


@app.post("/manual/igdb-match")
def igdb_match(req: MatchRequest) -> dict:
    """Apply a human-picked IGDB match for an owned game (review resolution).

    Writes igdb_matches + backfills owned_games.igdb_id; the next
    game_metadata run fetches the payload. Recorded in review_queue so the
    resolution is auditable (never silently dropped).
    """
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (req.owned_game_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"no owned game with id {req.owned_game_id}")
        # Dedup guard (catalog-dedup-fix): another OWNED row already claims this
        # (igdb_id, platform, format) — merge instead of leaving a duplicate.
        other = conn.execute(
            """SELECT * FROM owned_games
               WHERE is_owned = 1 AND igdb_id = ? AND platform = ? AND format = ? AND id != ?""",
            (req.igdb_id, row["platform"], row["format"], req.owned_game_id),
        ).fetchone()
        if other:
            winner_id = dedup.merge_group(conn, [row, other])
            conn.execute(
                """INSERT OR IGNORE INTO igdb_matches(owned_game_id, igdb_id, confidence, matched_title)
                   VALUES (?, ?, 'manual', ?)""",
                (req.owned_game_id, req.igdb_id, row["title"]),
            )
            conn.execute(
                """INSERT INTO review_queue(source, order_number, title, reason, payload, status)
                   VALUES ('manual_igdb_match', ?, ?, ?, ?, 'resolved')
                   ON CONFLICT(source, order_number, title, reason) DO NOTHING""",
                (
                    str(row["order_number"] or ""),
                    row["title"],
                    f"manual IGDB match applied (igdb {req.igdb_id}); merged into owned game {winner_id}",
                    req.note or "",
                ),
            )
            conn.commit()
            return {
                "owned_game_id": winner_id,
                "igdb_id": req.igdb_id,
                "applied": True,
                "merged": True,
                "retired_owned_game_id": req.owned_game_id,
            }
        conn.execute(
            """INSERT OR IGNORE INTO igdb_matches(owned_game_id, igdb_id, confidence, matched_title)
               VALUES (?, ?, 'manual', ?)""",
            (req.owned_game_id, req.igdb_id, row["title"]),
        )
        conn.execute("UPDATE owned_games SET igdb_id = ?, updated_at = datetime('now') WHERE id = ?", (req.igdb_id, req.owned_game_id))
        conn.execute(
            """INSERT INTO review_queue(source, order_number, title, reason, payload, status)
               VALUES ('manual_igdb_match', ?, ?, ?, ?, 'resolved')
               ON CONFLICT(source, order_number, title, reason) DO NOTHING""",
            (
                str(row["order_number"] or ""),
                row["title"],
                f"manual IGDB match applied (igdb {req.igdb_id})",
                req.note or "",
            ),
        )
        conn.commit()
        return {"owned_game_id": req.owned_game_id, "igdb_id": req.igdb_id, "applied": True}
    finally:
        conn.close()


@app.post("/manual/needs-match/exclude")
def exclude_needs_match(req: ExcludeRequest) -> dict:
    """Mark an owned game as excluded (a non-game) so it leaves the catalog.

    Some owned rows are not games — an artbook, a beta, a hardware add-on, or
    a mis-parsed reseller listing. They'd otherwise sit forever in the
    needs-match list and clutter the catalog. Exclusion retires the row
    (is_owned=0, retire_reason='excluded:...', never deleted) and audits the
    decision to review_queue so it's reversible/auditable.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM owned_games WHERE id = ?", (req.owned_game_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"no owned game with id {req.owned_game_id}")
        if row["is_owned"] != 1:
            raise HTTPException(409, f"owned game {req.owned_game_id} is already retired/excluded")
        reason = (req.reason or "").strip()
        retire_reason = f"excluded:{reason}" if reason else "excluded:not_a_game"
        conn.execute(
            """UPDATE owned_games SET is_owned = 0, status = 'retired',
               retire_reason = ?, updated_at = datetime('now') WHERE id = ?""",
            (retire_reason, req.owned_game_id),
        )
        conn.execute(
            """INSERT INTO review_queue(source, order_number, title, reason, payload, status)
               VALUES ('manual_exclude', ?, ?, ?, ?, 'resolved')""",
            (
                str(row["order_number"] or ""),
                row["title"],
                f"excluded from catalog ({retire_reason})",
                req.reason or "not a game",
            ),
        )
        conn.commit()
        return {
            "owned_game_id": req.owned_game_id,
            "title": row["title"],
            "excluded": True,
            "retire_reason": retire_reason,
        }
    finally:
        conn.close()


@app.post("/manual/owned-game/rename")
def rename_owned_game(req: RenameRequest) -> dict:
    """Clean a raw listing title to the canonical game title.

    eBay / bundle listings carry listing wording ('SEALED Wildermyth for Sony
    PlayStation 5 (PS5) w/ Monster Compendium', 'Metro Awakening + Arizona
    Sunshine 2') that should not be the catalog card name. Updates the owned
    row's title + normalized_title; catalog_games uses the new name on its
    next materialization. Audited to review_queue.
    """
    from mailroom.verticals.game_catalog.parsers.psn import normalize_title

    title = (req.title or "").strip()
    if not title:
        raise HTTPException(400, "title is required")
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM owned_games WHERE id = ?", (req.owned_game_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"no owned game with id {req.owned_game_id}")
        if req.platform:
            conn.execute(
                "UPDATE owned_games SET platform = ? WHERE id = ?",
                (req.platform.strip().lower(), req.owned_game_id),
            )
        conn.execute(
            """UPDATE owned_games SET title = ?, normalized_title = ?,
               updated_at = datetime('now') WHERE id = ?""",
            (title, normalize_title(title), req.owned_game_id),
        )
        conn.execute(
            """INSERT INTO review_queue(source, order_number, title, reason, payload, status)
               VALUES ('manual_rename', ?, ?, ?, ?, 'resolved')
               ON CONFLICT(source, order_number, title, reason) DO NOTHING""",
            (
                str(row["order_number"] or ""),
                row["title"],
                f"renamed to '{title}'",
                json.dumps({"new_title": title}),
            ),
        )
        conn.commit()
        return {"owned_game_id": req.owned_game_id, "title": title, "renamed": True}
    finally:
        conn.close()


@app.post("/manual/psn-credential")
def psn_credential(req: PsnCredentialRequest) -> dict:
    """Refresh the PSN credential from a user-supplied NPSSO (UI workflow).

    The catalog UI shows credentials.status (needs_refresh) and lets the user
    paste a fresh NPSSO (from https://ca.account.sony.com/api/v1/ssocookie);
    mailroom exchanges it and stores the refresh token — all writes through
    mailroom, never the UI directly.
    """
    from scripts.psn_mint_token import exchange_npsso

    from mailroom.clients import PsnAuthError

    try:
        tokens = exchange_npsso(req.npsso.strip())
    except (PsnAuthError, RuntimeError, Exception) as exc:
        conn = _conn()
        set_credential(conn, "psn", status="needs_refresh", last_error=f"exchange failed: {exc}")
        conn.close()
        raise HTTPException(400, f"NPSSO exchange failed: {exc}") from exc
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise HTTPException(400, "exchange succeeded but no refresh token returned")
    conn = _conn()
    set_credential(conn, "psn", token=refresh, token_type="refresh_token", status="valid", last_error=_CLEAR)
    # Fresh code-exchanged access token — the refresh-derived Bearer 403s on
    # the gameList playtime endpoint; the code exchange may carry full scope.
    access = tokens.get("access_token")
    if access:
        import time as _time

        expires_at = None
        if tokens.get("expires_in"):
            expires_at = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() + int(tokens["expires_in"])))
        set_credential(conn, "psn_access", token=access, token_type="access_token",
                       status="valid", last_error=_CLEAR, expires_at=expires_at)
    # Session cookies for the m.np.playstation.com playtime endpoint (gameList
    # 403s with the Bearer scope; the NPSSO authorize response sets the jar).
    cookies = tokens.get("cookies")
    if cookies:
        set_credential(
            conn, "psn_cookies",
            token=json.dumps(cookies), token_type="session_cookies",
            status="valid", last_error=_CLEAR,
        )
    conn.close()
    return {"status": "valid", "refresh_token_prefix": refresh[:12], "cookies_stored": bool(cookies)}


@app.get("/manual/psn-credential")
def psn_credential_status() -> dict:
    """Read-only credential status for the UI panel."""
    conn = _conn()
    try:
        cred = get_credential(conn, "psn")
        return {
            "source": "psn",
            "status": (cred or {}).get("status", "needs_refresh"),
            "last_success": (cred or {}).get("last_success"),
            "last_error": (cred or {}).get("last_error"),
            "expires_at": (cred or {}).get("expires_at"),
        }
    finally:
        conn.close()
