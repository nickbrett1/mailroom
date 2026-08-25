"""game_catalog Dagster assets (thin shells over plain library functions).

Chain: raw_psn_receipts → parsed_purchases_digital → classified_game_items
→ owned_games → catalog_views, plus the physical chain raw_retailer_receipts
→ parsed_purchases_physical feeding the same classified → owned_games, plus
psn_api_owned (recurring PSN sync) and the IGDB enrichment chain
igdb_matches → game_metadata → catalog_views.
"""

import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dagster import (
    AssetExecutionContext,
    Field,
    asset,
)

from mailroom.clients import (
    PsnAuthError,
    psn_game_list_item_to_stats,
    psn_library_item_to_game,
    psn_trophy_item_to_stats,
    recover_webview_html,
)
from mailroom.db import (
    checkpoint_wal,
    connect,
    enqueue_review,
    get_credential,
    get_cursor,
    init_db,
    set_credential,
    set_cursor,
    upsert_owned_game,
    upsert_raw_receipt,
)
from mailroom.verticals.game_catalog import dedup
from mailroom.verticals.game_catalog.classifier import Classification, classify_item
from mailroom.verticals.game_catalog.parsers.psn import (
    normalize_title,
    parse_psn_receipt,
)
from mailroom.verticals.game_catalog.sources import RETAILER_SOURCES, parse_source

# Partitioned by day so incremental runs and backfills are per-slice.
# PSN receipt senders across the archive eras (verified 2026-08-16):
# sony@… 2012→2022-12; reply@txn-email.playstation.com + sony@txn-email03… 2022→present.
PSN_SENDERS = (
    "sony@email.sonyentertainmentnetwork.com",
    "email@email.playstation.com",
    "reply@txn-email.playstation.com",
    "sony@txn-email03.playstation.com",
)

# Sources that produce DIGITAL games (PlayStation Store receipts + code
# resellers). Everything else is physical.
DIGITAL_SOURCES = {"psn_receipt", "cdkeys", "gameflip"}


def _price_is_zero(price: str | None) -> bool:
    """True when a parsed price string is $0.00.

    PSN Store 'Thank You For Your Purchase' emails for PS+ claims (monthly /
    catalog 'add to library') carry $0.00 line items — e.g. Hot Wheels
    Unleashed 10/04/2022, Wreckfest 05/08/2021, Witcher 3 Complete Edition
    12/14/2022. The store never emails a receipt for a truly-free game, so a
    $0 PSN item is the claim signal.
    """
    if not price:
        return False
    m = re.search(r"\d+(?:\.\d+)?", price)
    return bool(m) and float(m.group()) == 0.0


@asset(required_resource_keys={"db_url", "msgvault"})
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


