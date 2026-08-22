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


def _db_path() -> str:
    # read lazily so runtime env overrides / tests with different dbs work
    return os.environ.get("CATALOG_DB", "/data/mailroom.db")

mcp = FastMCP(
    "mailroom-catalog",
    instructions=(
        "Read-only PlayStation game catalog (owned games + IGDB metadata). "
        "Tools: search_catalog, get_game, catalog_stats, recently_added, "
        "list_games (game-centric — one row per game with aggregated editions). "
        "ownership_class: purchased | psplus_claimed | psplus_extra. "
        "format: digital | physical. is_psvr2: true filters to PSVR2 titles "
        "(a category flag — PSVR2 games are still platform 'playstation 5')."
    ),
)


def _conn() -> sqlite3.Connection:
    # BUG-2 fix (memos/mailroom-deploy-bugs): a `?mode=ro` URI connection
    # cannot open a WAL database on a `:ro` mount (SQLite needs to create the
    # -shm index). Open read-write but enforce read-only at the connection:
    # PRAGMA query_only=ON makes every write fail, so this process can never
    # modify the store regardless of the mount mode.
    conn = sqlite3.connect(f"file:{_db_path()}?mode=rw", uri=True)
    conn.execute("PRAGMA query_only=ON")
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
    is_psvr2: bool | None = None,
    limit: int = 25,
) -> list[dict]:
    """Search owned games by title substring, with optional filters."""
    sql = "SELECT * FROM catalog_views WHERE title LIKE ?"
    params: list = [f"%{query}%"]
    for col, val in (("platform", platform), ("format", format), ("ownership_class", ownership_class)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    if is_psvr2 is not None:
        sql += " AND is_psvr2 = ?"
        params.append(1 if is_psvr2 else 0)
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


def _apply_filters(base: str, platform=None, format=None, ownership_class=None, genre=None, is_psvr2=None) -> tuple[str, list]:
    sql, params = base, []
    for col, val in (("platform", platform), ("format", format), ("ownership_class", ownership_class)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    if is_psvr2 is not None:
        sql += " AND is_psvr2 = ?"
        params.append(1 if is_psvr2 else 0)
    if genre:
        sql += " AND genres LIKE ?"
        params.append(f"%{genre}%")
    return sql, params


@mcp.tool
def top_rated(limit: int = 20, platform: str | None = None, format: str | None = None, ownership_class: str | None = None, is_psvr2: bool | None = None) -> list[dict]:
    """The best games in the collection by IGDB rating (unrated last)."""
    sql, params = _apply_filters("SELECT * FROM catalog_views WHERE 1=1", platform=platform, format=format, ownership_class=ownership_class, is_psvr2=is_psvr2)
    conn = _conn()
    try:
        rows = conn.execute(sql + " ORDER BY rating IS NULL, rating DESC LIMIT ?", [*params, min(limit, 200)]).fetchall()
        return [_game(r) for r in rows]
    finally:
        conn.close()


@mcp.tool
def list_games(
    query: str | None = None,
    platform: str | None = None,
    is_psvr2: bool | None = None,
    purchased: bool | None = None,
    sort: str = "title",
    limit: int = 50,
) -> list[dict]:
    """Game-centric catalog: ONE row per logical game (editions/purchases of
    the same game are aggregated under `editions`). This is the model the
    catalog front-end lists. sort: title | rating | recent | editions.
    Filter by platform substring or is_psvr2/purchased."""
    sql = "SELECT * FROM catalog_games WHERE 1=1"
    params: list = []
    if query:
        sql += " AND title LIKE ?"
        params.append(f"%{query}%")
    if platform:
        sql += " AND platforms LIKE ?"
        params.append(f"%{platform}%")
    if is_psvr2 is not None:
        sql += " AND is_psvr2 = ?"
        params.append(1 if is_psvr2 else 0)
    if purchased is not None:
        sql += " AND purchased = ?"
        params.append(1 if purchased else 0)
    order = {
        "title": "title COLLATE NOCASE ASC",
        "rating": "rating IS NULL, rating DESC",
        "recent": "release_ts IS NULL, release_ts DESC",
        "editions": "num_editions DESC, title COLLATE NOCASE ASC",
    }.get(sort, "title COLLATE NOCASE ASC")
    conn = _conn()
    try:
        rows = conn.execute(sql + f" ORDER BY {order} LIMIT ?", [*params, min(limit, 200)]).fetchall()
        out = []
        for r in rows:
            g = _game(r)
            g["editions"] = json.loads(g.get("editions")) if g.get("editions") else []
            out.append(g)
        return out
    finally:
        conn.close()


@mcp.tool
def catalog_list(
    query: str | None = None,
    platform: str | None = None,
    format: str | None = None,
    ownership_class: str | None = None,
    genre: str | None = None,
    is_psvr2: bool | None = None,
    sort: str = "title",
    limit: int = 50,
) -> list[dict]:
    """List/filter the catalog. sort: title | rating | recent (igdb release) |
    acquired (acquisition_date)."""
    sql = "SELECT * FROM catalog_views WHERE 1=1"
    params: list = []
    if query:
        sql += " AND title LIKE ?"
        params.append(f"%{query}%")
    sql, extra = _apply_filters(sql, platform=platform, format=format, ownership_class=ownership_class, genre=genre, is_psvr2=is_psvr2)
    params.extend(extra)
    order = {
        "title": "title COLLATE NOCASE ASC",
        "rating": "rating IS NULL, rating DESC",
        "recent": "release_ts IS NULL, release_ts DESC",
        "acquired": "acquisition_date IS NULL, acquisition_date DESC",
    }.get(sort, "title COLLATE NOCASE ASC")
    conn = _conn()
    try:
        rows = conn.execute(sql + f" ORDER BY {order} LIMIT ?", [*params, min(limit, 200)]).fetchall()
        return [_game(r) for r in rows]
    finally:
        conn.close()


@mcp.tool
def by_genre(genre: str, limit: int = 20) -> list[dict]:
    """Owned games in a genre, best-rated first (e.g. 'Role-playing (RPG)')."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM catalog_views WHERE genres LIKE ? ORDER BY rating IS NULL, rating DESC LIMIT ?",
            (f"%{genre}%", min(limit, 200)),
        ).fetchall()
        return [_game(r) for r in rows]
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
