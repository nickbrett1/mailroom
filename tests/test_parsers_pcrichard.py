"""P.C. Richard & Son parser tests, based on the real confirmation body from
the archive (msgvault: order 012-7135059, Star Wars Outlaws, 2025-10-26)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.pcrichard import parse_pcrichard_receipt

CONFIRMATION = """
Order Confirmation Summary

P.C. Richard & Son ( https://click.e.pcrichard.com/?qs=abc )

Kitchen Appliances ( https://click.e.pcrichard.com/?qs=def ) | TVs ( https://click.e.pcrichard.com/?qs=ghi )

Thank you for shopping at
P.C. Richard & Son! Order # 012-7135059

Thank you for shopping at P.C. Richard & Son. We are now processing your order.

Billing & Shipping

Shipping Address Nick Brett
80 Riverside Blvd
Apt 10c
New York, NY 10069

Payment Method: Apple Pay

Order Details *Order Number* Order Date 012-7135059 10/26/2025

Items

Star Wars Outlaws - PlayStation 5 ( https://click.e.pcrichard.com/?qs=39f4ee929 )

Star Wars Outlaws - PlayStation 5 ( https://click.e.pcrichard.com/?qs=a11fa4ac ) Model: 887256115920 Qty: 1 $14.00

The merchandise you have ordered is promised for delivery to you on or before Thu Oct 30, 2025.

Tax: $1.69 Shipping: $4.99 Total: $20.68

Kitchen Appliances ( https://click.e.pcrichard.com/?qs=jk )
"""

SHIPPED = """
Details Inside

P.C. Richard & Son ( https://click.e.pcrichard.com/?qs=xyz )

Your item has shipped. Order # 012-7135059

Shipping Details

Order Details *Order Number* Order Date 012-7135059 10/25/2025

Item(s)

Star Wars Outlaws - PlayStation 5 ( https://click.e.pcrichard.com/?qs=0f69f576 ) Model: 887256115920 Qty: 1
"""


def test_parses_confirmation_item_and_totals():
    p = parse_pcrichard_receipt(CONFIRMATION, message_id="14132")
    assert p is not None
    assert p.source == "pcrichard"
    assert p.order_number == "012-7135059"
    assert p.purchased_at == "10/26/2025"
    assert p.tax == "$1.69"
    assert p.total == "$20.68"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "Star Wars Outlaws - PlayStation 5"
    assert item.price == "$14.00"
    assert item.qty == 1


def test_item_classifies_as_playstation_5():
    p = parse_pcrichard_receipt(CONFIRMATION)
    c = classify_item(p.items[0].title, platform_hint=p.items[0].platform_hint)
    assert c.classification == "playstation_game"
    assert c.platform == "playstation 5"


def test_shipping_email_has_no_price_so_is_ignored():
    assert parse_pcrichard_receipt(SHIPPED, message_id="14109") is None


def test_non_pcrichard_body_is_ignored():
    assert parse_pcrichard_receipt("Order # 111-2223334 Thanks for your order") is None
