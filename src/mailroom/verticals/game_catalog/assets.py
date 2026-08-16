"""game_catalog Dagster assets (thin shells over plain library functions).

Chain: raw_psn_receipts → parsed_purchases_digital → classified_game_items
→ owned_games → catalog_views. Future: retailer receipts, psn-api reconcile,
IGDB match + metadata.
"""

from dagster import (
    AssetExecutionContext,
    DailyPartitionsDefinition,
    asset,
)

from mailroom.db import (
    connect,
    enqueue_review,
    get_cursor,
    init_db,
    set_cursor,
    upsert_owned_game,
    upsert_raw_receipt,
)
from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.psn import (
    normalize_title,
    parse_psn_receipt,
)

# Partitioned by day so incremental runs and backfills are per-slice.
DAILY = DailyPartitionsDefinition(start_date="2024-01-01")


@asset(partitions_def=DAILY)
def raw_psn_receipts(context: AssetExecutionContext) -> None:
    """Fetch new PSN receipts from msgvault since the cursor and store raw."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    cursor = get_cursor(conn, "psn_receipts") or ""
    client = context.resources.msgvault
    messages = client.search_messages(
        sender="sony@email.sonyentertainmentnetwork.com",
        subject="Thank You For Your Purchase",
        after=cursor or None,
        limit=200,
    )
    last = cursor
    for m in messages:
        # search_messages returns metadata only; fetch the full body per message.
        detail = client.get_message(int(m["id"]))
        upsert_raw_receipt(
            conn,
            {
                "message_id": str(m["id"]),
                "source": "psn_receipt",
                "subject": detail.get("subject") or m.get("subject"),
                "sender": detail.get("from_email") or m.get("from_email"),
                "received_at": detail.get("sent_at") or m.get("sent_at"),
                "body": detail.get("body_text") or "",
                "body_html": detail.get("body_html") or "",
            },
        )
        last = str(m["id"])
    if last:
        set_cursor(conn, "psn_receipts", last)
    conn.close()


@asset(partitions_def=DAILY, deps=[raw_psn_receipts])
def parsed_purchases_digital(context: AssetExecutionContext) -> None:
    """Parse stored PSN receipts into normalized purchases."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    rows = conn.execute(
        "SELECT message_id, body FROM raw_receipts WHERE source = 'psn_receipt'"
    ).fetchall()
    parsed = 0
    for row in rows:
        purchase = parse_psn_receipt(row["body"] or "", message_id=row["message_id"])
        if not purchase:
            continue
        for i, item in enumerate(purchase.items):
            conn.execute(
                """INSERT INTO parsed_purchases
                   (source, order_number, item_key, purchased_at, title, platform,
                    price, message_id, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, order_number, item_key) DO NOTHING""",
                (
                    purchase.source,
                    purchase.order_number,
                    f"{purchase.order_number}:{i}",
                    purchase.purchased_at,
                    item.title,
                    item.platform_hint,
                    item.price,
                    purchase.message_id,
                    str(purchase.as_dict()),
                ),
            )
        parsed += 1
    conn.commit()
    conn.close()
    context.log.info(f"parsed {parsed} PSN receipts")


@asset(partitions_def=DAILY, deps=[parsed_purchases_digital])
def classified_game_items(context: AssetExecutionContext) -> None:
    """Classify parsed items through the platform gate; ambiguous → review."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    rows = conn.execute(
        "SELECT * FROM parsed_purchases WHERE source = 'psn_receipt'"
    ).fetchall()
    for row in rows:
        c = classify_item(row["title"], platform_hint=row["platform"])
        conn.execute(
            """INSERT INTO classified_game_items
               (source, order_number, item_key, title, platform, classification, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (row["source"], row["order_number"], row["item_key"], row["title"], c.platform, c.classification, c.reason),
        )
        if c.classification == "needs_review":
            enqueue_review(
                conn,
                {
                    "source": row["source"],
                    "order_number": row["order_number"],
                    "title": row["title"],
                    "reason": c.reason,
                    "payload": str(dict(row)),
                },
            )
    conn.commit()
    conn.close()


@asset(partitions_def=DAILY, deps=[classified_game_items])
def owned_games(context: AssetExecutionContext) -> None:
    """Merge classified PlayStation items into the definitive store."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    rows = conn.execute(
        """SELECT c.* FROM classified_game_items c
           WHERE c.classification = 'playstation_game'"""
    ).fetchall()
    added = 0
    for row in rows:
        game_id = upsert_owned_game(
            conn,
            {
                "title": row["title"],
                "normalized_title": normalize_title(row["title"]),
                "platform": row["platform"] or "playstation",
                "format": "digital",
                "retailer": None,
                "order_number": row["order_number"],
                "item_id": None,
                "condition": None,
                "psn_content_id": None,
                "igdb_id": None,
                "acquisition_date": None,
                "price": None,
                "source": row["source"],
                "source_ref": row["item_key"],
                "status": "owned",
                "is_owned": 1,
                "provenance": f"{row['source']}:{row['item_key']}",
            },
        )
        added += 1 if game_id else 0
    conn.close()
    context.log.info(f"owned_games: {added} rows upserted")


@asset(deps=[owned_games])
def catalog_views(context: AssetExecutionContext) -> None:
    """Read models for the site/MCP (counts for now)."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    total = conn.execute("SELECT COUNT(*) AS n FROM owned_games WHERE is_owned = 1").fetchone()["n"]
    context.log.info(f"catalog_views: {total} owned games")
    conn.close()
