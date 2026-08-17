"""Entity store: portable SQLite schema + thin repository layer.

Design decisions (from the mailroom memo):
- SQLite v1, single-writer (mailroom) with many read-only consumers.
- WAL mode so readers never block the writer and vice versa.
- Portable schema (no SQLite-isms) + DATABASE_URL-style config so a later
  move to Postgres touches only this module and the resource wiring.
- All writes go through mailroom; consumers open read-only (?mode=ro).
"""

from __future__ import annotations

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
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_owned_games_norm ON owned_games(normalized_title);
CREATE INDEX IF NOT EXISTS idx_owned_games_platform ON owned_games(platform);

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
    """Create schema if not present, plus lightweight column migrations."""
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for databases created before a schema change."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(owned_games)").fetchall()}
    if "ownership_class" not in cols:
        conn.execute("ALTER TABLE owned_games ADD COLUMN ownership_class TEXT DEFAULT 'purchased'")


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
    is_owned=0 + reason in provenance)."""
    key = (game.get("psn_content_id") or "", game.get("normalized_title") or "", game.get("platform") or "")
    row = conn.execute(
        """SELECT id FROM owned_games
           WHERE (psn_content_id = ? OR (normalized_title = ? AND platform = ?))""",
        key,
    ).fetchone()
    if row:
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
            {**game, "id": row["id"]},
        )
        conn.commit()
        return row["id"]
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
