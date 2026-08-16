"""game_catalog Dagster assets (thin shells over plain library functions).

Chain: raw_psn_receipts → parsed_purchases_digital → classified_game_items
→ owned_games → catalog_views, plus the physical chain raw_retailer_receipts
→ parsed_purchases_physical feeding the same classified → owned_games.
Future: psn-api reconcile, IGDB match + metadata.
"""

from dagster import (
    AssetExecutionContext,
    DailyPartitionsDefinition,
    asset,
)

from mailroom.clients import recover_webview_html
from mailroom.db import (
    connect,
    enqueue_review,
    get_cursor,
    init_db,
    set_cursor,
    upsert_owned_game,
    upsert_raw_receipt,
)
from mailroom.verticals.game_catalog.classifier import Classification, classify_item
from mailroom.verticals.game_catalog.parsers.psn import (
    normalize_title,
    parse_psn_receipt,
)
from mailroom.verticals.game_catalog.sources import RETAILER_SOURCES, parse_source

# Partitioned by day so incremental runs and backfills are per-slice.
DAILY = DailyPartitionsDefinition(start_date="2024-01-01")

# PSN receipt senders across the archive eras (verified 2026-08-16):
# sony@… 2012→2022-12, reply@txn-email.playstation.com 2022-02→present.
PSN_SENDERS = (
    "sony@email.sonyentertainmentnetwork.com",
    "email@email.playstation.com",
    "reply@txn-email.playstation.com",
)


@asset(partitions_def=DAILY)
def raw_psn_receipts(context: AssetExecutionContext) -> None:
    """Fetch new PSN receipts from msgvault since the cursor and store raw."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    cursor = get_cursor(conn, "psn_receipts") or ""
    client = context.resources.msgvault
    messages: list[dict] = []
    for sender in PSN_SENDERS:
        messages += client.search_messages(
            sender=sender,
            subject="Thank You For Your Purchase",
            after=cursor or None,
            limit=200,
        )
    # Newest-first across senders; dedupe + sort by id desc for cursor semantics.
    messages = sorted({int(m["id"]): m for m in messages}.values(), key=lambda m: -int(m["id"]))
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


@asset
def raw_retailer_receipts(context: AssetExecutionContext) -> None:
    """Fetch new retailer order emails per source (senders + subject filter)
    since each source's cursor, and store raw (Best Buy: recover the web-view
    HTML when the archived body is a stub).

    OPS NOTE (Best Buy, memos/DyzFYeFbReur98cjCoLxCJ): msgvault rows ingested
    before the HTML-supporting build have empty body_html even though the raw
    MIME is archived. After the msgvault-side rederive (body_html populated),
    reset the `raw_bestbuy` cursor (DELETE FROM cursors WHERE source =
    'raw_bestbuy') and re-materialize so bodies are re-fetched with HTML."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    client = context.resources.msgvault
    for source in RETAILER_SOURCES:
        cursor_key = f"raw_{source.name}"
        cursor = get_cursor(conn, cursor_key) or ""
        seen_max = int(cursor) if cursor.isdigit() else 0
        for sender in source.senders:
            messages = client.search_messages(sender=sender, after=cursor or None, limit=200)
            for m in messages:
                mid = int(m["id"])
                seen_max = max(seen_max, mid)
                subj = m.get("subject") or ""
                if source.subject_contains and not any(s.lower() in subj.lower() for s in source.subject_contains):
                    continue
                detail = client.get_message(mid)
                body = detail.get("body_text") or ""
                body_html = detail.get("body_html") or ""
                if source.body == "recover" and not body_html:
                    body_html = recover_webview_html(body, client=client) or ""
                upsert_raw_receipt(
                    conn,
                    {
                        "message_id": str(mid),
                        "source": source.name,
                        "subject": subj,
                        "sender": m.get("from_email") or sender,
                        "received_at": detail.get("sent_at") or m.get("sent_at"),
                        "body": body,
                        "body_html": body_html,
                    },
                )
        if seen_max:
            set_cursor(conn, cursor_key, str(seen_max))
    conn.close()


@asset(deps=[raw_retailer_receipts])
def parsed_purchases_physical(context: AssetExecutionContext) -> None:
    """Parse stored retailer receipts into normalized purchases (one row per
    line item, keyed (source, order_number, item_title); Mercari uses item_id).

    Every line item is also retained in order_items so excluded titles are
    never lost."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    rows = conn.execute("SELECT * FROM raw_receipts WHERE source != 'psn_receipt'").fetchall()
    parsed = 0
    for row in rows:
        purchases = parse_source(
            row["source"],
            body=row["body"] or "",
            body_html=row["body_html"] or "",
            subject=row["subject"],
            message_id=row["message_id"],
        )
        for purchase in purchases:
            for i, item in enumerate(purchase.items):
                if purchase.order_number:
                    item_key = f"{purchase.order_number}:{item.title}"
                else:
                    item_key = f"item:{purchase.item_id or item.title}"
                conn.execute(
                    """INSERT INTO parsed_purchases
                       (source, order_number, item_key, purchased_at, title, platform,
                        price, qty, condition, message_id, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source, order_number, item_key) DO NOTHING""",
                    (
                        row["source"],
                        purchase.order_number,
                        item_key,
                        purchase.purchased_at,
                        item.title,
                        item.platform_hint,
                        item.price,
                        item.qty,
                        item.condition,
                        purchase.message_id,
                        str(purchase.as_dict()),
                    ),
                )
                conn.execute(
                    """INSERT INTO order_items
                       (source, order_number, item_id, title, platform, price, qty, retailer, message_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source, order_number, item_id, title) DO NOTHING""",
                    (
                        row["source"],
                        purchase.order_number,
                        purchase.item_id,
                        item.title,
                        item.platform_hint,
                        item.price,
                        item.qty,
                        row["source"],
                        purchase.message_id,
                    ),
                )
            parsed += 1
    conn.commit()
    conn.close()
    context.log.info(f"parsed {parsed} retailer receipts")


@asset(partitions_def=DAILY, deps=[parsed_purchases_digital, parsed_purchases_physical])
def classified_game_items(context: AssetExecutionContext) -> None:
    """Classify parsed items (digital + physical) through the platform gate;
    ambiguous → review queue."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    rows = conn.execute("SELECT * FROM parsed_purchases").fetchall()
    for row in rows:
        if row["source"] == "psn_receipt":
            # PSN receipts are PlayStation by definition — the store only sells
            # PlayStation content. Title keywords ('steam', 'bundle', 'console',
            # 'epic'…) must NOT reclassify them.
            c = Classification("playstation_game", platform="playstation", reason="PSN receipt (platform implicit)")
        else:
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
        is_digital = row["source"] == "psn_receipt"
        game_id = upsert_owned_game(
            conn,
            {
                "title": row["title"],
                "normalized_title": normalize_title(row["title"]),
                "platform": row["platform"] or "playstation",
                "format": "digital" if is_digital else "physical",
                "retailer": None if is_digital else row["source"],
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
