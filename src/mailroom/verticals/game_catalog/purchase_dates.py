"""Purchase-date backfill: give `purchased` owned_games their receipt date.

Physical and digital Store purchases that were ingested before the parser
gained its "email received date = acquisition date" fallback sit in
owned_games with NO acquisition_date, so they never show in a purchase-date
view. This backfills them (idempotent — only rows with no date yet):

1. **Fix stale parsed rows** — parsed_purchases.purchased_at was stored as NULL
   before the email-fallback existed. Backfill it from the raw receipt's
   `received_at` (the email date) keyed by message_id.

2. **Date undated purchases**:
   - Receipt-backed rows (physical / psn_receipt carry an `order_number`):
     take the order's earliest parsed `purchased_at`.
   - psn_api-only digitals (no order number, no per-purchase date from the
     API): match them to a PSN receipt (same title + platform, a real paid
     purchase) and take that receipt date. Only applied when the match is
     unambiguous; otherwise left for review (never guessed).

Scope guard: ownership_class='purchased' ONLY — never psplus_claimed/extra,
never a row that already has a date.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from mailroom.db import checkpoint_wal
from mailroom.verticals.game_catalog.parsers.psn import normalize_title

_ZERO_PRICE = re.compile(r"^\$?0+(?:\.00+)?$")


def _price_is_purchase(price: str | None) -> bool:
    return bool(price) and not _ZERO_PRICE.match(price.strip())


def _plat(s: str | None) -> str | None:
    """Coarse platform bucket for compatibility checks."""
    s = (s or "").lower()
    if "ps5" in s or "playstation 5" in s or "ps5" == s:
        return "ps5"
    if "ps4" in s or "playstation 4" in s:
        return "ps4"
    if "vita" in s:
        return "vita"
    if "ps3" in s or "playstation 3" in s:
        return "ps3"
    return None


def _fill_stale_parsed_dates(conn: sqlite3.Connection) -> int:
    """Give parsed purchases that predate the email-fallback a purchased_at."""
    cur = conn.execute(
        """UPDATE parsed_purchases
           SET purchased_at = COALESCE(NULLIF(purchased_at, ''), (
               SELECT r.received_at FROM raw_receipts r
               WHERE r.message_id = parsed_purchases.message_id))
           WHERE purchased_at IS NULL OR purchased_at = ''"""
    )
    return cur.rowcount


def _date_for_order(conn: sqlite3.Connection, order_number: str) -> str | None:
    row = conn.execute(
        """SELECT MIN(purchased_at) AS d FROM parsed_purchases
           WHERE order_number = ? AND purchased_at IS NOT NULL AND purchased_at != ''""",
        (order_number,),
    ).fetchone()
    return row["d"] if row else None


def _date_from_psn_receipt(conn: sqlite3.Connection, title: str, platform: str) -> str | None:
    """psn_api-only digital: match to a paid PSN receipt for the same game.

    Returns the earliest receipt date when exactly one distinct paid order
    matches the title+platform; None otherwise (ambiguous / no receipt)."""
    target = normalize_title(title)
    cands = conn.execute(
        """SELECT order_number, MIN(purchased_at) AS d, title, platform, price
           FROM parsed_purchases
           WHERE source = 'psn_receipt' AND purchased_at IS NOT NULL
             AND purchased_at != ''
           GROUP BY order_number"""
    ).fetchall()
    orders: dict[str, str] = {}
    for c in cands:
        if normalize_title(c["title"]) != target:
            continue
        if _plat(c["platform"]) != _plat(platform):
            continue
        if not _price_is_purchase(c["price"]):
            continue  # a $0 PS+ claim, not a purchase
        orders.setdefault(c["order_number"], c["d"])
    if len(orders) == 1:
        return next(iter(orders.values()))
    return None


def backfill_purchase_dates(conn: sqlite3.Connection) -> dict[str, Any]:
    """Backfill acquisition_date on undated purchased rows from receipt emails."""
    stale = _fill_stale_parsed_dates(conn)
    rows = conn.execute(
        """SELECT id, title, normalized_title, platform, order_number
           FROM owned_games
           WHERE is_owned = 1 AND ownership_class = 'purchased'
             AND (acquisition_date IS NULL OR acquisition_date = '')"""
    ).fetchall()
    dated = 0
    unmatched: list[str] = []
    for row in rows:
        date = None
        if row["order_number"]:
            date = _date_for_order(conn, row["order_number"])
        if not date:
            date = _date_from_psn_receipt(conn, row["title"], row["platform"])
        if date:
            conn.execute(
                """UPDATE owned_games
                   SET acquisition_date = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (date, row["id"]),
            )
            dated += 1
        else:
            unmatched.append(row["title"])
    conn.commit()
    checkpoint_wal(conn)
    return {"stale_parsed_fixed": stale, "dated": dated, "unmatched": len(unmatched)}
