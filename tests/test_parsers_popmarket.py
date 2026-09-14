"""PopMarket parser tests, based on the real confirmation body from the
archive (msgvault: order 0121-2412-1125SA, Shin Megami Tensei V: Vengeance
Steelbook Edition for Playstation 5, 2025-01-12)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.popmarket import parse_popmarket_receipt

CONFIRMATION = """
Logo ( https://www.popmarket.com/ )

**************************************************
Thank you for your order!
Your order confirmation number is: 0121-2412-1125SA
**************************************************

You may check the status of your order by going to this web page:
https://www.popmarket.com/account/orderhistory

1/12/2025 12:41:00 PM
---------------------

*Bill-To*
PAYPAL nick.brett1@gmail.com
Nicholas Brett
nick.brett1@gmail.com *Ship-To*
Nick Brett
80 Riverside Blvd Apt 10C
New York, NY 10069-0311
US
9175107859
nick.brett1@gmail.com

-------------
Today's Order
-------------

Product Qty Cost Total

*Shin Megami Tensei V: Vengeance Steelbook Edition for Playstation 5*
Playstation 5
730865220786
Video Game
Availability: In Stock

Get it between Fri. Jan 17 - Mon. Jan 20 to New York 1 $24.99 $24.99
*Subtotal*
Standard Shipping
Sales Tax
Retail Delivery Fee
*Total*
(1 items)

8.875%

$24.99
$0.00
$2.22
$0.00
$27.21

Order Summary Today's Order $27.21

Thank you for shopping at PopMarket ( https://www.popmarket.com/ )
"""


def test_parses_confirmation_item_and_total():
    p = parse_popmarket_receipt(CONFIRMATION, message_id="25279")
    assert p is not None
    assert p.source == "popmarket"
    assert p.order_number == "0121-2412-1125SA"
    assert p.purchased_at == "1/12/2025"
    assert p.total == "$27.21"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "Shin Megami Tensei V: Vengeance Steelbook Edition for Playstation 5"
    assert item.price == "$24.99"
    assert item.qty == 1
    assert item.platform_hint == "Playstation 5"


def test_item_classifies_as_playstation_5():
    p = parse_popmarket_receipt(CONFIRMATION)
    c = classify_item(p.items[0].title, platform_hint=p.items[0].platform_hint)
    assert c.classification == "playstation_game"
    assert c.platform == "playstation 5"


def test_non_popmarket_body_is_ignored():
    assert parse_popmarket_receipt("Your order #1234 has shipped") is None


def test_order_without_priced_items_is_ignored():
    # A status/registration email carries the PopMarket banner but no items.
    assert parse_popmarket_receipt(
        "Welcome to PopMarket ( https://www.popmarket.com/ ). Complete Registration Process"
    ) is None
