"""Targeted receipt-backfill asset tests (backfill_missing_receipts).

Backfills specific msgvault message ids into raw_receipts so missed games
re-enter the owned catalog without a full history re-ingest. Source is inferred
from the message sender.
"""

from __future__ import annotations

import tempfile

from dagster import build_op_context

from mailroom.db import connect, init_db
from mailroom.verticals.game_catalog import assets


class _FakeMsgvault:
    """In-memory msgvault: returns a canned message per id."""

    def __init__(self, messages: dict[int, dict]):
        self._messages = messages

    def get_message(self, message_id: int) -> dict:
        return self._messages[int(message_id)]


def _ctx(db: str, msgvault):
    return build_op_context(
        resources={"db_url": f"sqlite:///{db}", "msgvault": msgvault, "igdb": object()},
        op_config={"message_ids": list(msgvault._messages)},
    )


def _seed(db: str, rows: list[tuple[str, str, str, str, str, str]]) -> None:
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    for r in rows:
        conn.execute(
            """INSERT INTO raw_receipts(message_id, source, subject, sender, received_at, body)
               VALUES (?, ?, ?, ?, ?, ?)""",
            r,
        )
    conn.commit()
    conn.close()


def test_backfill_ingests_amazon_delivery_estimate():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    conn.close()
    fake = _FakeMsgvault(
        {
            65274: {
                "id": 65274,
                "subject": "Delivery estimate update for your Amazon.com order #111-7703809-7385008",
                "from_email": "order-update@amazon.com",
                "sent_at": "2021-05-02T14:28:34Z",
                "body_text": "Order #111-7703809-7385008\nPlaced on  Thursday, April 29, 2021\n\n        Uncharted: Nathan Drake Collection Hits - PlayStation 4\n        Sold by Amazon.com Services LLC",
                "body_html": "",
            }
        }
    )
    assets.backfill_missing_receipts(_ctx(db, fake))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM raw_receipts WHERE message_id='65274'").fetchone()
    assert row is not None
    assert row["source"] == "amazon"
    assert row["received_at"] == "2021-05-02T14:28:34Z"
    assert "Uncharted: Nathan Drake Collection" in row["body"]
    conn.close()


def test_backfill_gamestop_order_confirmation():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    conn.close()
    fake = _FakeMsgvault(
        {
            42957: {
                "id": 42957,
                "subject": "Thank you for your order!",
                "from_email": "notifications@info.gamestop.com",
                "sent_at": "2023-06-23T01:51:22Z",
                "body_text": "Thank you for your order, Nicholas\nOrder Number: 1100000059461018\nOrder Date: 6/22/23\nSHIP TO HOME\nShipping to 80 RIVERSIDE BLVD\nReturnal - PlayStation 5\nQTY: 1\n$19.99\nORDER SUMMARY",
                "body_html": "",
            }
        }
    )
    assets.backfill_missing_receipts(_ctx(db, fake))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM raw_receipts WHERE message_id='42957'").fetchone()
    assert row is not None
    assert row["source"] == "gamestop"
    conn.close()


def _known_ctx(db: str, items: list[dict]):
    return build_op_context(
        resources={"db_url": f"sqlite:///{db}", "msgvault": object(), "igdb": object()},
        op_config={"items": items},
    )


def test_record_known_order_items_flows_to_owned():
    """Games from anonymized receipts (no item lines) can be recorded and flow
    through parsed -> classified -> owned as physical amazon games."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    conn.close()
    items = [
        {"source": "amazon", "order_number": "111-0336555-9833027", "title": "Hades - PlayStation 4",
         "platform": "playstation 4", "price": "$24.99", "acquisition_date": "2021-11-21T04:08:56Z", "note": "from $65.06 order"},
        {"source": "amazon", "order_number": "111-0336555-9833027", "title": "The Last of Us Part II - PlayStation 4",
         "platform": "playstation 4", "acquisition_date": "2021-11-21T04:08:56Z"},
    ]
    assets.record_known_order_items(_known_ctx(db, items))
    # durable record + parsed feed
    conn = connect(f"sqlite:///{db}")
    k = conn.execute("SELECT COUNT(*) n FROM known_order_items").fetchone()["n"]
    p = conn.execute("SELECT COUNT(*) n FROM parsed_purchases").fetchone()["n"]
    assert k == 2 and p == 2
    conn.close()

    # downstream chain
    assets.classified_game_items(_known_ctx(db, []))
    assets.owned_games(_known_ctx(db, []))
    conn = connect(f"sqlite:///{db}")
    rows = conn.execute(
        "SELECT title, format, retailer, order_number, acquisition_date FROM owned_games WHERE is_owned=1 ORDER BY title"
    ).fetchall()
    titles = {r["title"] for r in rows}
    assert "Hades - PlayStation 4" in titles
    assert "The Last of Us Part II - PlayStation 4" in titles
    for r in rows:
        assert r["format"] == "physical"
        assert r["retailer"] == "amazon"
        assert r["order_number"] == "111-0336555-9833027"
    conn.close()


def test_record_known_order_items_is_idempotent():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    conn.close()
    item = [{"source": "amazon", "order_number": "o1", "title": "Hades - PlayStation 4", "platform": "playstation 4"}]
    assets.record_known_order_items(_known_ctx(db, item))
    assets.record_known_order_items(_known_ctx(db, item))
    conn = connect(f"sqlite:///{db}")
    assert conn.execute("SELECT COUNT(*) n FROM known_order_items").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM parsed_purchases").fetchone()["n"] == 1
    conn.close()


def test_backfill_skips_unknown_sender_and_is_idempotent():
    db = tempfile.mktemp(suffix=".db")
    _seed(db, [("100", "amazon", "s", "order-update@amazon.com", "2021-01-01T00:00:00Z", "body")])
    fake = _FakeMsgvault(
        {
            100: {"id": 100, "subject": "s", "from_email": "order-update@amazon.com", "sent_at": "2021-01-01T00:00:00Z", "body_text": "body", "body_html": ""},
            999: {"id": 999, "subject": "x", "from_email": "someone@else.com", "sent_at": "2020-01-01T00:00:00Z", "body_text": "b", "body_html": ""},
        }
    )
    assets.backfill_missing_receipts(_ctx(db, fake))
    conn = connect(f"sqlite:///{db}")
    rows = conn.execute("SELECT message_id, source FROM raw_receipts").fetchall()
    assert [tuple(r) for r in rows] == [("100", "amazon")]  # unknown sender skipped, existing preserved
    conn.close()
