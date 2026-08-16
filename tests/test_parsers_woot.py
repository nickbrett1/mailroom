"""Woot parser tests, from real confirmation bodies in the archive
(msgvault 2026-08-16: orders 212839495 / 209228559)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.woot import parse_woot_receipt

SINGLE_ITEM = """-------------------------------------------------------------------------------
Order Confirmation: #212839495 | Woot says thank you for your order
-------------------------------------------------------------------------------

Hey Wooter16424892,

Thanks for your purchase! Further details on your order are below.

Woot

-----------------------------------
ORDER DETAILS
-----------------------------------
Order Date: Wednesday, August 5, 2026
Order Number: 212839495

Estimated delivery date: Wednesday, August 12, 2026 
8BitDo 64 Bluetooth Controller
$34.99  $44.99 22% off Reference Price
Sold by Woot LLC 
Condition: New 
Quantity: 1 
Item Subtotal $34.99 

Subtotal: $34.99
Shipping: $0.00
Tax: $3.11
Total: $38.10

-----------------------------------
SHIPPING & PAYMENT
-----------------------------------
"""

TWO_GAMES = """Order Confirmation: #209228559 | Woot says thank you for your order

ORDER DETAILS
-----------------------------------
Order Date: Saturday, January 31, 2026
Order Number: 209228559

Estimated delivery date: Saturday, February 7, 2026 
Sonic X Shadow Generations (PS5)
$14.99  $49.99 70% off Reference Price
Sold by Woot LLC 
Condition: New 
Model: Playstation 5 
Quantity: 1 
Item Subtotal $14.99 
Syberia Remastered
$14.99  $39.99 63% off Reference Price
Sold by Woot LLC 
Condition: New 
Model: Playstation 5 
Quantity: 1 
Item Subtotal $14.99 

Subtotal: $29.98
Shipping: $0.00
Tax: $2.66
Total: $32.64
"""

SHIPPED = """Woo hoo! Your Woot order's a-comin'! #212839495

Your Woot order is on its way!

Order #212839495
8BitDo 64 Bluetooth Controller
Tracking: 9400111202552014046464
"""

DELIVERED = """Rejoice! Your Package Has Been Delivered!

Your Woot order has arrived!

Order #212839495
"""


def test_single_item():
    p = parse_woot_receipt(SINGLE_ITEM, message_id="121509")
    assert p is not None
    assert p.source == "woot"
    assert p.order_number == "212839495"
    assert p.purchased_at == "Wednesday, August 5, 2026"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "8BitDo 64 Bluetooth Controller"
    assert item.price == "$34.99"  # first $ on the line; reference price ignored
    assert item.qty == 1
    assert item.condition == "New"
    assert p.subtotal == "$34.99"
    assert p.tax == "$3.11"
    assert p.total == "$38.10"
    # A Bluetooth controller is hardware — excluded from the catalog.
    assert classify_item(item.title, platform_hint=item.platform_hint).classification == "accessory_hardware"


def test_two_games_with_model_platform():
    p = parse_woot_receipt(TWO_GAMES, message_id="7306")
    assert p is not None
    assert p.order_number == "209228559"
    assert len(p.items) == 2
    sonic, syberia = p.items
    assert sonic.title == "Sonic X Shadow Generations (PS5)"
    assert sonic.platform_hint == "ps5"  # from the '(PS5)' suffix
    assert sonic.price == "$14.99"
    # No '(PS5)' suffix — platform comes from the 'Model: Playstation 5' line.
    assert syberia.title == "Syberia Remastered"
    assert syberia.platform_hint == "Playstation 5"
    assert classify_item(syberia.title, platform_hint=syberia.platform_hint).classification == "playstation_game"
    assert p.total == "$32.64"


def test_shipped_returns_none():
    assert parse_woot_receipt(SHIPPED, message_id="121529") is None


def test_delivered_returns_none():
    assert parse_woot_receipt(DELIVERED, message_id="121068") is None


def test_unrelated_body_returns_none():
    assert parse_woot_receipt("some random email") is None
