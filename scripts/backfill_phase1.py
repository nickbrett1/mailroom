"""Phase-1 backfill + validation report (one-time; memo step 4).

Full-history ingest per source (all senders paged, subject-filtered), then
the standard parsed → classified → owned chain, then a per-source report:
receipts, line items, distinct titles, classified buckets, owned counts.

Usage:
    python3 scripts/backfill_phase1.py [--db sqlite:///path.db] [--msgvault http://msgvault:8080] [--max-per-sender 3000]
"""

from __future__ import annotations

import argparse
import os
import sys

from dagster import build_op_context

from mailroom.clients import MsgvaultClient, recover_webview_html
from mailroom.db import connect, init_db, set_cursor, upsert_raw_receipt
from mailroom.verticals.game_catalog import assets
from mailroom.verticals.game_catalog.sources import RETAILER_SOURCES

DEFAULT_MAX = int(os.environ.get("BACKFILL_MAX_PER_SENDER", "3000"))


def fetch_candidates(client: MsgvaultClient, sender: str, subject_contains: list[str], max_msgs: int):
    """Page ALL messages from `sender` (newest-first), client-side subject
    filter, bounded by max_msgs. Returns (candidates, paged_full)."""
    out: list[dict] = []
    offset = 0
    while True:
        page = client._get(
            "/api/v1/messages/filter",
            sender=sender,
            offset=offset,
            limit=500,
            sort="date",
            direction="desc",
        )
        msgs = page.get("messages") or []
        if not msgs:
            return out, True
        for m in msgs:
            if subject_contains and not any(s.lower() in (m.get("subject") or "").lower() for s in subject_contains):
                continue
            out.append(m)
            if len(out) >= max_msgs:
                return out, False
        if not page.get("has_more"):
            return out, True
        offset += len(msgs)


def backfill_raw(client: MsgvaultClient, db_url: str, max_per_sender: int) -> dict:
    conn = connect(db_url)
    init_db(conn)
    stats: dict[str, dict] = {}
    # --- PSN (digital) ---
    for sender in assets.PSN_SENDERS:
        cands, full = fetch_candidates(client, sender, ["Thank You For Your Purchase"], max_per_sender)
        last = 0
        for m in cands:
            mid = int(m["id"])
            detail = client.get_message(mid)
            upsert_raw_receipt(
                conn,
                {
                    "message_id": str(mid),
                    "source": "psn_receipt",
                    "subject": m.get("subject"),
                    "sender": m.get("from_email") or sender,
                    "received_at": detail.get("sent_at") or m.get("sent_at"),
                    "body": detail.get("body_text") or "",
                    "body_html": detail.get("body_html") or "",
                },
            )
            last = max(last, mid)
        stats[f"psn:{sender.split('@')[0]}"] = {"raw": len(cands), "paged_full": full}
        set_cursor(conn, "psn_receipts", str(last) if last else "0")
    # --- retailers (physical) ---
    for source in RETAILER_SOURCES:
        total = 0
        full = True
        last = 0
        for sender in source.senders:
            cands, sender_full = fetch_candidates(client, sender, source.subject_contains, max_per_sender)
            total += len(cands)
            full = full and sender_full
            for m in cands:
                mid = int(m["id"])
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
                        "subject": m.get("subject"),
                        "sender": m.get("from_email") or sender,
                        "received_at": detail.get("sent_at") or m.get("sent_at"),
                        "body": body,
                        "body_html": body_html,
                    },
                )
                last = max(last, mid)
        stats[source.name] = {"raw": total, "paged_full": full}
        set_cursor(conn, f"raw_{source.name}", str(last) if last else "0")
    conn.close()
    return stats


def run_chain(db_url: str, client: MsgvaultClient) -> None:
    ctx = build_op_context(resources={"db_url": db_url, "msgvault": client})
    assets.parsed_purchases_digital(ctx)
    assets.parsed_purchases_physical(ctx)
    assets.classified_game_items(ctx)
    assets.owned_games(ctx)


