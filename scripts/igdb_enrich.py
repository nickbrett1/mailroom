"""IGDB enrichment runner: igdb_matches → game_metadata → catalog_views + report.

Usage:
    doppler run --project mailroom --config dev -- \
        python3 scripts/igdb_enrich.py --db sqlite:////tmp/merged4.db
"""

from __future__ import annotations

import argparse
import os
import sys

from dagster import build_op_context

from mailroom.clients import IgdbClient
from mailroom.db import connect, init_db
from mailroom.verticals.game_catalog import assets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MAILROOM_DB_URL", "sqlite:////data/mailroom.db"))
    ap.add_argument(
        "--recheck",
        action="store_true",
        help="re-match ALL owned rows (not just unmatched): clears igdb_id first so the "
        "exact-name matcher heals wrong picks (e.g. Elden Ring -> Nightreign). ~10-15 min.",
    )
    args = ap.parse_args()

    if args.recheck:
        conn = connect(args.db)
        init_db(conn)
        conn.execute("UPDATE owned_games SET igdb_id = NULL")
        conn.commit()
        conn.close()
        print("recheck: cleared igdb_id on all rows — re-matching with exact-name preference")

    igdb = IgdbClient(os.environ["IGDB_CLIENT_ID"], os.environ["IGDB_CLIENT_SECRET"])
    ctx = build_op_context(resources={"db_url": args.db, "igdb": igdb})
    assets.igdb_matches(ctx)
    assets.game_metadata(ctx)
    assets.catalog_views(ctx)

    conn = connect(args.db)
    init_db(conn)
    print("\n================ IGDB ENRICHMENT REPORT ================")
    print(f"{'format':10s} {'owned':>6s} {'matched':>8s} {'unmatched':>10s}")
    for fmt in ("digital", "physical"):
        owned = conn.execute("SELECT COUNT(*) n FROM owned_games WHERE is_owned=1 AND format=?", (fmt,)).fetchone()["n"]
        matched = conn.execute("SELECT COUNT(*) n FROM owned_games WHERE is_owned=1 AND format=? AND igdb_id IS NOT NULL", (fmt,)).fetchone()["n"]
        print(f"{fmt:10s} {owned:6d} {matched:8d} {owned - matched:10d}")
    total = conn.execute("SELECT COUNT(*) n FROM owned_games WHERE is_owned=1").fetchone()["n"]
    matched_total = conn.execute("SELECT COUNT(*) n FROM owned_games WHERE is_owned=1 AND igdb_id IS NOT NULL").fetchone()["n"]
    print(f"{'TOTAL':10s} {total:6d} {matched_total:8d} {total - matched_total:10d}  ({100 * matched_total / max(total, 1):.1f}% matched)")
    print(f"\nmetadata fetched: {conn.execute('SELECT COUNT(*) n FROM game_metadata').fetchone()['n']}")
    print("\n-- unmatched (first 25) --")
    for r in conn.execute(
        "SELECT title, platform, format, psn_content_id FROM owned_games WHERE is_owned=1 AND igdb_id IS NULL ORDER BY title LIMIT 25"
    ).fetchall():
        print(f"   [{r['format'][:8]:8s}] {r['title'][:48]:48s} {str(r['platform'])[:12]:12s} {str(r['psn_content_id'] or '')[:22]}")
    print("=========================================================")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
