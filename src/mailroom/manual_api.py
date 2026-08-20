"""Mailroom manual-edit API — the ONLY write path for UI-driven catalog edits.

All writes go through mailroom (single-writer rule): the frontend never
touches the SQLite store directly. This FastAPI service (separate process,
same image, Tailscale-only) exposes the manual-edit endpoints the catalog UI
uses — today: resolving ambiguous/unmatched IGDB matches and the dedup
review queue (possible double purchases, memos/catalog-dedup-fix).

Endpoints:
  GET  /manual/needs-match           -> owned games without an igdb_id
  POST /manual/igdb-match            -> {owned_game_id, igdb_id, note?} apply a match
  GET  /manual/review-queue          -> open (or all) dedup/manual review flags
  POST /manual/review-queue/{id}/resolve -> {decision, note?} adjudicate a flag
  GET  /health
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mailroom.db import connect, get_credential, init_db, set_credential
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


class ReviewResolveRequest(BaseModel):
    decision: str  # e.g. 'double_purchase' | 'same_purchase' | 'not_a_duplicate'
    note: str | None = None


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
                   token_type="session_cookies", status="valid", last_error=None)
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
                   VALUES ('manual_igdb_match', ?, ?, ?, ?, 'resolved')""",
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
               VALUES ('manual_igdb_match', ?, ?, ?, ?, 'resolved')""",
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
    set_credential(conn, "psn", token=refresh, token_type="refresh_token", status="valid", last_error=None)
    # Fresh code-exchanged access token — the refresh-derived Bearer 403s on
    # the gameList playtime endpoint; the code exchange may carry full scope.
    access = tokens.get("access_token")
    if access:
        import time as _time

        expires_at = None
        if tokens.get("expires_in"):
            expires_at = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() + int(tokens["expires_in"])))
        set_credential(conn, "psn_access", token=access, token_type="access_token",
                       status="valid", last_error=None, expires_at=expires_at)
    # Session cookies for the m.np.playstation.com playtime endpoint (gameList
    # 403s with the Bearer scope; the NPSSO authorize response sets the jar).
    cookies = tokens.get("cookies")
    if cookies:
        set_credential(
            conn, "psn_cookies",
            token=json.dumps(cookies), token_type="session_cookies",
            status="valid", last_error=None,
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