def report(db_url: str) -> None:
    conn = connect(db_url)
    init_db(conn)
    print("\n================ PHASE-1 VALIDATION REPORT ================")
    print(f"{'source':14s} {'raw':>5s} {'parsed':>6s} {'lines':>6s} {'titles':>7s} | {'ps_game':>8s} {'non_ps':>7s} {'acc':>4s} {'review':>7s} {'full':>5s}")
    rows = conn.execute(
        """SELECT r.source source, COUNT(DISTINCT r.message_id) raw, COUNT(DISTINCT p.id) parsed,
                  COUNT(DISTINCT p.item_key) lines, COUNT(DISTINCT p.title) titles
           FROM raw_receipts r LEFT JOIN parsed_purchases p ON p.source = r.source AND p.message_id = r.message_id
           GROUP BY r.source ORDER BY r.source"""
    ).fetchall()
    cls = {r["source"]: r for r in conn.execute(
        """SELECT source,
                  SUM(classification='playstation_game') ps, SUM(classification='non_playstation') np,
                  SUM(classification='accessory_hardware') acc, SUM(classification='needs_review') rev
           FROM classified_game_items GROUP BY source"""
    ).fetchall()}
    total_lines = total_titles = 0
    for r in rows:
        c = cls.get(r["source"])
        ps = (c["ps"] or 0) if c else 0
        np_ = (c["np"] or 0) if c else 0
        acc = (c["acc"] or 0) if c else 0
        rev = (c["rev"] or 0) if c else 0
        total_lines += r["lines"] or 0
        total_titles += r["titles"] or 0
        print(f"{r['source']:14s} {r['raw']:5d} {r['parsed']:6d} {r['lines']:6d} {r['titles']:7d} | "
              f"{ps:8d} {np_:7d} {acc:4d} {rev:7d}")
    print("-" * 76)
    print(f"{'TOTAL':14s} {sum(r['raw'] for r in rows):5d} {'':>6s} {total_lines:6d} {total_titles:7d}")
    print("\n-- owned_games --")
    for r in conn.execute("SELECT format, retailer, COUNT(*) n FROM owned_games WHERE is_owned=1 GROUP BY format, retailer ORDER BY format, n DESC").fetchall():
        print(f"  {r['format']:9s} {r['retailer'] or '-'!s:11s} {r['n']}")
    d = conn.execute("SELECT COUNT(*) n FROM owned_games WHERE is_owned=1 AND format='digital'").fetchone()["n"]
    p = conn.execute("SELECT COUNT(*) n FROM owned_games WHERE is_owned=1 AND format='physical'").fetchone()["n"]
    dupe = conn.execute(
        """SELECT COUNT(*) n FROM owned_games
           WHERE is_owned=1 AND format='physical'
             AND (provenance LIKE '%;%' OR json_array_length(provenance) > 1)"""
    ).fetchone()["n"]
    print(f"  TOTAL owned: {d + p}  (digital {d}, physical {p})")
    print(f"  distinct titles: {conn.execute('SELECT COUNT(DISTINCT normalized_title) n FROM owned_games WHERE is_owned=1').fetchone()['n']}")
    print(f"  physical rows with multi-source provenance (merged/deduped): {dupe}")
    rq = conn.execute("SELECT reason, COUNT(*) n FROM review_queue WHERE status='open' GROUP BY reason ORDER BY n DESC LIMIT 8").fetchall()
    open_rq = conn.execute("SELECT COUNT(*) n FROM review_queue WHERE status='open'").fetchone()["n"]
    print(f"\n-- review_queue open: {open_rq} --")
    for r in rq:
        print(f"  {r['n']:4d}  {r['reason']}")
    print("===========================================================")
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MAILROOM_DB_URL", "sqlite:////tmp/backfill.db"))
    ap.add_argument("--msgvault", default=os.environ.get("MSGVAULT_URL", "http://msgvault:8080"))
    ap.add_argument("--max-per-sender", type=int, default=DEFAULT_MAX)
    ap.add_argument("--report-only", action="store_true", help="skip ingest, print the report for an existing db")
    args = ap.parse_args()

    if args.report_only:
        report(args.db)
        return
    client = MsgvaultClient(args.msgvault, retries=6)
    stats = backfill_raw(client, args.db, args.max_per_sender)
    for name, s in stats.items():
        flag = "" if s["paged_full"] else "  <-- PARTIAL (max-per-sender hit)"
        print(f"raw {name:16s} {s['raw']:5d}{flag}")
    run_chain(args.db, client)
    report(args.db)


if __name__ == "__main__":
    sys.exit(main())
