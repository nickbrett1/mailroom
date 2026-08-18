"""Larian merch-store parser tests (real archived bodies)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.larian import parse_larian_receipt

ORDER_CONFIRMATION = """
Order confirmation

***********
Greetings !
***********

Thanks for ordering Larian swag from our store!

Here is your order summary. Our imps will start working on putting it all together and we will teleport it to you as soon as it's ready!

--------
Summary:
--------

Shipping address
----------------

Nick Brett

address:
80 RIVERSIDE BLVD
APT 10C
10069, New York

Item: Qty: Price:

Baldur's Gate 3 - Deluxe Edition PS5

1 $79.99 Items total: $79.99 Taxes total: $0.00 Shipping total: $20.00 Total: $99.99

You can view and check your order by clicking on the button below, and you'll find your invoice attached to this email.

View order ( http://us.merch.larian.com/en/order/_MNXwH-R2B )
"""

KEY_EMAIL = """
Dear Nick Brett,

The time has come to claim your digital goods for Baldur's Gate 3!

*JLDD-7FNB-PKTG*

Follow the link below for instructions on how to redeem your keys:
https://www.playstation.com/en-ie/support/store/redeem-ps-store-voucher-code

All bonus in-game items will be waiting for you in Baldur's Gate 3 - just check inside your treasure chest at camp.
"""

SHIPPING_UPDATE = """
Shipping Update: PS5 North America Deluxe Edition
Dear Nick Brett,

Your order is being packed by our imps and will ship soon. Tracking to follow.
"""


def test_order_confirmation_parses():
    p = parse_larian_receipt(ORDER_CONFIRMATION, message_id="32465")
    assert p is not None
    assert p.order_number == "_MNXwH-R2B"
    assert p.total == "$99.99"
    assert len(p.items) == 1
    item = p.items[0]
    assert "Baldur's Gate 3" in item.title
    assert item.price == "$79.99"
    c = classify_item(item.title, platform_hint=item.platform_hint)
    assert c.classification == "playstation_game"
    assert c.platform == "ps5"


def test_key_email_returns_none():
    assert parse_larian_receipt(KEY_EMAIL, message_id="30358") is None


def test_shipping_update_returns_none():
    assert parse_larian_receipt(SHIPPING_UPDATE, message_id="31337") is None


def test_unrelated_body_returns_none():
    assert parse_larian_receipt("completely unrelated newsletter body") is None
