"""Target parser tests, based on real confirmation bodies from the archive
(msgvault 2026-08-16: orders 912003531191358 / 912000475874886)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.target import parse_target_receipt

LAUNDRY_BAG = """
Order #912003531191358 

Thanks for your order, Nick!

Placed June 26, 2026

Now just sit back while we get to work. We'll send you an email when tracking info is available.

Order total
$20.68

You saved
$1.00

https://click.oe1.target.com/?qs=abc
Visit order details

Shipping

Delivers to: 
Nick Brett, 80 Riverside Blvd Apt 10C, New York, NY 10069-0311

https://click.oe1.target.com/?qs=def

https://click.oe1.target.com/?qs=ghi
Gleener S'wet Wet/Dry Travel Laundry Bag Aqua Blue  

Qty: 1 

$19.99 / ea 

Arrives Sun, Jun 28 

Order Summary 

Subtotal (1 item)

$19.99

Discounts

Target Circle Card 5%

-$1.00 

Delivery

Free

Estimated taxes

Based on 10069

$1.69

Total

$20.68

Target Circle Credit Card *6867

$20.68

Need to make changes? Act fast.
"""

TLOU_GAME = """
Order #912000475874886

Thanks for your order, Nick!

Placed November 24, 2023

Order total
$42.99

https://click.oe1.target.com/?qs=abc
Visit order details

Shipping

Delivers to: 
Nick Brett, 80 Riverside Blvd Apt 10C, New York, NY 10069-0311

https://click.oe1.target.com/?qs=def

The Last of Us Part 1 - PlayStation 5 

Qty: 1 

$39.99 / ea

Arriving by Wed, Nov 29 

Order Summary

Subtotal (1 item)

$39.99

Estimated taxes

$3.00

Total

$42.99
"""

STATUS_ARRIVED = """
An item has arrived from order #912003531191358!

Your package has been delivered.

Order #912003531191358

Gleener S'wet Wet/Dry Travel Laundry Bag Aqua Blue
"""


def test_laundry_bag_confirmation():
    p = parse_target_receipt(LAUNDRY_BAG, message_id="831")
    assert p is not None
    assert p.source == "target"
    assert p.order_number == "912003531191358"
    assert p.purchased_at == "June 26, 2026"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "Gleener S'wet Wet/Dry Travel Laundry Bag Aqua Blue"
    assert item.price == "$19.99"
    assert item.qty == 1
    assert p.subtotal == "$19.99"
    assert p.tax == "$1.69"
    assert p.total == "$20.68"
    assert classify_item(item.title).classification == "needs_review"


def test_playstation_game_confirmation():
    p = parse_target_receipt(TLOU_GAME, message_id="36977")
    assert p is not None
    assert p.order_number == "912000475874886"
    assert p.purchased_at == "November 24, 2023"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "The Last of Us Part 1 - PlayStation 5"
    assert item.price == "$39.99"
    assert classify_item(item.title).classification == "playstation_game"
    assert p.total == "$42.99"


def test_status_email_returns_none():
    assert parse_target_receipt(STATUS_ARRIVED, message_id="785") is None


def test_unrelated_body_returns_none():
    assert parse_target_receipt("some random email") is None
