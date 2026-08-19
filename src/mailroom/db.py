"""Entity store: portable SQLite schema + thin repository layer.

Design decisions (from the mailroom memo):
- SQLite v1, single-writer (mailroom) with many read-only consumers.
- WAL mode so readers never block the writer and vice versa.
- Portable schema (no SQLite-isms) + DATABASE_URL-style config so a later
  move to Postgres touches only this module and the resource wiring.
- All writes go through mailroom; consumers open read-only (?mode=ro).
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

DEFAULT_DB_PATH = os.environ.get("MAILROOM_DB", "/data/mailroom.db")


SCHEMA = """
-- Source cursors for incremental ingestion (per source).
CREATE TABLE IF NOT EXISTS cursors (
    source TEXT PRIMARY KEY,
    cursor_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Raw emails pulled from msgvault (kept for provenance + reparse).
CREATE TABLE IF NOT EXISTS raw_receipts (
    message_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    subject TEXT,
    sender TEXT,
    received_at TEXT,
    body TEXT,
    body_html TEXT
);

-- Parsed purchases (digital + physical) per line item.
-- item_key: order_number + item position/title; Mercari uses item_id.
CREATE TABLE IF NOT EXISTS parsed_purchases (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    order_number TEXT,
    item_key TEXT NOT NULL,
    purchased_at TEXT,
    title TEXT NOT NULL,
    platform TEXT,
    price TEXT,
    qty INTEGER DEFAULT 1,
    condition TEXT,
    message_id TEXT,
    raw_json TEXT,
    UNIQUE(source, order_number, item_key)
);

-- Raw line items retained even when excluded from the catalog (platform gate).
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    order_number TEXT,
    item_id TEXT,
    title TEXT NOT NULL,
    platform TEXT,
    price TEXT,
    qty INTEGER DEFAULT 1,
    retailer TEXT,
    message_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_order_items_unique
    ON order_items(source, order_number, item_id, title);

-- Classified line items: playstation_game / non_playstation /
-- accessory_hardware / needs_review. Idempotent per (source, order, item).
CREATE TABLE IF NOT EXISTS classified_game_items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    order_number TEXT,
    item_key TEXT,
    title TEXT NOT NULL,
    platform TEXT,
    classification TEXT NOT NULL,
    reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_classified_unique
    ON classified_game_items(source, order_number, item_key);

-- The store: definitive PlayStation catalog.
CREATE TABLE IF NOT EXISTS owned_games (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    normalized_title TEXT,
    platform TEXT,
    format TEXT,                -- digital | physical
    ownership_class TEXT DEFAULT 'purchased',  -- purchased | psplus_claimed | psplus_extra
    retailer TEXT,
    order_number TEXT,
    item_id TEXT,
    condition TEXT,
    psn_content_id TEXT,
    igdb_id INTEGER,
    acquisition_date TEXT,
    price TEXT,
    source TEXT,
    source_ref TEXT,
    status TEXT DEFAULT 'owned',
    is_owned INTEGER DEFAULT 1,
    provenance TEXT,
    retire_reason TEXT,          -- set when a dup-merge retires a row (never delete)
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_owned_games_norm ON owned_games(normalized_title);
CREATE INDEX IF NOT EXISTS idx_owned_games_platform ON owned_games(platform);
-- Dedup guard (memos/catalog-dedup-fix): at most one OWNED row per enriched
-- (igdb_id, platform, format). format is part of the key because a digital +
-- physical copy of the same game legitimately coexist (the memo's open
-- question — resolved: keep both when format differs). Created after a dedup
-- pass in init_db (_ensure_dedup_index) so existing dirty stores migrate.
-- (The DDL lives in _ensure_dedup_index, not here, so a fresh CREATE UNIQUE
-- INDEX on an already-dirty table doesn't fail init_db.)

-- IGDB match results with confidence.
CREATE TABLE IF NOT EXISTS igdb_matches (
    owned_game_id INTEGER NOT NULL,
    igdb_id INTEGER NOT NULL,
    confidence TEXT,
    matched_title TEXT,
    matched_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (owned_game_id, igdb_id)
);

-- IGDB metadata payloads (covers, genres, ratings, ...).
CREATE TABLE IF NOT EXISTS game_metadata (
    igdb_id INTEGER PRIMARY KEY,
    payload TEXT,
    fetched_at TEXT DEFAULT (datetime('now'))
);

-- Read model for the catalog site / MCP (owned games + IGDB enrichment).
-- Key IGDB metadata is extracted into real columns (SQLite JSON1) so agents /
-- the site can sort and filter (top rated, by genre, by year) without parsing
-- the payload blob. DROP + CREATE keeps the view definition current on every
-- init_db (views are cheap; recreating applies schema changes).
DROP VIEW IF EXISTS catalog_views;
CREATE VIEW catalog_views AS
SELECT
    g.id AS game_id,
    g.title,
    g.normalized_title,
    g.platform,
    g.format,
    g.ownership_class,
    g.retailer,
    g.order_number,
    g.psn_content_id,
    g.igdb_id,
    g.acquisition_date,
    g.price,
    g.source,
    g.is_owned,
    g.provenance,
    m.payload AS igdb_payload,
    CAST(json_extract(m.payload, '$.total_rating') AS REAL) AS rating,
    CAST(json_extract(m.payload, '$.aggregated_rating') AS REAL) AS aggregated_rating,
    CAST(json_extract(m.payload, '$.first_release_date') AS INTEGER) AS release_ts,
    json_extract(m.payload, '$.cover.url') AS cover_url,
    (SELECT group_concat(json_extract(j.value, '$.name'), ', ')
       FROM json_each(m.payload, '$.genres') j) AS genres
FROM owned_games g
LEFT JOIN game_metadata m ON m.igdb_id = g.igdb_id
WHERE g.is_owned = 1;

-- Source authentication (PSN PS-App OAuth refresh token, future API sources).
-- Token lives here (not .env) so a UI can refresh it without container edits.
CREATE TABLE IF NOT EXISTS credentials (
    source TEXT PRIMARY KEY,
    token TEXT,
    token_type TEXT,
    expires_at TEXT,
    last_success TEXT,
    last_error TEXT,
    status TEXT NOT NULL DEFAULT 'needs_refresh',  -- valid | needs_refresh
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Human review queue: ambiguous platform / unmatched / edge cases.
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY,
    source TEXT,
    order_number TEXT,
    title TEXT,
    reason TEXT,
    payload TEXT,
    status TEXT DEFAULT 'open',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_unique
    ON review_queue(source, order_number, title, reason);
"""


def connect(database_url: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection, WAL mode, foreign keys on.

    DATABASE_URL-style: 'sqlite:///path/to.db' (default) or a bare path.
    A later Postgres switch keeps this function's signature.
    """
    url = database_url or f"sqlite:///{DEFAULT_DB_PATH}"
    if url.startswith("sqlite:///"):
        path = url[len("sqlite://"):]  # keep the leading '/'
        # read-only consumers can pass ?mode=ro in the future
        path = path.split("?")[0]
    else:
        path = url
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create schema if not present, plus lightweight column migrations and the
    dedup guard index (dedupes existing duplicates first — catalog-dedup-fix)."""
    conn.executescript(SCHEMA)
    _migrate(conn)
    _ensure_dedup_index(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for databases created before a schema change."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(owned_games)").fetchall()}
    if "ownership_class" not in cols:
        conn.execute("ALTER TABLE owned_games ADD COLUMN ownership_class TEXT DEFAULT 'purchased'")
    if "retire_reason" not in cols:
        conn.execute("ALTER TABLE owned_games ADD COLUMN retire_reason TEXT")


def _ensure_dedup_index(conn: sqlite3.Connection) -> None:
    """Create the partial unique dedup index, deduping first if needed.

    A plain CREATE UNIQUE INDEX would fail on a store that already has
    duplicates (the exact bug this fixes), so existing duplicates are merged
    (is_owned=0 retire, never delete) before the index is created. Once the
    index exists, any INSERT/UPDATE that would recreate a duplicate owned
    (igdb_id, platform, format) row fails loudly — the ingest paths catch that
    and collapse instead (memos/catalog-dedup-fix).
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_owned_games_dedup'"
    ).fetchone():
        return
    from mailroom.verticals.game_catalog import (
        dedup as _dedup,  # lazy: dedup imports db
    )

    _dedup.dedupe_owned_games(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_owned_games_dedup "
        "ON owned_games(igdb_id, platform, format) "
        "WHERE igdb_id IS NOT NULL AND is_owned = 1"
    )


# --- provenance helpers (dedup / merge support) ---


def provenance_parts(provenance: str | None) -> list[str]:
    """Split a provenance value into its ordered source:ref parts.

    Accepts the scalar form ('psn_api:UP9000-…') used before the dedup fix and
    the merged JSON-array form ('["psn_api:…", "psn_receipt:…"]') the merge
    rules write (memos/catalog-dedup-fix: provenance becomes a list). None /
    empty -> [].
    """
    if not provenance:
        return []
    s = provenance.strip()
    if s.startswith("["):
        try:
            parts = json.loads(s)
        except (ValueError, TypeError):
            return [s]
        return [p for p in parts if isinstance(p, str) and p]
    return [s]


def merge_provenance(*values: str | None) -> str | None:
    """Union of provenance values as a JSON array string (order preserved).

    'psn_api:UP9000-…' + 'psn_receipt:o1' -> '["psn_api:UP9000-…", "psn_receipt:o1"]'.
    Duplicates collapse; no parts -> None (matches NULL provenance rows).
    """
    parts: list[str] = []
    for v in values:
        for p in provenance_parts(v):
            if p not in parts:
                parts.append(p)
    return json.dumps(parts) if parts else None


# --- thin repository helpers (the only place SQL lives) ---


def get_cursor(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT cursor_value FROM cursors WHERE source = ?", (source,)
    ).fetchone()
    return row["cursor_value"] if row else None


def set_cursor(conn: sqlite3.Connection, source: str, value: str) -> None:
    conn.execute(
        """INSERT INTO cursors(source, cursor_value, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(source) DO UPDATE SET
             cursor_value = excluded.cursor_value,
             updated_at = datetime('now')""",
        (source, value),
    )
    conn.commit()


def upsert_raw_receipt(conn: sqlite3.Connection, receipt: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO raw_receipts(message_id, source, subject, sender, received_at, body, body_html)
           VALUES (:message_id, :source, :subject, :sender, :received_at, :body, :body_html)
           ON CONFLICT(message_id) DO UPDATE SET
             source = excluded.source, subject = excluded.subject,
             sender = excluded.sender, received_at = excluded.received_at,
             body = excluded.body, body_html = excluded.body_html""",
        receipt,
    )
    conn.commit()


def upsert_owned_game(conn: sqlite3.Connection, game: dict[str, Any]) -> int:
    """Upsert an owned game. Merge policy: receipts add, psn-api confirms /
    adds claims, manual overrides win, never silently delete (retire via
    is_owned=0 + reason in provenance).

    Enriched games (game['igdb_id'] set) merge on (igdb_id, platform, format)
    FIRST so a re-ingested matched game collapses into its existing row
    instead of inserting a duplicate. Un-enriched games keep the old keys:
    psn_content_id, then (normalized_title, platform) — with generic
    'playstation' receipt rows matching a concrete API row so an API sync
    merges into the receipt row even before the platform backfill happens
    (memos/catalog-dedup-fix).
    """
    if game.get("igdb_id"):
        # Match on the enriched canonical key, tolerant of generic platforms on
        # EITHER side: 'playstation' (receipt rows carry no platform signal)
        # merges into the concrete psn_api row, and vice versa.
        row = conn.execute(
            """SELECT id, provenance FROM owned_games
               WHERE is_owned = 1 AND igdb_id = :igdb_id AND format = :format
                 AND (platform = :platform
                      OR platform IN ('playstation', 'ps')
                      OR :platform IN ('playstation', 'ps'))
               ORDER BY (platform = :platform) DESC, id LIMIT 1""",
            game,
        ).fetchone()
        if row:
            return _update_owned_game(conn, row, game)
    key = (game.get("psn_content_id") or "", game.get("normalized_title") or "", game.get("platform") or "")
    # Same title/format on a generic platform is the same game — a receipt row
    # ('playstation' — the parser emits no platform hint) merges into the
    # concrete psn_api row and vice versa. Prefer the concrete row (has the
    # content id) when both exist.
    row = conn.execute(
        """SELECT id, provenance FROM owned_games
           WHERE (psn_content_id = ?
                  OR (normalized_title = ? AND platform = ?)
                  OR (format = 'digital' AND normalized_title = ?
                      AND (platform IN ('playstation', 'ps') OR ? IN ('playstation', 'ps'))))
           ORDER BY (platform = ?) DESC, (platform IN ('playstation', 'ps')) ASC, id LIMIT 1""",
        (*key, game.get("normalized_title") or "", game.get("platform") or "", game.get("platform") or ""),
    ).fetchone()
    if row:
        return _update_owned_game(conn, row, game)
    cur = conn.execute(
        """INSERT INTO owned_games
           (title, normalized_title, platform, format, ownership_class, retailer,
            order_number, item_id, condition, psn_content_id, igdb_id,
            acquisition_date, price, source, source_ref, status, is_owned, provenance)
           VALUES (:title, :normalized_title, :platform, :format, :ownership_class,
                   :retailer, :order_number, :item_id, :condition, :psn_content_id,
                   :igdb_id, :acquisition_date, :price, :source, :source_ref,
                   :status, :is_owned, :provenance)""",
        game,
    )
    conn.commit()
    return cur.lastrowid


def _update_owned_game(conn: sqlite3.Connection, row: sqlite3.Row, game: dict[str, Any]) -> int:
    """Merge `game` into the existing row: union provenance (never replace —
    provenance is a list, memos/catalog-dedup-fix), COALESCE the rest."""
    merged_prov = merge_provenance(row["provenance"], game.get("provenance"))
    conn.execute(
        """UPDATE owned_games SET
             title = :title, igdb_id = COALESCE(:igdb_id, igdb_id),
             acquisition_date = COALESCE(:acquisition_date, acquisition_date),
             price = COALESCE(:price, price),
             ownership_class = COALESCE(:ownership_class, ownership_class),
             psn_content_id = COALESCE(:psn_content_id, psn_content_id),
             provenance = :provenance,
             updated_at = datetime('now')
           WHERE id = :id""",
        {**game, "id": row["id"], "provenance": merged_prov},
    )
    conn.commit()
    return row["id"]


# --- credential lifecycle (PSN PS-App OAuth + future API sources) ---


def get_credential(conn: sqlite3.Connection, source: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM credentials WHERE source = ?", (source,)).fetchone()
    return dict(row) if row else None


def set_credential(
    conn: sqlite3.Connection,
    source: str,
    *,
    token: str | None = None,
    token_type: str | None = None,
    expires_at: str | None = None,
    status: str | None = None,
    last_error: str | None = None,
    last_success: str | None = None,
) -> None:
    """Upsert a source credential; None fields are left unchanged."""
    cur = get_credential(conn, source) or {}
    conn.execute(
        """INSERT INTO credentials(source, token, token_type, expires_at, status, last_error, last_success, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(source) DO UPDATE SET
             token = COALESCE(?, token),
             token_type = COALESCE(?, token_type),
             expires_at = COALESCE(?, expires_at),
             status = COALESCE(?, status),
             last_error = COALESCE(?, last_error),
             last_success = COALESCE(?, last_success),
             updated_at = datetime('now')""",
        (
            source,
            token if token is not None else cur.get("token"),
            token_type if token_type is not None else cur.get("token_type"),
            expires_at if expires_at is not None else cur.get("expires_at"),
            status if status is not None else cur.get("status", "needs_refresh"),
            last_error if last_error is not None else cur.get("last_error"),
            last_success if last_success is not None else cur.get("last_success"),
            token, token_type, expires_at, status, last_error, last_success,
        ),
    )
    conn.commit()


def enqueue_review(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO review_queue(source, order_number, title, reason, payload)
           VALUES (:source, :order_number, :title, :reason, :payload)
           ON CONFLICT(source, order_number, title, reason) DO NOTHING""",
        item,
    )
    conn.commit()
