"""essentials_lineup / essentials_claim_dates backfill tests.

Covers the acquisition_date backfill safety rules (memos/
psplus-essentials-acquisition-date-backfill): only digital psplus_claimed rows
that appear in the lineup get a date; freebies/demos/Extra rows stay undated and
are reported; physical / purchased / psplus_extra rows are never touched; the
run is idempotent.
"""

from __future__ import annotations

import tempfile

from mailroom.db import connect, init_db
from mailroom.verticals.game_catalog.essentials import enrich_psplus_claim_dates


def _db():
    conn = connect(f"sqlite:///{tempfile.mkdtemp()}/test.db")
    init_db(conn)
    return conn


def _owned(conn, title, platform, ownership, fmt="digital", date=None, igdb=None):
    conn.execute(
        """INSERT INTO owned_games
           (title, normalized_title, platform, format, ownership_class,
            acquisition_date, igdb_id, source, is_owned)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'test', 1)""",
        (title, title.lower(), platform, fmt, ownership, date, igdb),
    )
    conn.commit()


def _lineup(conn, title, platform, available_from, igdb=None):
    conn.execute(
        """INSERT INTO essentials_lineup
           (month, title, normalized_title, platform, available_from, available_to, igdb_id, source)
           VALUES (?, ?, ?, ?, ?, NULL, ?, 'test')""",
        ("2024-08", title, title.lower(), platform, available_from, igdb),
    )
    conn.commit()


def test_backfill_dates_only_real_monthly_claims():
    conn = _db()
    # A genuine Essentials monthly claim that IS in the lineup -> gets dated.
    _owned(conn, "Stray", "playstation 4", "psplus_claimed")
    _lineup(conn, "Stray", "playstation 4", "2022-07-05")
    # A freebie/demo that is psplus_claimed but was never an Essentials monthly.
    _owned(conn, "The Elder Scrolls Online", "playstation 4", "psplus_claimed")
    # An already-dated claim -> untouched.
    _owned(conn, "It Takes Two", "playstation 4", "psplus_claimed", date="2022-04-05")

    # Outside scope: physical, purchased, psplus_extra must never be dated.
    _owned(conn, "Sackboy: A Big Adventure", "playstation 4", "psplus_claimed", fmt="physical")
    _owned(conn, "God of War", "playstation 4", "purchased", fmt="physical")
    _owned(conn, "Returnal", "playstation 5", "psplus_extra")

    report = enrich_psplus_claim_dates(conn)
    assert report == {"dated": 1, "already_dated": 1, "unmatched": 1}

    # Stray got the structural first-Tuesday date; nothing else did.
    stray = conn.execute(
        "SELECT acquisition_date FROM owned_games WHERE title = 'Stray'"
    ).fetchone()
    assert stray["acquisition_date"] == "2022-07-05"
    for title in ("The Elder Scrolls Online", "Sackboy: A Big Adventure",
                  "God of War", "Returnal", "It Takes Two"):
        val = conn.execute(
            "SELECT acquisition_date FROM owned_games WHERE title = ?", (title,)
        ).fetchone()
        if title == "It Takes Two":
            assert val["acquisition_date"] == "2022-04-05"
        else:
            assert val["acquisition_date"] is None

    # The unmatched claim was reported (never silently dropped).
    flagged = conn.execute(
        "SELECT * FROM review_queue WHERE reason = 'psplus_claimed_no_essentials_month'"
    ).fetchall()
    assert len(flagged) == 1
    assert flagged[0]["title"] == "The Elder Scrolls Online"

    # Idempotent: second run dates nothing new.
    report2 = enrich_psplus_claim_dates(conn)
    assert report2 == {"dated": 0, "already_dated": 2, "unmatched": 1}
    conn.close()


def test_platform_must_match_to_date():
    conn = _db()
    # Claimed the PS5 copy, but the lineup only has the PS4 row -> no match.
    _owned(conn, "Stray", "playstation 5", "psplus_claimed")
    _lineup(conn, "Stray", "playstation 4", "2022-07-05")
    report = enrich_psplus_claim_dates(conn)
    assert report == {"dated": 0, "already_dated": 0, "unmatched": 1}
    conn.close()


def test_igdb_join_overrides_normalization_drift():
    """The igdb_id + platform join dates a row even when the stored
    normalized_title drifted from the lineup's (real-world: source-string
    differences like 'minecraft: legends' vs 'minecraft legends')."""
    conn = _db()
    # Stored row: normalized_title has a colon the lineup title lacks.
    _owned(conn, "Minecraft Legends", "playstation 4", "psplus_claimed", igdb=307731)
    conn.execute(
        "UPDATE owned_games SET normalized_title = 'minecraft: legends' WHERE title = 'Minecraft Legends'"
    )
    conn.commit()
    _lineup(conn, "Minecraft Legends", "playstation 4", "2024-04-02", igdb=307731)
    report = enrich_psplus_claim_dates(conn)
    assert report == {"dated": 1, "already_dated": 0, "unmatched": 0}
    val = conn.execute(
        "SELECT acquisition_date FROM owned_games WHERE title = 'Minecraft Legends'"
    ).fetchone()
    assert val["acquisition_date"] == "2024-04-02"
    conn.close()
