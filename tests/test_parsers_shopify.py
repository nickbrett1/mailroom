"""Shopify-generic parser tests (LRG / LRG SOHO / Atari style).

The Gmail archive (2026-08-16) has no LRG/Atari Shopify confirmation emails
(those orders flow through the Shop app), so these samples are built from the
memo's documented template + canonical Shopify layouts: item + variant line =
platform, with books/merch excluded by the classifier.
"""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.shopify import parse_shopify_receipt

VARIANT_LINE_LAYOUT = """
Thanks for your order!

Hi Nick,

Order #12345
Placed on: March 3, 2023

Hades - Standard Edition
PS5
Qty: 1
$34.99

Celeste
Switch
Qty: 1
$24.99

Subtotal $59.98
Tax $5.10
Total $65.08

View your order
https://shop.example.com/orders/12345
"""

BULLET_INLINE_LAYOUT = """
Order confirmed

Hi Nick,

Order # 2944075
Date: 07/05/2024

* Celeste - PS5
  Quantity: 1
  $34.99

* The Art of Castlevania: Symphony of the Night
  Quantity: 1
  $49.99

Subtotal: $84.98
Tax: $7.56
Total: $92.54
"""

FULFILLED_EMAIL = """
Your order has been fulfilled!

Hi Nick,

Order #12345

Celeste - PS5
Qty: 1

Track your package
https://shop.example.com/tracking/abc
"""


def test_variant_line_sets_platform():
    p = parse_shopify_receipt(VARIANT_LINE_LAYOUT, message_id="s1")
    assert p is not None
    assert p.source == "shopify"
    assert p.order_number == "12345"
    assert p.purchased_at == "March 3, 2023"
    assert len(p.items) == 2
    hades, celeste = p.items
    assert hades.title == "Hades - Standard Edition"
    assert hades.platform_hint == "ps5"  # variant line below the title
    assert hades.price == "$34.99"
    assert celeste.title == "Celeste"
    assert celeste.platform_hint == "switch"
    assert p.subtotal == "$59.98"
    assert p.tax == "$5.10"
    assert p.total == "$65.08"


def test_bullet_inline_variant_and_book_exclusion():
    p = parse_shopify_receipt(BULLET_INLINE_LAYOUT, message_id="s2")
    assert p is not None
    assert p.order_number == "2944075"
    assert p.purchased_at == "07/05/2024"
    assert len(p.items) == 2
    game, book = p.items
    assert game.title == "Celeste - PS5"
    assert game.platform_hint == "ps5"
    assert game.price == "$34.99"
    # LRG book -> excluded by the classifier (never catalogued).
    assert book.title == "The Art of Castlevania: Symphony of the Night"
    assert classify_item(book.title).classification == "needs_review"
    assert classify_item(game.title).classification == "playstation_game"


def test_fulfilled_email_returns_none():
    assert parse_shopify_receipt(FULFILLED_EMAIL, message_id="s3") is None


def test_unrelated_body_returns_none():
    assert parse_shopify_receipt("some random email") is None
