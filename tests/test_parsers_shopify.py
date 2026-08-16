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
    assert parse_shopify_receipt(body_text="some random email") is None


# --- Real bodies from the archive (msgvault 2026-08-16) ---

LRG_SOHO_REAL = """Thank you for your purchase!

*****************************************************************
Limited Run SOHO 
( https://limited-run-soho.myshopify.com )

*****************************************************************

Order #1005

----------------------------
Thank you for your purchase!
----------------------------

Visit our store 
( https://limited-run-soho.myshopify.com )


Order summary
-------------

Animal Well × 1

PS5

$39.99

Gameplay Harmonies × 1

$24.99

Plumbers Don't Wear Ties × 1

$19.99

Subtotal

$84.97

Taxes

$7.53

Total

$92.50 USD

Total paid today

$0.00 USD
"""

ATARI_REAL = """Thank you for your purchase!

Atari®

Order #165370

----------------------------
Thank you for your purchase!
----------------------------

We're getting your order ready and will send an email
notification when it has shipped.

View your order 
( https://atari.com/orders/abc )

Order summary
-------------

MY PLAY WATCH ARCADE SMARTWATCH BAND × 1

Asteroids Arcade Watch Band

39.99

Subtotal

39.99

Shipping

5.99

Taxes

$3.56

Total

$49.54
"""


def test_lrg_soho_real_receipt():
    p = parse_shopify_receipt(body_text=LRG_SOHO_REAL, message_id="27053")
    assert p is not None
    assert p.order_number == "1005"
    assert len(p.items) == 3
    well, harmonies, _plumbers = p.items
    assert well.title == "Animal Well"
    assert well.platform_hint == "ps5"
    assert well.price == "$39.99"
    assert well.qty == 1
    assert classify_item(well.title, platform_hint=well.platform_hint).classification == "playstation_game"
    # Soundtrack / non-game -> excluded by the classifier.
    assert classify_item(harmonies.title).classification == "needs_review"
    assert p.subtotal == "$84.97"
    assert p.tax == "$7.53"
    assert p.total == "$92.50"


def test_atari_real_receipt_bare_prices():
    p = parse_shopify_receipt(body_text=ATARI_REAL, message_id="3850")
    assert p is not None
    assert p.order_number == "165370"
    assert len(p.items) == 1
    band = p.items[0]
    # The sub-variant line ('Asteroids Arcade Watch Band') is absorbed into the
    # item; bare '39.99' price is normalized to $.
    assert band.title == "MY PLAY WATCH ARCADE SMARTWATCH BAND - Asteroids Arcade Watch Band"
    assert band.price == "$39.99"
    assert band.qty == 1
    assert p.subtotal == "$39.99"
    assert p.total == "$49.54"
