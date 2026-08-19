"""owned_games dedup — collapse duplicate rows by IGDB match.

Per memos/catalog-dedup-fix: after enrichment the canonical key is
(igdb_id, platform) — plus format, since a digital AND a physical copy of the
same game legitimately coexist (the memo's open question; resolved: keep both
when format differs). Rows in the same (key, platform, format) bucket are
merged into a winner and the losers are RETIRED (is_owned=0 + retire_reason),
never deleted, so provenance is preserved everywhere.

Generic platform handling: receipt rows carry platform 'playstation' (the PSN
receipt parser emits no platform hint) while psn_api/ps_plus rows carry a
concrete one ('playstation 5'). Within an (igdb_id, format) group, a generic
row adopts the group's concrete platform when there is exactly ONE — so a
receipt merges into its API row. When the group has several concrete
platforms (Ragnarök PS4 + PS5), generic rows stay in their own bucket and the
group is flagged for review.

Wired into:
  - db.init_db (migration: dedupes existing duplicates, then the partial
    unique index idx_owned_games_dedup guards future inserts/updates),
  - the dedupe_owned_games asset (idempotent re-run after each enrichment
    pass), and
  - igdb_matches / manual_igdb_match (enrichment-time collapse).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from mailroom.db import merge_provenance

# Platforms that carry no real platform signal (receipt rows default to these).
_GENERIC_PLATFORMS = {None, "", "playstation", "ps"}

# Source authority for picking the winner row (memos/catalog-dedup-fix):
# psn_api (has content_id + authoritative platform) > earliest psn_receipt
# > ps_plus > everything else (cdkeys/gameflip/retailers).
_SOURCE_RANK = {"psn_api": 0, "psn_receipt": 1, "ps_plus": 2}

# ownership_class precedence: purchased > psplus_claimed > psplus_extra
_CLASS_RANK = {"purchased": 0, "psplus_claimed": 1, "psplus_extra": 2}


@dataclass
class DedupReport:
    groups: int = 0
    retired: int = 0
    winners: list[dict] = field(default_factory=list)  # {winner_id, title, merged}
    review_flags: list[dict] = field(default_factory=list)


def _source_rank(source: str | None) -> int:
    return _SOURCE_RANK.get(source or "", 3)


def _winner_key(row: dict[str, Any]) -> tuple:
    """Winner = lowest rank, then earliest acquisition_date (nulls last), then
    lowest id (stable)."""
    return (_source_rank(row.get("source")), row.get("acquisition_date") or "9999-12-31", row["id"])


def _platform_bucket(row: dict[str, Any], concrete_platforms: set[str]) -> str | None:
    """Effective platform for bucketing: a generic row adopts the group's only
    concrete platform; otherwise it keeps its own (stays separate)."""
    p = row["platform"]
    if p in _GENERIC_PLATFORMS and len(concrete_platforms) == 1:
        return next(iter(concrete_platforms))
    return p


def merge_group(conn, rows: Iterable[dict | Any]) -> int:
    """Merge `rows` (one dedup bucket = the same game) into a winner row;
    retire the losers. Returns the winner's owned_games id.

    Field rules (memos/catalog-dedup-fix):
      - provenance becomes a list of EVERY source (JSON array, winner first);
      - acquisition_date = earliest non-null; price = winner's, else first;
      - ownership_class = purchased > psplus_claimed > psplus_extra;
      - psn_content_id = winner's, concatenating any additional distinct ids;
      - platform = a concrete platform when any row has one;
      - igdb_id = COALESCE(winner, any row's).
    Losers: is_owned=0, status='retired', retire_reason='dup_merged:game_id=<winner>'.
    """
    rows = [dict(r) for r in rows]
    winner = min(rows, key=_winner_key)
    losers = [r for r in rows if r["id"] != winner["id"]]

    ids: list[str] = []
    for r in rows:
        cid = r.get("psn_content_id")
        if cid and cid not in ids:
            ids.append(cid)
    igdb_id = next((r.get("igdb_id") for r in rows if r.get("igdb_id")), None)
    platform = next(
        (r["platform"] for r in rows if r["platform"] not in _GENERIC_PLATFORMS),
        winner["platform"],
    )
    # Retire the losers FIRST: the partial dedup index is over
    # (igdb_id, platform, format) WHERE is_owned=1 — if the winner is the
    # incoming row (e.g. a psn_api row matched after its receipt row) its
    # igdb_id update would collide with the still-owned loser, so the losers
    # must leave the index before the winner claims the key.
    for r in losers:
        conn.execute(
            """UPDATE owned_games SET
                 is_owned = 0, status = 'retired',
                 retire_reason = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (f"dup_merged:game_id={winner['id']}", r["id"]),
        )
    conn.execute(
        """UPDATE owned_games SET
             igdb_id = COALESCE(igdb_id, ?),
             platform = ?,
             acquisition_date = ?,
             price = ?,
             ownership_class = ?,
             psn_content_id = ?,
             provenance = ?,
             updated_at = datetime('now')
           WHERE id = ?""",
        (
            igdb_id,
            platform,
            min((r["acquisition_date"] for r in rows if r["acquisition_date"]), default=None),
            winner["price"] or next((r["price"] for r in rows if r["price"]), None),
            min(rows, key=lambda r: _CLASS_RANK.get(r.get("ownership_class"), 0))["ownership_class"],
            ",".join(ids) or None,
            merge_provenance(*[r["provenance"] for r in rows]),
            winner["id"],
        ),
    )
    return winner["id"]


