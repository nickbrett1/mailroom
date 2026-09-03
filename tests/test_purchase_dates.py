"""purchase_date_backfill tests: date purchased games from receipt/email date.

Covers: fixing stale parsed purchases (email-date fallback that predates
ingestion), order-number matching (physical), psn_api-only digitals matched to
a paid PSN receipt, and the scope guards ($0 claims never date a purchase;
already-dated / psplus_claimed rows untouched).
"""

from __future__ import annotations

import tempfile

from mailroom.db import connect, init_db
from mailroom.verticals.game_catalog.purchase_dates import backfill_purchase_dates


def _db():
    conn = connect(f"sqlite:///{tempfile.mkdtemp()}/p.db")
    init_db(conn)
    return conn


def _raw(conn, mid, source, received_at):
    conn.execute(
        "INSERT INTO raw_receipts(message_id, source, received_at) VALUES (?,?,?)",
        (mid, source, received_at),
    )


def _parsed(conn, source, order, title, platform, price, purchased_at, mid):
    conn.execute(
        """INSERT INTO parsed_purchases
           (source, order_number, item_key, purchased_at, title, platform, price, message_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (source, order, f"{order}:0", purchased_at, title, platform, price, mid),
    )


def _owned(conn, title, platform, fmt, order, source, acq=None):
    conn.execute(
        """INSERT INTO owned_games
           (title, normalized_title, platform, format, ownership_class,
            acquisition_date, order_number, source, is_owned)
           VALUES (?,?,?,?, 'purchased', ?,?,?, 1)""",
        (title, title.lower(), platform, fmt, acq, order, source),
    )


def test_physical_backfilled_from_email_date():
    conn = _db()
    _raw(conn, "m1", "amazon", "2025-02-10")
    _parsed(conn, "amazon", "AMZ1", "Animal Well", "playstation 5", "$29.99", None, "m1")
    _owned(conn, "Animal Well", "playstation 5", "physical", "AMZ1", "amazon")
    report = backfill_purchase_dates(conn)
    assert report["stale_parsed_fixed"] == 1
    assert report["dated"] == 1
    d = conn.execute(
        "SELECT acquisition_date FROM owned_games WHERE title='Animal Well'"
    ).fetchone()["acquisition_date"]
    assert d == "2025-02-10"  # the email received date
    conn.close()


def test_psn_api_only_digital_matched_to_paid_receipt():
    conn = _db()
    _raw(conn, "m2", "psn_receipt", "2024-06-14")
    _parsed(conn, "psn_receipt", "P1", "ASTRO BOT", "playstation 5", "$69.99", "2024-06-14", "m2")
    _owned(conn, "ASTRO BOT", "playstation 5", "digital", None, "psn_api")
    report = backfill_purchase_dates(conn)
    assert report["dated"] == 1
    d = conn.execute(
        "SELECT acquisition_date FROM owned_games WHERE title='ASTRO BOT'"
    ).fetchone()["acquisition_date"]
    assert d == "2024-06-14"
    conn.close()


def test_zero_dollar_claim_never_dates_a_purchase():
    conn = _db()
    _parsed(conn, "psn_receipt", "P2", "Stray", "playstation 4", "$0.00", "2022-07-05", "m3")
    _owned(conn, "Stray", "playstation 4", "digital", None, "psn_api")
    report = backfill_purchase_dates(conn)
    assert report["dated"] == 0
    conn.close()


def test_already_dated_and_scope_guards():
    conn = _db()
    # Already-dated purchase -> untouched.
    _owned(conn, "Dated", "playstation 5", "digital", "AMZ9", "amazon", acq="2021-01-01")
    # A psplus_claimed row is never dated by this backfill.
    conn.execute(
        """INSERT INTO owned_games(title, normalized_title, platform, format,
            ownership_class, acquisition_date, order_number, source, is_owned)
           VALUES ('Claimed', 'claimed', 'playstation 4', 'digital',
                   'psplus_claimed', NULL, NULL, 'psn_api', 1)"""
    )
    report = backfill_purchase_dates(conn)
    assert report["dated"] == 0
    d = conn.execute("SELECT acquisition_date FROM owned_games WHERE title='Dated'").fetchone()["acquisition_date"]
    assert d == "2021-01-01"
    conn.close()