@asset(deps=[raw_psn_receipts], required_resource_keys={"db_url"})
def parsed_purchases_digital(context: AssetExecutionContext) -> None:
    """Parse stored PSN receipts into normalized purchases."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    rows = conn.execute(
        "SELECT message_id, body, received_at FROM raw_receipts WHERE source = 'psn_receipt'"
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
                    purchase.purchased_at or row["received_at"],  # email date fallback = acquisition date
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


@asset(required_resource_keys={"db_url", "psn_api"})
def psn_api_owned(context: AssetExecutionContext) -> None:
    """Weekly PSN full-library sync (PS App OAuth refresh token).

    The ONLY record of PS+ claims (monthly + Extra/Premium generate zero
    email). Full pull → idempotent merge keyed on psn_content_id → diff log.
    Auth failures degrade to credentials.status = needs_refresh and the next
    run does a full catch-up — never hard-fails (memos/game-catalog-pipeline
    §PSN sync).
    """
    conn = connect(context.resources.db_url)
    init_db(conn)
    cred = get_credential(conn, "psn")
    if not cred or not cred.get("token"):
        set_credential(conn, "psn", status="needs_refresh", last_error="no refresh token stored")
        context.log.warning("psn_api_owned: no PSN refresh token in credentials table — set one to enable the sync")
        conn.close()
        return
    try:
        titles = context.resources.psn_api.library_titles()
    except PsnAuthError as exc:
        set_credential(conn, "psn", status="needs_refresh", last_error=str(exc))
        context.log.warning(f"psn_api_owned: auth degraded to needs_refresh ({exc}) — weekly retry will catch up")
        conn.close()
        return
    except httpx.HTTPError as exc:  # transport failures are not auth — leave status alone
        context.log.error(f"psn_api_owned: fetch failed: {exc}")
        conn.close()
        return

    # One-time re-normalization for rows written before dashes were unified
    # (normalize_title now maps en/em dashes -> hyphen) so they match API rows.
    conn.execute(
        """UPDATE owned_games
           SET normalized_title = replace(replace(normalized_title, '–', '-'), '—', '-')
           WHERE normalized_title LIKE '%–%' OR normalized_title LIKE '%—%'"""
    )
    conn.commit()

    added = confirmed = 0
    for raw in titles:
        game = psn_library_item_to_game(raw)
        if not game:
            continue
        # Merge into a DIGITAL row only (physical disc rows stay receipt-only):
        # 1) by content id, 2) exact (normalized_title, platform), 3) generic
        # platform receipt rows ("playstation") — backfill the concrete platform.
        exists = conn.execute(
            """SELECT id, platform FROM owned_games
               WHERE psn_content_id = ?
                  OR (format = 'digital' AND normalized_title = ? AND platform = ?)
                  OR (format = 'digital' AND normalized_title = ? AND platform = 'playstation')""",
            (game["psn_content_id"], game["normalized_title"], game["platform"], game["normalized_title"]),
        ).fetchone()
        if exists:
            if exists["platform"] == "playstation" and game["platform"] != "playstation":
                try:
                    conn.execute(
                        "UPDATE owned_games SET platform = ?, updated_at = datetime('now') WHERE id = ?",
                        (game["platform"], exists["id"]),
                    )
                except sqlite3.IntegrityError:
                    # dup guard (catalog-dedup-fix): (igdb_id, platform, format)
                    # already owned elsewhere — the upsert below still merges into
                    # this row (generic-platform branch); dedupe_owned_games will
                    # reconcile platform. Don't fail the whole sync.
                    pass
            confirmed += 1
        else:
            added += 1
        upsert_owned_game(conn, game)
    set_credential(conn, "psn", status="valid", last_success=datetime.now(UTC).isoformat(timespec="seconds"))
    conn.close()
    context.log.info(f"psn_api_owned: {added} added, {confirmed} confirmed/updated ({len(titles)} library items)")


@asset(deps=[psn_api_owned], required_resource_keys={"db_url", "psn_api"})
def psn_playtime(context: AssetExecutionContext) -> None:
    """Playtime + trophy stats per title from PSN.

    The entitlements sync carries no playtime. Trophy stats come from the
    Trophy API (same Bearer token); playtime (playDuration, ISO-8601) comes
    from the store GraphQL getUserGameList (web session cookie). Both upsert
    game_stats keyed on the NPWR title id; catalog_views surfaces
    hours_played / trophy_progress. Auth failures degrade to needs_refresh
    like the sync.
    """
    conn = connect(context.resources.db_url)
    init_db(conn)
    cred = get_credential(conn, "psn")
    if not cred or not cred.get("token"):
        context.log.warning("psn_playtime: no PSN refresh token — skipped")
        conn.close()
        return
    try:
        raw = context.resources.psn_api.trophy_titles()
    except PsnAuthError as exc:
        set_credential(conn, "psn", status="needs_refresh", last_error=str(exc))
        context.log.warning(f"psn_playtime: auth degraded to needs_refresh ({exc})")
        conn.close()
        return
    except httpx.HTTPError as exc:
        context.log.error(f"psn_playtime: fetch failed: {exc}")
        conn.close()
        return
    # Log a raw sample every run so the field shape stays verified against a
    # live account (npCommunicationId is the NPWR set id; no content id, no
    # playDuration — verified live 2026-08-20).
    if raw:
        first = raw[0]
        context.log.info(
            f"psn_playtime: trophy titles n={len(raw)} keys={sorted(first.keys())} "
            f"first_npComm={first.get('npCommunicationId')!r}"
        )
    upserted = 0
    for item in raw:
        stats = psn_trophy_item_to_stats(item)
        if not stats:
            continue
        conn.execute(
            """INSERT INTO game_stats(trophy_title_id, title, normalized_title, playtime_minutes,
                   trophies_earned, trophies_defined, progress, last_update, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(trophy_title_id) DO UPDATE SET
                 title = excluded.title,
                 normalized_title = excluded.normalized_title,
                 playtime_minutes = excluded.playtime_minutes,
                 trophies_earned = excluded.trophies_earned,
                 trophies_defined = excluded.trophies_defined,
                 progress = excluded.progress,
                 last_update = excluded.last_update,
                 updated_at = datetime('now')""",
            (
                stats["trophy_title_id"],
                stats["title"],
                stats["normalized_title"],
                stats["playtime_minutes"],
                stats["trophies_earned"],
                stats["trophies_defined"],
                stats["progress"],
                stats["last_update"],
            ),
        )
        upserted += 1

    # Playtime: the store GraphQL getUserGameList (web.np.playstation.com)
    # returns playDuration per played title, authenticated by the store
    # `session` cookie — the PS App Bearer scope 403s on the old m.np gameList
    # endpoint. Same NPWR key as the trophy stats, so playtime lands on the
    # same game_stats rows; games with playtime but no trophy set get their
    # own rows (0 trophies).
    played = 0
    session = (get_credential(conn, "psn_web_session") or {}).get("token")
    if session:
        try:
            games = context.resources.psn_api.played_games(session)
            context.log.info(f"psn_playtime: store GraphQL played n={len(games)}")
            if games:
                first = games[0]
                with_pd = sum(1 for g in games if g.get("playDuration") is not None)
                context.log.info(
                    f"psn_playtime: played keys={sorted(first.keys())} with_playDuration={with_pd} "
                    f"sample={str(first)[:180]}"
                )
                for g in games:
                    s = psn_game_list_item_to_stats(g)
                    if not s:
                        continue
                    conn.execute(
                        """INSERT INTO game_stats(trophy_title_id, title, normalized_title,
                               playtime_minutes, trophies_earned, trophies_defined, progress,
                               last_update, updated_at)
                           VALUES (?, ?, ?, ?, 0, 0, 0, NULL, datetime('now'))
                           ON CONFLICT(trophy_title_id) DO UPDATE SET
                             title = excluded.title,
                             normalized_title = excluded.normalized_title,
                             playtime_minutes = excluded.playtime_minutes,
                             updated_at = datetime('now')""",
                        (s["trophy_title_id"], s["title"], s["normalized_title"], s["playtime_minutes"]),
                    )
                    played += 1
            else:
                context.log.warning("psn_playtime: getUserGameList returned no games (session may be stale)")
        except (PsnAuthError, httpx.HTTPError, ValueError) as exc:
            context.log.warning(f"psn_playtime: store playtime failed ({exc}) — playtime skipped")
    else:
        context.log.warning("psn_playtime: no psn_web_session credential — playtime needs a store session cookie (POST /manual/psn-web-session)")

    conn.commit()
    conn.close()
    context.log.info(f"psn_playtime: upserted {upserted} trophy stats, {played} playtime rows")


@asset(required_resource_keys={"db_url", "msgvault"})
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


@asset(
    required_resource_keys={"db_url", "msgvault"},
    config_schema={
        "message_ids": Field(list, is_required=True, description="msgvault message ids to backfill into raw_receipts")
    },
)
def backfill_missing_receipts(context: AssetExecutionContext) -> None:
    """Targeted receipt backfill — ingest a specific set of msgvault message
    ids into raw_receipts so a missed game can re-enter the owned catalog
    without a full history re-ingest.

    Use when an order email predates the ingestion window (e.g. the Amazon
    Delivery-estimate email for 'Uncharted: Nathan Drake Collection', msg
    65274, or the GameStop order confirmation 42957) and the sender was never
    picked up by raw_retailer_receipts' incremental cursor. The source is
    inferred from the message sender, so the receipt lands under the right
    retailer and flows through parsed_purchases_physical → classified →
    owned_games on the next materialization. Trigger from the UI or MCP:

        Materialize with config {"message_ids": [65274, 39189, 42957]}
    """
    conn = connect(context.resources.db_url)
    init_db(conn)
    client = context.resources.msgvault
    ids = list(context.op_config["message_ids"])
    # sender -> retailer source name
    sender_to_source = {
        s.lower(): src.name for src in RETAILER_SOURCES for s in src.senders
    }
    done = 0
    for mid in ids:
        detail = client.get_message(int(mid))
        sender = (detail.get("from_email") or "").lower()
        source = sender_to_source.get(sender)
        if source is None:
            context.log.warning(f"backfill_missing_receipts: no retailer for msg {mid} (sender {sender}) — skipped")
            continue
        upsert_raw_receipt(
            conn,
            {
                "message_id": str(mid),
                "source": source,
                "subject": detail.get("subject") or "",
                "sender": detail.get("from_email") or sender,
                "received_at": detail.get("sent_at") or "",
                "body": detail.get("body_text") or "",
                "body_html": detail.get("body_html") or "",
            },
        )
        done += 1
        context.log.info(f"backfill_missing_receipts: ingested msg {mid} -> {source}")
    conn.commit()
    conn.close()
    context.log.info(f"backfill_missing_receipts: {done}/{len(ids)} receipts backfilled")


@asset(
    required_resource_keys={"db_url"},
    config_schema={
        "items": Field(
            list,
            is_required=True,
            description=(
                "Manually recorded order line items, each a dict with keys: source, "
                "order_number, title, platform, price, acquisition_date, note. Used to "
                "catalogue games whose receipt never disclosed them (e.g. Amazon's "
                "anonymized 'Order Total' confirmations)."
            ),
        )
    },
)
def record_known_order_items(context: AssetExecutionContext) -> None:
    """Record owned games that never appeared in a receipt.

    Some old receipts (e.g. Amazon's anonymized 'Order Total' confirmations)
    carry the order total but no line items, so the parser can't see the
    games. When the owner knows what a given order contained, this records
    each title so it flows through parsed_purchases -> classified_game_items ->
    owned_games exactly like a parsed receipt. Durable audit row in
    known_order_items + idempotent insert into parsed_purchases.

    Materialize with config:
      {"items": [
        {"source": "amazon", "order_number": "111-0336555-9833027",
         "title": "Hades - PlayStation 4", "platform": "playstation 4",
         "price": "$24.99", "acquisition_date": "2021-11-21T04:08:56Z", "note": "..."},
        ...
      ]}
    """
    conn = connect(context.resources.db_url)
    init_db(conn)
    items = context.op_config["items"]
    recorded = 0
    for item in items:
        source = str(item.get("source") or "").strip()
        order = str(item.get("order_number") or "").strip()
        title = str(item.get("title") or "").strip()
        if not (source and order and title):
            context.log.warning(f"record_known_order_items: skipping incomplete item {item!r}")
            continue
        platform = item.get("platform")
        price = item.get("price")
        acq = item.get("acquisition_date")
        note = item.get("note")
        conn.execute(
            """INSERT INTO known_order_items(source, order_number, title, platform, price, acquisition_date, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, order_number, title) DO UPDATE SET
                 platform = COALESCE(?, platform),
                 price = COALESCE(?, price),
                 acquisition_date = COALESCE(?, acquisition_date)""",
            (source, order, title, platform, price, acq, note, platform, price, acq),
        )
        conn.execute(
            """INSERT INTO parsed_purchases(source, order_number, item_key, purchased_at, title, platform, price)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, order_number, item_key) DO NOTHING""",
            (source, order, f"{order}:{title}", acq, title, platform, price),
        )
        recorded += 1
    conn.commit()
    conn.close()
    context.log.info(f"record_known_order_items: {recorded}/{len(items)} known items recorded")


@asset(deps=[raw_retailer_receipts], required_resource_keys={"db_url"})
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
                        purchase.purchased_at or row["received_at"],  # email date fallback = acquisition date
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


@asset(deps=[parsed_purchases_digital, parsed_purchases_physical], required_resource_keys={"db_url"})
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


@asset(deps=[classified_game_items], required_resource_keys={"db_url"})
def owned_games(context: AssetExecutionContext) -> None:
    """Merge classified PlayStation items into the definitive store.

    Ownership classification: receipts are purchases EXCEPT PSN Store emails
    whose line item is $0.00 — those are PS+ claims (monthly/catalog 'add to
    library' emails carry a $0 price; see _price_is_zero). A $0 claim must not
    become 'purchased', or the dedup merge would keep it over the API's
    psplus_claimed row and the game would wrongly survive a PS+ cancellation
    (Hot Wheels Unleashed 10/04/2022, memos/catalog-dedup-fix).
    """
    conn = connect(context.resources.db_url)
    init_db(conn)
    rows = conn.execute(
        """SELECT c.*, p.purchased_at AS acquisition_date, p.price AS item_price
           FROM classified_game_items c
           LEFT JOIN parsed_purchases p
             ON p.source = c.source AND p.order_number = c.order_number AND p.item_key = c.item_key
           WHERE c.classification = 'playstation_game'"""
    ).fetchall()
    # Deterministic merge order: PS+ claim rows ($0 PSN items) first, paid rows
    # last, so a game with ANY real purchase ends 'purchased' (min-rank
    # semantics) and a claim-only game ends 'psplus_claimed' — independent of
    # row iteration order (Witcher 3: paid receipt + later $0 PS+ claim).
    rows = sorted(
        rows,
        key=lambda r: 0 if (r["source"] == "psn_receipt" and _price_is_zero(r["item_price"])) else 1,
    )
    added = 0
    for row in rows:
        # psn_receipt = PlayStation Store; cdkeys/gameflip = digital key codes —
        # all digital games. Everything else is physical.
        is_digital = row["source"] in DIGITAL_SOURCES
        ownership_class = "purchased"
        if row["source"] == "psn_receipt" and _price_is_zero(row["item_price"]):
            ownership_class = "psplus_claimed"
        game_id = upsert_owned_game(
            conn,
            {
                "title": row["title"],
                "normalized_title": normalize_title(row["title"]),
                "platform": row["platform"] or "playstation",
                "format": "digital" if is_digital else "physical",
                "ownership_class": ownership_class,
                "retailer": None if is_digital else row["source"],
                "order_number": row["order_number"],
                "item_id": None,
                "condition": None,
                "psn_content_id": None,
                "igdb_id": None,
                "acquisition_date": row["acquisition_date"],  # email date / receipt date
                "price": row["item_price"],  # thread the parsed price through (evidence on the row)
                "source": row["source"],
                "source_ref": row["item_key"],
                "status": "owned",
                "is_owned": 1,
                "provenance": f"{row['source']}:{row['item_key']}",
            },
        )
        added += 1 if game_id else 0
    checkpoint_wal(conn)
    conn.close()
    context.log.info(f"owned_games: {added} rows upserted")


# IGDB platform ids for owned-platform preference among exact-name matches.
_IGDB_PS_IDS = {
    "playstation 5": {167},
    "ps5": {167},
    "playstation 4": {48},
    "ps4": {48},
    "ps vita": {46},
    "vita": {46},
    "playstation 3": {9},
    "ps3": {9},
    "playstation": {7, 8, 9, 38, 46, 48, 167},
    "ps": {7, 8, 9, 38, 46, 48, 167},
}


def _igdb_platform_matches(igdb_platforms: list[int] | None, owned_platform: str) -> bool:
    """True if an IGDB result's platforms include the owned platform (used to
    disambiguate same-name entries — e.g. Resident Evil 4 2005/2011/2023)."""
    if not igdb_platforms:
        return False
    wanted = _IGDB_PS_IDS.get((owned_platform or "").lower(), _IGDB_PS_IDS["playstation"])
    return bool(wanted & set(igdb_platforms))


# IGDB platform ids used for backfilling a generic 'playstation' owned row.
_IGDB_PS4_ID, _IGDB_PS5_ID, _IGDB_PSVR2_ID = 48, 167, 390


def _backfill_platform(payload: dict | None) -> str | None:
    """Concrete platform for a generic owned row from an IGDB metadata payload.

    Returns 'playstation 4' when PS4 (48) is present and PS5 is not,
    'playstation 5' when PS5 (167) is present and PS4 is not, else None
    (ambiguous / neither — a cross-gen or non-PS game stays as-is). PSVR2
    (390) is a PS5 headset, NOT a standalone platform, so it never sets the
    platform — it only marks `is_psvr2` in catalog_views.
    """
    if not payload:
        return None
    ids = {p.get("id") for p in (payload.get("platforms") or []) if isinstance(p, dict)}
    has_ps4, has_ps5 = _IGDB_PS4_ID in ids, _IGDB_PS5_ID in ids
    if has_ps4 and not has_ps5:
        return "playstation 4"
    if has_ps5 and not has_ps4:
        return "playstation 5"
    return None


_ROMAN_TO_ARABIC = {
    "iii": "3", "ii": "2", "iv": "4", "vi": "6", "vii": "7",
    "viii": "8", "ix": "9", "v": "5", "x": "10",
}


def _roman_to_arabic(s: str) -> str:
    """Convert standalone roman-numeral tokens to digits ('God of War III' ->
    'god of war 3', 'Ghostrunner II' -> 'ghostrunner 2', 'The Last of Us
    Part I' -> 'part 1').

    Longest-first so 'iii' isn't clobbered by 'ii'. Single 'i' is converted
    ONLY when it is the final token — 'The Last of Us Part I' is 'part 1',
    but 'I Am Setsuna' stays 'i am setsuna'.
    """
    for rom, arabic in sorted(_ROMAN_TO_ARABIC.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"\b{rom}\b", arabic, s)
    s = re.sub(r"\bi\b(?=\s*$)", "1", s)
    return s


def _igdb_norm(s: str) -> str:
    """Normalize a title/name for comparison: strip punctuation/platform
    words, roman numerals -> digits, collapse whitespace.

    'hits' is Amazon/Sony's "PlayStation Hits" budget re-release branding —
    'Uncharted: Nathan Drake Collection Hits' is the same game as IGDB's
    'Uncharted: The Nathan Drake Collection', so it is dropped for matching.
    """
    s = re.sub(r"[™®©&()]", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:ps4|ps5|ps vita|psvita|ps3|playstation\s*[45]|for playstation\s*[45]|game|hits)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip(" -–—:;").lower()
    return _roman_to_arabic(s)


def _igdb_name_matches(title: str, igdb_name: str | None) -> bool:
    """Exact-name check between our title and an IGDB result name.

    IGDB search ranks hyped/newer titles first (Elden Ring Nightreign before
    Elden Ring), so taking results[0] can link a sequel/spinoff. Prefer the
    result whose name normalizes to the same string as our title.
    """
    if not igdb_name:
        return False
    return _igdb_norm(title) == _igdb_norm(igdb_name)


def _igdb_search_tokens(term: str) -> set[str]:
    """Significant tokens of a (normalized) search term — used to gate the
    no-exact-match fallback so a wrong popular title is not auto-linked."""
    t = _roman_to_arabic(term.lower())
    return set(re.findall(r"[a-z0-9]+", t))


def _igdb_name_compact(name: str) -> str:
    """Space-less lowercase name ('Coffee Talk' -> 'coffeetalk') — lets a
    concatenated PSN title ('CoffeeTalk') match its space-separated IGDB name."""
    return re.sub(r"\s+", "", _igdb_norm(name))


_EDITION_WORDS = (
    "standard edition", "deluxe edition", "ultimate edition", "launch edition",
    "collector's edition", "collectors edition", "game of the year edition",
    "special edition", "complete edition", "definitive edition", "day 1 edition",
    "cross-gen", "digital edition", "premium edition", "anniversary edition",
    "monarch edition", "exclusive", "goty", "remastered", "remake", "version",
)


def igdb_search_term(title: str) -> str:
    """Strip platform/edition/marketplace noise for an IGDB name search."""
    t = re.sub(r"\([^)]*\)", " ", title)   # (PS5), (US), (Game)
    t = re.sub(r"\[[^\]]*\]", " ", t)      # [Devolver Deluxe]
    # ™/®/© FIRST: NFKD below would decompose ™ -> 'TM' ('PS5™' -> 'PS5TM'),
    # poisoning the search term.
    t = re.sub(r"[™®©]", " ", t)
    import unicodedata

    t = unicodedata.normalize("NFKD", t)  # é -> e, û -> u (keeps accented titles searchable)
    t = re.sub(r"[\u0300-\u036f]", "", t)  # combining marks
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2300-\u23FF\u2200-\u22FF]", " ", t)  # emoji/dingbats/math symbols (ZONE OF THE ENDERS …M∀RS)
    t = re.split(r"\s+w/", t)[0]           # drop eBay 'w/ <variant>' suffixes
    t = re.sub(r"\b(?:ps4|ps5|ps vita|psvita|ps3|playstation\s*[45]|for playstation\s*[45]|hits)\b", " ", t, flags=re.IGNORECASE)  # 'hits' = PlayStation Hits re-release branding
    # Edition phrases FIRST (they contain stopwords — 'standard edition' must
    # go as a phrase, not be reduced to a stray 'standard' by the stopword
    # pass below, which would pollute the search term and the token gate).
    for w in _EDITION_WORDS:
        t = re.sub(rf"\b{re.escape(w)}\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:a|an|the|and|for|of|on|with|us|edition)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:sealed|sony|new|brand new|gamestop)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" -–—:;").lower()
    return t[:80] or title[:80].lower()


def igdb_search_terms(title: str) -> list[str]:
    """Candidate search terms, best first: stripped term, raw title, digit-split.

    The stripped term misses concatenated titles ('DIRT5' -> 'dirt 5') and
    over-stripped ones ('God of War III Remastered' losing 'of'/'remastered'),
    so when it returns nothing we fall back to the raw title, then to a
    digit-boundary split ('dirt5' -> 'dirt 5').
    """
    terms = [igdb_search_term(title)]
    raw = re.sub(r"\([^)]*\)", " ", title)
    raw = re.sub(r"\s+", " ", raw).strip(" -–—:;").lower()
    if raw and raw != terms[0]:
        terms.append(raw)
    split = re.sub(r"(?<=[a-z])(?=\d)", " ", terms[0])  # dirt5 -> dirt 5
    split = re.sub(r"(?<=\d)(?=[a-z])", " ", split)      # 5x -> 5 x
    if split and split not in terms:
        terms.append(split)
    # Roman -> digits ('god war iii' -> 'god war 3') so IGDB finds the sequel
    # instead of ranking a hyped newer title first (GODS/GOW III class).
    roman = _roman_to_arabic(terms[0])
    if roman and roman not in terms:
        terms.append(roman)
    return terms


def _pick_igdb_result(title: str, results: list[dict], platform: str, term: str | None = None) -> dict | None:
    """Pick the best IGDB result for a title, or None to leave it unmatched.

    1. EXACT-name results win (platform-preferred among same-name entries).
    2. Otherwise a result is acceptable when the search term's significant
       tokens ALL appear in its normalized name (forward containment) — the
       space-less form also counts so 'CoffeeTalk' ~ 'Coffee Talk'.
    3. When the term has >= 2 tokens, a result whose tokens are a SUBSET of
       the term is also acceptable — the receipt title carries extra words
       ('Unplugged - Air Guitar (Game)' -> 'Unplugged').

    The single-token case is deliberately NOT guarded here: 'Skyrim' must
    match 'The Elder Scrolls V: Skyrim' and 'Skul' -> 'Skul: The Hero Slayer'.
    Truly ambiguous short titles ('Unplugged' -> 'Rock Band Unplugged') are
    pinned by content id in catalog_quality_repairs, which runs after
    igdb_matches in the same job — the read model never shows the wrong pick.
    """
    exact = [r for r in results if _igdb_name_matches(title, r.get("name"))]
    if exact:
        return next((r for r in exact if _igdb_platform_matches(r.get("platforms") or [], platform)), exact[0])
    term = term or igdb_search_term(title)
    tokens = _igdb_search_tokens(term)
    if not tokens:
        return None
    single = len(tokens) == 1
    forward: list[dict] = []  # term appears in the name (base game / DLC shadowing)
    reverse: list[dict] = []  # name is a subset of the term's words (receipt title carries extra words)
    for r in results:
        name = r.get("name") or ""
        name_tokens = _igdb_search_tokens(_igdb_norm(name))
        if not name_tokens:
            continue
        compact_ok = single and _igdb_name_compact(name) == term  # 'CoffeeTalk' ~ 'Coffee Talk'
        if tokens <= name_tokens or compact_ok:  # forward: term appears in the name
            if single and name_tokens != tokens and not compact_ok:
                # single-token guard: 'Dreams' must not auto-link to
                # 'The House in Fata Morgana: ... of Dreams' — the name must BE
                # the term (or its space-less form). Ambiguous common-word
                # titles stay unmatched (review).
                continue
            forward.append(r)
        elif not single and name_tokens <= tokens:  # reverse: name is a subset of the title's words
            reverse.append(r)
    if forward:
        # Among forward-containment candidates, prefer the CLOSEST name: when a
        # base game and its DLC/spin-off both contain the term ('Crypt of the
        # NecroDancer' vs 'Crypt of the NecroDancer: Synchrony'), IGDB ranks the
        # newer DLC first, but the SHORTEST normalized name is the base game.
        # Platform preference still dominates (RE4 2005/2011/2023 class); name
        # length is the tie-break among platform-matching (or all) candidates.
        def _closeness(r: dict) -> tuple[bool, int]:
            n = len(_igdb_search_tokens(_igdb_norm(r.get("name") or "")))
            return (not _igdb_platform_matches(r.get("platforms") or [], platform), n)
        return min(forward, key=_closeness)
    if reverse:
        return next((r for r in reverse if _igdb_platform_matches(r.get("platforms") or [], platform)), reverse[0])
    return None


def _igdb_match_scope(only: list[str]) -> tuple[str, list]:
    """Build a SQL scope clause + params restricting IGDB matching to a subset.

    Each `only` entry matches an owned_games.id, a psn_content_id, OR a
    case-insensitive substring of the title. Empty list => no restriction
    (match ALL eligible rows). Used by igdb_matches so a targeted re-match can
    heal a handful of wrong picks (Crypt of the NecroDancer -> Synchrony)
    without a full ~15-30 min re-match of every row.
    """
    groups: list[str] = []
    params: list = []
    for val in only:
        v = str(val).strip()
        if not v:
            continue
        groups.append("(id = ? OR psn_content_id = ? OR LOWER(title) LIKE ?)")
        params += [int(v) if v.isdigit() else 0, v, f"%{v.lower()}%"]
    if not groups:
        return "", []
    return "AND (" + " OR ".join(groups) + ")", params


@asset(
    deps=[owned_games],
    required_resource_keys={"db_url", "igdb"},
    config_schema={
        "recheck": Field(bool, default_value=False),
        "only": Field(
            [str],
            default_value=[],
            description=(
                "Restrict matching/re-matching to only these owned rows. Each entry "
                "matches an owned_games.id, a psn_content_id, or a case-insensitive "
                "substring of the title. Empty = all rows. Combine with recheck=true "
                "to re-match just a subset (e.g. heal one wrong pick without a full "
                "~15-30 min re-match)."
            ),
        ),
    },
)
def igdb_matches(context: AssetExecutionContext) -> None:
    """Match owned games to IGDB ids (paced).

    Name search after stripping platform/edition words (medium confidence) for
    ALL rows — IGDB external_games has ~no PSN entries (verified 2026-08-17),
    so the psn_content_id -> external_games lookup is only a last-resort
    fallback when search finds nothing. Target <5% unmatched.

    Config `recheck: true` re-matches EVERY row (clears igdb_id first) so the
    improved exact-name/platform matcher heals wrong picks (Elden Ring ->
    Nightreign class) — trigger from the UI (Materialize with config) or via
    the catalog_recheck job. Config `only: [..]` narrows the pass to a subset
    of owned rows (by id / psn_content_id / title substring) — a scoped
    re-match that heals just the given titles without re-processing the whole
    catalog.
    """
    conn = connect(context.resources.db_url)
    init_db(conn)
    igdb = context.resources.igdb
    only = [o for o in (context.op_config.get("only") or []) if str(o).strip()]
    scope_sql, scope_params = _igdb_match_scope(only)
    if context.op_config.get("recheck"):
        # re-match the (optionally scoped) rows: clear igdb_id so the loop below
        # re-processes them with the exact-name/platform matcher (heals wrong picks)
        conn.execute(
            f"UPDATE owned_games SET igdb_id = NULL, updated_at = datetime('now') "
            f"WHERE igdb_id IS NOT NULL {scope_sql}",
            scope_params,
        )
        conn.commit()
    # Re-apply prior MANUAL matches to rows that lost them. A full-chain
    # re-ingestion rebuilds owned_games with igdb_id=NULL, but human picks
    # (confidence='manual') are durable in igdb_matches — keyed on the OLD row
    # id. Match the current unmatched row to the originally-matched row by its
    # stable identity (normalized_title, platform, format) and restore igdb_id
    # so a human pick survives re-ingestion. Idempotent: restored rows then
    # have igdb_id set, so they drop out of the auto-match pass below, and
    # recheck (which clears igdb_id first) restores human picks instead of
    # letting the auto-matcher override them.
    to_restore = conn.execute(
        """SELECT g.id, g.platform, g.format, m.igdb_id
           FROM owned_games g
           JOIN igdb_matches m ON m.confidence = 'manual'
           JOIN owned_games orig ON orig.id = m.owned_game_id
           WHERE g.is_owned = 1 AND g.igdb_id IS NULL
             AND g.normalized_title = orig.normalized_title
             AND g.format = orig.format
             AND (g.platform = orig.platform
                  OR COALESCE(g.platform, '') IN ('playstation', 'ps')
                  OR COALESCE(orig.platform, '') IN ('playstation', 'ps'))"""
    ).fetchall()
    restored = 0
    for r in to_restore:
        try:
            conn.execute(
                "UPDATE owned_games SET igdb_id = ?, updated_at = datetime('now') WHERE id = ?",
                (r["igdb_id"], r["id"]),
            )
            restored += 1
        except sqlite3.IntegrityError:
            # Dedup guard (catalog-dedup-fix): another OWNED row already has
            # this (igdb_id, platform, format) — a restored generic-platform
            # row can collide with its concrete sibling (or vice versa). Collapse
            # instead of leaving a duplicate (mirrors the auto-match handling).
            other = conn.execute(
                """SELECT * FROM owned_games
                   WHERE is_owned = 1 AND igdb_id = ? AND platform = ? AND format = ? AND id != ?""",
                (r["igdb_id"], r["platform"], r["format"], r["id"]),
            ).fetchone()
            this = conn.execute("SELECT * FROM owned_games WHERE id = ?", (r["id"],)).fetchone()
            if other and this and this["is_owned"]:
                dedup.merge_group(conn, [this, other])
            else:
                raise
    conn.commit()
    rows = conn.execute(
        f"SELECT * FROM owned_games WHERE is_owned = 1 AND igdb_id IS NULL {scope_sql}",
        scope_params,
    ).fetchall()
    matched = unmatched = 0
    for row in rows:
        gid, matched_title, method = None, None, None
        # Search EVERY candidate term (not just the first non-empty): a raw or
        # roman-digit term can surface the exact entry the stripped term missed
        # ('GODS Remastered' -> stripped 'gods' ranks Ragnarök first; raw
        # 'gods remastered' returns the exact 'Gods Remastered').
        first_results, first_term = None, None
        results_by_term: dict[str, list] = {}
        for term in igdb_search_terms(row["title"]):
            results = igdb.search_game(term)
            if results:
                if first_results is None:
                    first_results, first_term = results, term
                results_by_term[term] = results
        if first_results:
            # 1) EXACT-name preference: IGDB ranks hyped/newer titles first
            # ('Elden Ring' -> 'Elden Ring Nightreign'), so the exact-name
            # result wins; among same-name entries (Resident Evil 4
            # 2005/2011/2023) prefer our owned platform (PS5 -> the 2023 remake).
            pick = None
            for term, res in results_by_term.items():
                exact = [r for r in res if _igdb_name_matches(row["title"], r.get("name"))]
                if exact:
                    pick = next((r for r in exact if _igdb_platform_matches(r.get("platforms") or [], row["platform"])), exact[0])
                    break
            # 2) No exact name: gate the fallback instead of blindly taking
            # results[0] — a wrong-but-popular entry must not be auto-linked
            # ('Unplugged' -> Rock Band Unplugged; GODS -> Ragnarök; Dreams OST
            # -> High School Musical). Ambiguous picks stay unmatched (review).
            if pick is None:
                pick = _pick_igdb_result(row["title"], first_results, row["platform"], term=first_term)
            if pick is not None:
                gid = pick["id"]
                matched_title = pick.get("name")
                method = "search"
        if not gid and row["psn_content_id"]:
            gid = igdb.game_by_external_psn_uid(row["psn_content_id"])
            method = "external_games"
        if gid:
            conn.execute(
                """INSERT OR IGNORE INTO igdb_matches(owned_game_id, igdb_id, confidence, matched_title)
                   VALUES (?, ?, ?, ?)""",
                (row["id"], gid, "high" if method == "external_games" else "medium", matched_title),
            )
            try:
                conn.execute("UPDATE owned_games SET igdb_id = ? WHERE id = ?", (gid, row["id"]))
            except sqlite3.IntegrityError:
                # Dedup guard (catalog-dedup-fix): another OWNED row already has
                # this (igdb_id, platform, format) — collapse this row into it
                # instead of leaving a duplicate (a receipt row matched after its
                # API row, or two receipts of the same game).
                other = conn.execute(
                    """SELECT * FROM owned_games
                       WHERE is_owned = 1 AND igdb_id = ? AND platform = ? AND format = ? AND id != ?""",
                    (gid, row["platform"], row["format"], row["id"]),
                ).fetchone()
                this = conn.execute("SELECT * FROM owned_games WHERE id = ?", (row["id"],)).fetchone()
                if other and this and this["is_owned"]:
                    dedup.merge_group(conn, [this, other])
                else:
                    raise
            matched += 1
        else:
            unmatched += 1
    conn.commit()
    checkpoint_wal(conn)
    conn.close()
    context.log.info(f"igdb_matches: {matched} matched, {unmatched} unmatched, {restored} manual restored")


@asset(deps=[igdb_matches], required_resource_keys={"db_url"})
def dedupe_owned_games(context: AssetExecutionContext) -> None:
    """Collapse duplicate owned_games rows by IGDB match (idempotent).

    Runs after every enrichment pass: matched rows merge on
    (igdb_id, platform, format), unmatched on (normalized_title, platform,
    format). Winners keep every provenance (JSON list); losers are retired
    (is_owned=0, retire_reason='dup_merged:game_id=<winner>'), never deleted.
    Possible double purchases (distinct order numbers) are flagged to the
    review queue, not silently merged (memos/catalog-dedup-fix).
    """
    conn = connect(context.resources.db_url)
    init_db(conn)
    report = dedup.dedupe_owned_games(conn)
    checkpoint_wal(conn)
    conn.close()
    context.log.info(
        f"dedupe_owned_games: {report.groups} groups merged, {report.retired} rows retired, "
        f"{len(report.review_flags)} review flags"
    )


@asset(deps=[dedupe_owned_games], required_resource_keys={"db_url"})
def catalog_quality_repairs(context: AssetExecutionContext) -> None:
    """Idempotent data-quality repairs the matcher can't do itself.

    Retires cancelled-order junk + non-game add-ons, splits rows whose
    psn_content_id jammed two games together (wrong IGDB match -> dedup
    merge), and pins ambiguous short titles by content id. Every action is
    audited to review_queue; safe to re-run (keyed on stable facts).
    """
    from mailroom.verticals.game_catalog.repairs import apply_catalog_repairs

    conn = connect(context.resources.db_url)
    init_db(conn)
    report = apply_catalog_repairs(conn)
    checkpoint_wal(conn)
    conn.close()
    context.log.info(
        f"catalog_quality_repairs: retired {len(report.retired)}, split {len(report.split)}, "
        f"rematched {len(report.rematched)}, merged {len(report.merged)}, cleaned {len(report.cleaned)}, "
        f"skipped {len(report.skipped)}"
    )
    for r in report.retired + report.split + report.rematched + report.merged + report.cleaned:
        context.log.info(f"  repair: {r}")
    for r in report.skipped:
        context.log.warning(f"  repair skipped (needs review): {r}")


@asset(deps=[dedupe_owned_games, catalog_quality_repairs], required_resource_keys={"db_url", "igdb"})
def game_metadata(context: AssetExecutionContext) -> None:
    """IGDB details per matched game (covers/genres/rating/release/platforms).
    Paced, resumable (skips ids already fetched); on-demand only, not
    scheduled.

    Backfills a generic 'playstation' owned row to the concrete PS4/PS5 when
    the matched IGDB entry is unambiguous, and records the platforms array so
    catalog_views can expose `is_psvr2` (memos/game-catalog-platforms).
    Payloads fetched before `platforms` was in the field list are refetched
    once so the backfill + flag apply to pre-existing matched rows too.
    """
    conn = connect(context.resources.db_url)
    init_db(conn)
    igdb = context.resources.igdb
    # Include ids whose stored payload lacks a platforms array (never fetched,
    # or fetched before `platforms` was requested) so the platform backfill and
    # PSVR2 flag cover rows matched before this change.
    rows = conn.execute(
        """SELECT DISTINCT g.igdb_id FROM owned_games g
           WHERE g.igdb_id IS NOT NULL
             AND g.igdb_id NOT IN (
                 SELECT m.igdb_id FROM game_metadata m
                 WHERE json_valid(m.payload)
                   AND json_type(m.payload, '$.platforms') = 'array')"""
    ).fetchall()
    fetched = 0
    for r in rows:
        gid = r["igdb_id"]
        payload = igdb.game_details(gid)
        if not payload:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO game_metadata(igdb_id, payload) VALUES (?, ?)",
            (gid, json.dumps(payload)),
        )
        fetched += 1
        plat = _backfill_platform(payload)
        if plat:
            conn.execute(
                """UPDATE owned_games SET platform = ?, updated_at = datetime('now')
                   WHERE igdb_id = ? AND is_owned = 1
                     AND platform IN ('playstation', 'ps', NULL)""",
                (plat, gid),
            )
    conn.commit()
    checkpoint_wal(conn)
    conn.close()
    context.log.info(f"game_metadata: fetched {fetched}")


# IGDB cover URLs are `//images.igdb.com/igdb/image/upload/<t_size>/<image_id>.jpg`
# — the API returns a size variant (t_cover_big) that we rewrite to the
# higher-res t_cover_big_2x pshelf actually displays (memos/covers-caching-design).
_IGDB_COVER_URL_RE = re.compile(r"(?:^|/)(t_[a-z0-9_]+)/([a-z0-9]+)\.jpg$", re.IGNORECASE)


def igdb_cover_image_id(cover_url: str | None) -> str | None:
    """Extract the IGDB image id from a cover.url.

    '//images.igdb.com/igdb/image/upload/t_cover_big/co1x9c.jpg' -> 'co1x9c'.
    This is the cache filename key (stable per image across size variants).
    """
    if not cover_url:
        return None
    m = _IGDB_COVER_URL_RE.search(cover_url)
    return m.group(2) if m else None


def igdb_cover_big2x_url(cover_url: str | None) -> str | None:
    """Rewrite a cover.url to the t_cover_big_2x variant on images.igdb.com.

    IGDB returns t_cover_big in the metadata payload; pshelf displays
    t_cover_big_2x, so we fetch that variant directly (one fewer upgrade hop).
    """
    image_id = igdb_cover_image_id(cover_url)
    if not image_id:
        return None
    return f"https://images.igdb.com/igdb/image/upload/t_cover_big_2x/{image_id}.jpg"


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes to a temp file then rename, so a partially-written cover is
    never served to pshelf (readers mount the same volume read-only)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


@asset(deps=[game_metadata], required_resource_keys={"db_url", "igdb"})
def game_covers(context: AssetExecutionContext) -> None:
    """Fetch and cache IGDB cover images to the shared data volume at sync time.

    For every game whose IGDB metadata carries a cover URL, fetch the
    t_cover_big_2x variant and write `/data/covers/<image_id>.jpg`, so pshelf
    can serve covers as static files instead of live-proxying images.igdb.com
    (memos/covers-caching-design). Idempotent: skips when the file exists AND
    the source URL is unchanged; re-fetches when the cover URL changes (IGDB
    metadata updates); upstream 404s leave any existing file and record
    status='missing'. Each fetch is recorded in `game_covers`, which
    `catalog_views` joins to expose `cover_local`.
    """
    covers_dir = Path(os.environ.get("MAILROOM_COVERS_DIR", "/data/covers"))
    covers_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(context.resources.db_url)
    init_db(conn)
    igdb = context.resources.igdb
    rows = conn.execute(
        """SELECT igdb_id, json_extract(payload, '$.cover.url') AS cover_url
           FROM game_metadata
           WHERE json_extract(payload, '$.cover.url') IS NOT NULL"""
    ).fetchall()
    fetched = skipped = missing = failed = 0
    for r in rows:
        cover_url = r["cover_url"]
        image_id = igdb_cover_image_id(cover_url)
        if not image_id:
            continue  # malformed cover.url — nothing to cache
        local = covers_dir / f"{image_id}.jpg"
        cached = conn.execute(
            "SELECT cover_url FROM game_covers WHERE igdb_id = ?", (r["igdb_id"],)
        ).fetchone()
        if local.exists() and cached and cached["cover_url"] == cover_url:
            skipped += 1
            continue
        big2x = igdb_cover_big2x_url(cover_url)
        if not big2x:
            continue
        try:
            data = igdb.fetch_image(big2x)
        except httpx.HTTPError as exc:
            context.log.warning(f"game_covers: fetch failed for igdb {r['igdb_id']}: {exc}")
            failed += 1
            continue
        if data is None:
            # Upstream 404 — leave any existing file in place, record missing so
            # we don't hammer a removed cover on every sync.
            conn.execute(
                """INSERT INTO game_covers(igdb_id, image_id, cover_url, local_path, status, fetched_at)
                   VALUES (?, ?, ?, NULL, 'missing', datetime('now'))
                   ON CONFLICT(igdb_id) DO UPDATE SET
                     image_id = excluded.image_id, cover_url = excluded.cover_url,
                     status = 'missing', fetched_at = datetime('now')""",
                (r["igdb_id"], image_id, cover_url),
            )
            missing += 1
            continue
        _atomic_write(local, data)
        conn.execute(
            """INSERT INTO game_covers(igdb_id, image_id, cover_url, local_path, status, fetched_at)
               VALUES (?, ?, ?, ?, 'ok', datetime('now'))
               ON CONFLICT(igdb_id) DO UPDATE SET
                 image_id = excluded.image_id, cover_url = excluded.cover_url,
                 local_path = excluded.local_path, status = 'ok', fetched_at = datetime('now')""",
            (r["igdb_id"], image_id, cover_url, f"/covers/{image_id}.jpg"),
        )
        fetched += 1
    conn.commit()
    checkpoint_wal(conn)
    conn.close()
    context.log.info(
        f"game_covers: {fetched} fetched, {skipped} cached, {missing} missing(404), {failed} failed"
    )


@asset(deps=[game_metadata, game_covers], required_resource_keys={"db_url"})
def catalog_views(context: AssetExecutionContext) -> None:
    """Read model for the site/MCP — a VIEW created by init_db over
    owned_games LEFT JOIN game_metadata (and game_covers for cover_local).
    Materializing ensures it exists."""
    conn = connect(context.resources.db_url)
    init_db(conn)  # creates/recreates the catalog_views view
    conn.close()
    context.log.info("catalog_views: view ensured")


@asset(
    deps=[owned_games, dedupe_owned_games, catalog_quality_repairs, game_metadata],
    required_resource_keys={"db_url"},
)
def catalog_games(context: AssetExecutionContext) -> None:
    """Build the canonical `games` table + reparent owned_games under it.

    One row per LOGICAL game (memos/catalog-games-model): multiple editions /
    purchases of the same game (Alien Isolation + 'The Collection', Arcade
    Paradise + its VR version, Slay the Spire bought AND PS+ claimed) collapse
    into a single game whose `editions` JSON aggregates the owned rows. The
    front-end reads catalog_games (one card per game); owned_games stays the
    per-edition/per-purchase record.
    """
    from mailroom.verticals.game_catalog.game_groups import build_games

    conn = connect(context.resources.db_url)
    init_db(conn)
    rows = conn.execute("SELECT * FROM owned_games WHERE is_owned = 1").fetchall()
    meta_rows = conn.execute("SELECT igdb_id, payload FROM game_metadata").fetchall()
    metadata: dict[int, dict] = {}
    for r in meta_rows:
        if r["payload"]:
            try:
                metadata[r["igdb_id"]] = json.loads(r["payload"])
            except (ValueError, TypeError):
                pass
    games, report = build_games([dict(r) for r in rows], metadata=metadata)

    conn.execute("DELETE FROM games")
    reparented = 0
    for g in games:
        editions = json.loads(g["editions"])
        cur = conn.execute(
            """INSERT INTO games(title, normalized_title, igdb_id, platform, platforms,
                   formats, ownership_classes, num_editions, purchased,
                   earliest_acquisition, price, provenance, editions, is_psvr2,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            (
                g["title"], g["normalized_title"], g["igdb_id"], g["platform"],
                g["platforms"], g["formats"], g["ownership_classes"], g["num_editions"],
                g["purchased"], g["earliest_acquisition"], g["price"], g["provenance"],
                g["editions"], g["is_psvr2"],
            ),
        )
        gid = cur.lastrowid
        ids = [e["id"] for e in editions]
        if ids:
            conn.execute(
                f"UPDATE owned_games SET game_id = ? WHERE id IN ({','.join('?' * len(ids))})",
                (gid, *ids),
            )
            reparented += len(ids)
    conn.commit()
    checkpoint_wal(conn)
    conn.close()
    report.reparented = reparented
    context.log.info(
        f"catalog_games: {report.games} games from {reparented} owned editions "
        f"({len(report.groups)} groups)"
    )
