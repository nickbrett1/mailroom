"""Mailroom manual-edit API — the ONLY write path for UI-driven catalog edits.

All writes go through mailroom (single-writer rule): the frontend never
touches the SQLite store directly. This FastAPI service (separate process,
same image, Tailscale-only) exposes the manual-edit endpoints the catalog UI
uses — today: resolving ambiguous/unmatched IGDB matches.

Endpoints:
  GET  /manual/needs-match   -> list of owned games without an igdb_id
  POST /manual/igdb-match    -> {owned_game_id, igdb_id, note?} apply a match
  GET  /health
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mailroom.db import connect, get_credential, init_db, set_credential

app = FastAPI(title="mailroom manual-edit API")


def _db_url() -> str:
    return os.environ.get("MAILROOM_DB_URL", "sqlite:////data/mailroom.db")


class MatchRequest(BaseModel):
    owned_game_id: int
    igdb_id: int
    note: str | None = None


class PsnCredentialRequest(BaseModel):
    npsso: str


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
    conn.close()
    return {"status": "valid", "refresh_token_prefix": refresh[:12]}


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
