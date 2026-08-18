"""game_catalog Dagster assets (thin shells over plain library functions).

Chain: raw_psn_receipts → parsed_purchases_digital → classified_game_items
→ owned_games → catalog_views, plus the physical chain raw_retailer_receipts
→ parsed_purchases_physical feeding the same classified → owned_games, plus
psn_api_owned (recurring PSN sync) and the IGDB enrichment chain
igdb_matches → game_metadata → catalog_views.
"""

import json
import re
from datetime import UTC, datetime

import httpx
from dagster import (
    AssetExecutionContext,
    DailyPartitionsDefinition,
    asset,
)

from mailroom.clients import (
    PsnAuthError,
    psn_library_item_to_game,
    recover_webview_html,
)
from mailroom.db import (
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
from mailroom.verticals.game_catalog.classifier import Classification, classify_item
from mailroom.verticals.game_catalog.parsers.psn import (
    normalize_title,
    parse_psn_receipt,
)
from mailroom.verticals.game_catalog.sources import RETAILER_SOURCES, parse_source

# Partitioned by day so incremental runs and backfills are per-slice.
DAILY = DailyPartitionsDefinition(start_date="2024-01-01")

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


@asset
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
                conn.execute(
                    "UPDATE owned_games SET platform = ?, updated_at = datetime('now') WHERE id = ?",
                    (game["platform"], exists["id"]),
                )
            confirmed += 1
        else:
            added += 1
        upsert_owned_game(conn, game)
    set_credential(conn, "psn", status="valid", last_success=datetime.now(UTC).isoformat(timespec="seconds"))
    conn.close()
    context.log.info(f"psn_api_owned: {added} added, {confirmed} confirmed/updated ({len(titles)} library items)")


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
        """SELECT c.*, p.purchased_at AS acquisition_date
           FROM classified_game_items c
           LEFT JOIN parsed_purchases p
             ON p.source = c.source AND p.order_number = c.order_number AND p.item_key = c.item_key
           WHERE c.classification = 'playstation_game'"""
    ).fetchall()
    added = 0
    for row in rows:
        # psn_receipt = PlayStation Store; cdkeys/gameflip = digital key codes —
        # all digital games. Everything else is physical.
        is_digital = row["source"] in DIGITAL_SOURCES
        game_id = upsert_owned_game(
            conn,
            {
                "title": row["title"],
                "normalized_title": normalize_title(row["title"]),
                "platform": row["platform"] or "playstation",
                "format": "digital" if is_digital else "physical",
                "ownership_class": "purchased",  # receipts = purchases; PS+ claims come via psn_api_owned
                "retailer": None if is_digital else row["source"],
                "order_number": row["order_number"],
                "item_id": None,
                "condition": None,
                "psn_content_id": None,
                "igdb_id": None,
                "acquisition_date": row["acquisition_date"],  # email date / receipt date
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


def _igdb_name_matches(title: str, igdb_name: str | None) -> bool:
    """Exact-name check between our title and an IGDB result name.

    IGDB search ranks hyped/newer titles first (Elden Ring Nightreign before
    Elden Ring), so taking results[0] can link a sequel/spinoff. Prefer the
    result whose name normalizes to the same string as our title.
    """
    if not igdb_name:
        return False

    def norm(s: str) -> str:
        s = re.sub(r"[™®©&()]", " ", s, flags=re.IGNORECASE)
        s = re.sub(r"\b(?:ps4|ps5|ps vita|psvita|ps3|playstation\s*[45]|for playstation\s*[45]|game)\b", " ", s, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", s).strip().lower()

    return norm(title) == norm(igdb_name)


_EDITION_WORDS = (
    "standard edition", "deluxe edition", "ultimate edition", "launch edition",
    "collector's edition", "collectors edition", "game of the year edition",
    "special edition", "complete edition", "definitive edition", "day 1 edition",
    "cross-gen", "digital edition", "premium edition", "anniversary edition",
    "monarch edition", "exclusive", "goty", "remastered", "remake",
)


def igdb_search_term(title: str) -> str:
    """Strip platform/edition/marketplace noise for an IGDB name search."""
    t = re.sub(r"\([^)]*\)", " ", title)   # (PS5), (US), (Game)
    t = re.sub(r"\[[^\]]*\]", " ", t)      # [Devolver Deluxe]
    import unicodedata

    t = unicodedata.normalize("NFKD", t)  # é -> e, û -> u (keeps accented titles searchable)
    t = re.sub(r"[\u0300-\u036f]", "", t)  # combining marks
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2300-\u23FF]", " ", t)  # emoji/dingbats
    t = re.split(r"\s+w/", t)[0]           # drop eBay 'w/ <variant>' suffixes
    t = re.sub(r"\b(?:ps4|ps5|ps vita|psvita|ps3|playstation\s*[45]|for playstation\s*[45])\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:a|an|the|and|for|of|on|with|us|edition)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:sealed|sony|new|brand new)\b", " ", t, flags=re.IGNORECASE)
    for w in _EDITION_WORDS:
        t = re.sub(rf"\b{re.escape(w)}\b", " ", t, flags=re.IGNORECASE)
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
    return terms


@asset(deps=[owned_games])
def igdb_matches(context: AssetExecutionContext) -> None:
    """Match owned games to IGDB ids (paced).

    Name search after stripping platform/edition words (medium confidence) for
    ALL rows — IGDB external_games has ~no PSN entries (verified 2026-08-17),
    so the psn_content_id -> external_games lookup is only a last-resort
    fallback when search finds nothing. Target <5% unmatched.
    """
    conn = connect(context.resources.db_url)
    init_db(conn)
    igdb = context.resources.igdb
    rows = conn.execute("SELECT * FROM owned_games WHERE is_owned = 1 AND igdb_id IS NULL").fetchall()
    matched = unmatched = 0
    for row in rows:
        gid, matched_title, method = None, None, None
        results = []
        for term in igdb_search_terms(row["title"]):
            results = igdb.search_game(term)
            if results:
                break
        if results:
            # Prefer the EXACT-name result over results[0]: IGDB search ranks
            # hyped/newer titles first, so 'Elden Ring' can resolve to
            # 'Elden Ring Nightreign' (325591) instead of Elden Ring (119133).
            exact = next((r for r in results if _igdb_name_matches(row["title"], r.get("name"))), None)
            pick = exact if exact is not None else results[0]
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
            conn.execute("UPDATE owned_games SET igdb_id = ? WHERE id = ?", (gid, row["id"]))
            matched += 1
        else:
            unmatched += 1
    conn.commit()
    conn.close()
    context.log.info(f"igdb_matches: {matched} matched, {unmatched} unmatched")


@asset(deps=[igdb_matches])
def game_metadata(context: AssetExecutionContext) -> None:
    """IGDB details per matched game (covers/genres/rating/release). Paced,
    resumable (skips ids already fetched); on-demand only, not scheduled."""
    conn = connect(context.resources.db_url)
    init_db(conn)
    igdb = context.resources.igdb
    rows = conn.execute(
        """SELECT DISTINCT igdb_id FROM owned_games
           WHERE igdb_id IS NOT NULL
             AND igdb_id NOT IN (SELECT igdb_id FROM game_metadata)"""
    ).fetchall()
    fetched = 0
    for r in rows:
        payload = igdb.game_details(r["igdb_id"])
        if payload:
            conn.execute(
                "INSERT OR REPLACE INTO game_metadata(igdb_id, payload) VALUES (?, ?)",
                (r["igdb_id"], json.dumps(payload)),
            )
            fetched += 1
    conn.commit()
    conn.close()
    context.log.info(f"game_metadata: fetched {fetched}")


@asset(deps=[game_metadata])
def catalog_views(context: AssetExecutionContext) -> None:
    """Read model for the site/MCP — a VIEW created by init_db over
    owned_games LEFT JOIN game_metadata. Materializing ensures it exists."""
    conn = connect(context.resources.db_url)
    init_db(conn)  # creates/recreates the catalog_views view
    conn.close()
    context.log.info("catalog_views: view ensured")

