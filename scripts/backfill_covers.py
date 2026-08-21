"""Backfill: fetch + cache IGDB cover images for every game with a cover URL.

One-time pass (also safe to re-run — idempotent) that populates
/data/covers/<image_id>.jpg for all current titles, so pshelf can serve covers
as static files without a live IGDB proxy (memos/covers-caching-design).

Usage:
    doppler run --project mailroom --config dev -- \
        python3 scripts/backfill_covers.py --db sqlite:////tmp/merged4.db
"""

from __future__ import annotations

import argparse
import os
import sys

from dagster import build_op_context

from mailroom.clients import IgdbClient
from mailroom.verticals.game_catalog import assets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MAILROOM_DB_URL", "sqlite:////data/mailroom.db"))
    ap.add_argument(
        "--covers-dir",
        default=os.environ.get("MAILROOM_COVERS_DIR", "/data/covers"),
        help="Directory to write cached cover images to.",
    )
    args = ap.parse_args()

    igdb = IgdbClient(os.environ["IGDB_CLIENT_ID"], os.environ["IGDB_CLIENT_SECRET"])
    os.environ["MAILROOM_COVERS_DIR"] = args.covers_dir
    ctx = build_op_context(resources={"db_url": args.db, "igdb": igdb})
    assets.game_covers(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
