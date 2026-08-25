"""Walmart parser tests, based on a real arrival email from the archive
(msgvault 2026-08-25: msg 36576, order 2000114-00769572 — the Star Wars Jedi:
Survivor PS5 receipt the id-cursor miss left out of the catalog)."""

from __future__ import annotations

from pathlib import Path

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.walmart import parse_walmart_receipt

HERE = Path(__file__).parent

ARRIVAL_36576 = (HERE / "fixtures" / "walmart_36576_body.txt").read_text()


def test_walmart_arrival_parses_star_wars_jedi_ps5():
    p = parse_walmart_receipt(ARRIVAL_36576, message_id="36576")
    assert p is not None
    assert p.source == "walmart"
    assert p.order_number == "2000114-00769572"
    assert p.purchased_at == "Nov 26, 2023"
    assert p.total == "$30.00"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "Star Wars Jedi: Survivor - PlayStation 5"
    assert item.price == "$30.00"
    assert item.qty == 1
    c = classify_item(item.title, platform_hint=item.platform_hint)
    assert c.classification == "playstation_game"
    assert c.platform == "playstation 5"


def test_unrelated_body_returns_none():
    assert parse_walmart_receipt("some random email about nothing") is None


def test_walmart_tracking_email_returns_none():
    # "Shipped: items from order #..." carries no item price -> no facts.
    body = """Your package is on its way
Order date: Mon, Nov 27, 2023
Order number: 2000114-00769572
Star Wars Jedi: Survivor - PlayStation 5
Qty: 1
"""
    assert parse_walmart_receipt(body) is None
