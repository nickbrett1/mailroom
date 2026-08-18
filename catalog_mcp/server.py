"""Catalog MCP — read-only FastMCP server over the mailroom catalog store.

Serves the `catalog_views` read model (owned_games + IGDB metadata) to agents
and the frontend. Read-only by construction: opens the SQLite file with
?mode=ro and exposes no write tools (mailroom is the single writer).

Run: python3 server.py   (transport=http, port $CATALOG_PORT or 8766)
"""

from __future__ import annotations

import json
import os
import sqlite3

from fastmcp import FastMCP

DB_PATH = os.environ.get("CATALOG_DB", "/data/mailroom.db")

mcp = FastMCP(
    "mailroom-catalog",
    instructions=(
        "Read-only PlayStation game catalog (owned games + IGDB metadata). "
        "Tools: search_catalog, get_game, catalog_stats, recently_added. "
        "ownership_class: purchased | psplus_claimed | psplus_extra. "
        "format: digital | physical."
    ),
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _game(r: sqlite3.Row) -> dict:
    g = dict(zip(r.keys(), r))
    payload = g.get("igdb_payload")
    g["igdb_payload"] = json.loads(payload) if payload else None
    return g


@mcp.tool
def search_catalog(
    query: str,
    platform: str | None = None,
    format: str | None = None,
    ownership_class: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """Search owned games by title substring, with optional filters."""
    sql = "SELECT * FROM catalog_views WHERE title LIKE ?"
    params: list = [f"%{query}%"]
    for col, val in (("platform", platform), ("format", format), ("ownership_class", ownership_class)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    sql += " ORDER BY title LIMIT ?"
    params.append(limit)
    conn = _conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [_game(r) for r in rows]
    finally:
        conn.close()


@mcp.tool
def get_game(game_id: int) -> dict | None:
    """Full detail for one catalog game (includes the IGDB metadata payload)."""
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM catalog_views WHERE game_id = ?", (game_id,)).fetchone()
        return _game(r) if r else None
    finally:
        conn.close()


@mcp.tool
def catalog_stats() -> dict:
    """Catalog counts + the 'keep if I cancel PS+' split."""
    conn = _conn()
    try:
        totals = dict(conn.execute(
            "SELECT format, COUNT(*) n FROM catalog_views GROUP BY format"
        ).fetchall())
        by_class = dict(conn.execute(
            "SELECT ownership_class, COUNT(*) n FROM catalog_views GROUP BY ownership_class"
        ).fetchall())
        return {
            "total": sum(totals.values()),
            "by_format": totals,
            "by_ownership_class": by_class,
            "keep_if_ps_plus_cancelled": by_class.get("purchased", 0),
            "lost_if_ps_plus_cancelled": by_class.get("psplus_claimed", 0) + by_class.get("psplus_extra", 0),
        }
    finally:
        conn.close()


@mcp.tool
def needs_igdb_match(limit: int = 100) -> list[dict]:
    """Owned games still missing an IGDB match (the review list)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT game_id, title, platform, format, ownership_class FROM catalog_views "
            "WHERE igdb_id IS NULL ORDER BY title LIMIT ?",
            (min(limit, 500),),
        ).fetchall()
        return [_game(r) for r in rows]
    finally:
        conn.close()


@mcp.tool
def recently_added(limit: int = 20) -> list[dict]:
    """Most recently updated catalog games."""
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM catalog_views ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_game(r) for r in rows]
    except sqlite3.OperationalError:
        # catalog_views lacks updated_at in older dbs — fall back to game_id order
        rows = conn.execute("SELECT * FROM catalog_views ORDER BY game_id DESC LIMIT ?", (limit,)).fetchall()
        return [_game(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("CATALOG_PORT", "8766")))
