"""acquisition_date enhancement tests: physical purchases store the email
received date as the acquisition date (fallback when the parser has none),
and owned_games threads it through via the parsed_purchases join."""

from __future__ import annotations

import tempfile

from dagster import build_op_context
from tests.test_parsers_amazon import (
    ORDERED_SINGLE as AMAZON_CONFIRMATION,  # real archived body fixture
)

from mailroom.db import connect, init_db, upsert_owned_game
from mailroom.verticals.game_catalog import assets


def _ctx(db):
    return build_op_context(resources={"db_url": f"sqlite:///{db}", "msgvault": object(), "igdb": object()})


def test_physical_parse_falls_back_to_email_date():
    """A physical receipt whose parser has no purchase date gets the email
    received_at as purchased_at (the acquisition date)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    conn.execute(
        """INSERT INTO raw_receipts(message_id, source, subject, sender, received_at, body, body_html)
           VALUES ('9001', 'amazon', 'Ordered: Sonic Superstars', 'auto-confirm@amazon.com',
                   '2025-01-13T18:30:00Z', ?, '')""",
        (AMAZON_CONFIRMATION,),
    )
    conn.commit()
    conn.close()

    assets.parsed_purchases_physical(_ctx(db))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT purchased_at FROM parsed_purchases WHERE source='amazon'").fetchone()
    assert row is not None
    assert row["purchased_at"] == "2025-01-13T18:30:00Z"
    conn.close()


def test_owned_games_stores_acquisition_date():
    """owned_games sets acquisition_date from the parsed purchase date."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    upsert_owned_game(
        conn,
        {
            "title": "Cyberpunk 2077 - PlayStation 4",
            "normalized_title": assets.normalize_title("Cyberpunk 2077 - PlayStation 4"),
            "platform": "playstation 4",
            "format": "physical",
            "ownership_class": "purchased",
            "retailer": "gamestop",
            "order_number": "o1",
            "item_id": None,
            "condition": None,
            "psn_content_id": None,
            "igdb_id": None,
            "acquisition_date": None,
            "price": None,
            "source": "gamestop",
            "source_ref": "o1:Cyberpunk 2077 - PlayStation 4",
            "status": "owned",
            "is_owned": 1,
            "provenance": "gamestop:o1:Cyberpunk 2077 - PlayStation 4",
        },
    )
    conn.execute(
        """INSERT INTO parsed_purchases(source, order_number, item_key, purchased_at, title, platform)
           VALUES ('gamestop', 'o1', 'o1:Cyberpunk 2077 - PlayStation 4', '2021-01-15T09:00:00Z', 'Cyberpunk 2077 - PlayStation 4', 'playstation 4')"""
    )
    conn.execute(
        """INSERT INTO classified_game_items(source, order_number, item_key, title, platform, classification, reason)
           VALUES ('gamestop', 'o1', 'o1:Cyberpunk 2077 - PlayStation 4', 'Cyberpunk 2077 - PlayStation 4', 'playstation 4', 'playstation_game', 'platform match')"""
    )
    conn.commit()
    conn.close()

    assets.owned_games(_ctx(db))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT acquisition_date FROM owned_games WHERE title LIKE 'Cyberpunk 2077%'").fetchone()
    assert row["acquisition_date"] == "2021-01-15T09:00:00Z"
    conn.close()