def _flag_possible_double_purchase(
    conn, rows: list[dict], *, winner_id: int, key: Any, fmt: str, report: DedupReport
) -> None:
    """Distinct order numbers among receipt-derived rows = either a genuine
    double purchase or a same-purchase re-parse — flag for the user, never
    silently merged away (Witcher 3 case, memos/catalog-dedup-fix)."""
    orders = sorted({r["order_number"] for r in rows if r["order_number"]})
    if len(orders) < 2:
        return
    payload = json.dumps(
        {
            "group_key": str(key),
            "format": fmt,
            "orders": orders,
            "row_ids": [r["id"] for r in rows],
            "winner_id": winner_id,
            "hint": "distinct order numbers — genuine double purchase or same-purchase re-parse? check before trusting the merge",
        },
        sort_keys=True,
    )
    conn.execute(
        """INSERT OR IGNORE INTO review_queue(source, order_number, title, reason, payload)
           VALUES ('dedup', ?, ?, 'possible_double_purchase', ?)""",
        (str(key), rows[0]["title"], payload),
    )
    report.review_flags.append(
        {"title": rows[0]["title"], "orders": orders, "winner_id": winner_id, "reason": "possible_double_purchase"}
    )


def dedupe_owned_games(conn) -> DedupReport:
    """Idempotent dedup pass over owned_games.

    Matched rows (igdb_id set) bucket on (igdb_id, format); unmatched rows on
    (normalized_title, format). Within a bucket, generic-platform rows adopt
    the bucket's single concrete platform, then each (platform) subgroup with
    >1 row is merged (winner + retire). Idempotent: merged groups collapse to
    one owned row, so a re-run has nothing left to do.
    """
    report = DedupReport()
    for matched in (True, False):
        cond = "igdb_id IS NOT NULL" if matched else "igdb_id IS NULL"
        rows = conn.execute(
            f"SELECT * FROM owned_games WHERE is_owned = 1 AND {cond}"
        ).fetchall()
        buckets: dict[tuple, list[dict]] = defaultdict(list)
        for r in rows:
            key = r["igdb_id"] if matched else r["normalized_title"]
            buckets[(key, r["format"])].append(dict(r))
        for (key, fmt), group in buckets.items():
            concrete = {r["platform"] for r in group if r["platform"] not in _GENERIC_PLATFORMS}
            subgroups: dict[str | None, list[dict]] = defaultdict(list)
            for r in group:
                subgroups[_platform_bucket(r, concrete)].append(r)
            for plat, sub in subgroups.items():
                if len(sub) < 2:
                    continue
                winner_id = merge_group(conn, sub)
                _flag_possible_double_purchase(conn, sub, winner_id=winner_id, key=key, fmt=fmt, report=report)
                report.groups += 1
                report.retired += len(sub) - 1
                report.winners.append(
                    {"winner_id": winner_id, "title": sub[0]["title"], "merged": len(sub), "platform": plat}
                )
    conn.commit()
    return report
