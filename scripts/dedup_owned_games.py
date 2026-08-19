"""Run the owned_games dedup (idempotent) + print the merge report.

Dedupes duplicate owned_games rows by IGDB match (memos/catalog-dedup-fix):
matched rows merge on (igdb_id, platform, format), unmatched on
(normalized_title, platform, format); winners keep every provenance, losers
are retired (is_owned=0 + retire_reason), never deleted. Also creates the
partial unique index guard (via init_db) on first run.

Usage:
    python3 scripts/dedup_owned_games.py [--db sqlite:///path.db]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from mailroom.db import connect, init_db
from mailroom.verticals.game_catalog.dedup import dedupe_owned_games


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MAILROOM_DB_URL", "sqlite:////data/mailroom.db"))
    args = ap.parse_args()

    conn = connect(args.db)
    # Run the dedup FIRST so the report shows the merge, then init_db (which
    # migrates the retire_reason column and creates the dedup guard index —
    # its own internal dedup pass then has nothing left to do). A fresh store
    # has no owned_games table yet — fall back to init_db, then dedup.
    try:
        report = dedupe_owned_games(conn)
    except sqlite3.OperationalError:
        conn.rollback()
        init_db(conn)  # fresh store: schema + index (empty dedup)
        report = dedupe_owned_games(conn)
    else:
        init_db(conn)

    print("\n================ DEDUP REPORT ================")
    print(f"groups merged : {report.groups}")
    print(f"rows retired  : {report.retired}")
    for w in sorted(report.winners, key=lambda w: -w["merged"]):
        print(f"  merged {w['merged']} rows -> game_id {w['winner_id']}: {w['title']}")
    if report.review_flags:
        print(f"\nreview flags ({len(report.review_flags)}) — possible double purchases:")
        for f in report.review_flags:
            print(f"  {f['title']}: orders {f['orders']} -> winner game_id {f['winner_id']}")
    print("=============================================")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
