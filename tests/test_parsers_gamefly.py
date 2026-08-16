"""GameFly parser tests, from real confirmation bodies in the archive
(msgvault 2026-08-16: orders 72519662 / 72511160 / 72522029)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.gamefly import parse_gamefly_receipt

CONFIRM_SILENT_HILL = """This e-mail was sent to you from GameFly.  To ensure delivery to your inbox, please add us to your address book.


ORDER CONFIRMATION


Hi Nick,

Thanks for shopping with GameFly! Your purchase details are listed below.

We will send you a follow up email when your items ship.

Enjoy your purchase!


Order Number:  72519662
Order Submitted:  9/7/2025

Order Details
  Status:  In Process

  Shipping To:

    Nick Brett
    80 RIVERSIDE BLVD APT 10C
    NEW YORK, NY 10069-0311
    917-510-7859
  Shipping Method:  Standard $2.98

  Charge To:

    Paypal:  nick.brett1@gmail.com

  Order Summary

    Item:  Silent Hill 2 (PS5)
    Price:  $29.99
    Tracking Number:  N/A


Subtotal:          $29.99
Tax:               $2.92
Shipping:          $2.98
Order Total:       $35.89


Thank you,
The GameFly Team
"""

CONFIRM_ATOMFALL = """This e-mail was sent to you from GameFly.


ORDER CONFIRMATION


Hi Nick,

Thanks for shopping with GameFly! Your purchase details are listed below.


Order Number:  72511160
Order Submitted:  7/28/2025

Order Details
  Status:  In Process

  Order Summary

    Item:  Atomfall (PS5)
    Price:  $29.99
    Tracking Number:  N/A


Subtotal:          $29.99
Tax:               $2.92
Shipping:          $2.98
Order Total:       $35.89


Thank you,
The GameFly Team
"""

CONFIRM_PS4 = """ORDER CONFIRMATION

Order Number:  72522029
Order Submitted:  9/14/2025

Order Summary

    Item:  Where the Heart Leads (PS4)
    Price:  $7.99
    Tracking Number:  N/A

Subtotal:          $7.99
Tax:               $0.71
Shipping:          $0.00
Order Total:       $8.70
"""

STATUS_UPDATE = """This e-mail was sent to you from GameFly.

GameFly Order Status Update

Hi Nick,

Your recent order has shipped!

Order Number:  72519662

Tracking Number:  9400111202552014046464

Silent Hill 2 (PS5)

Enjoy your purchase!
"""

RENTAL_SURVEY = """Please review your GameFly - Game Rental purchase

How was your rental experience with GameFly?
Rate your rental...
"""


def test_ps5_confirmation():
    p = parse_gamefly_receipt(CONFIRM_SILENT_HILL, message_id="16132")
    assert p is not None
    assert p.source == "gamefly"
    assert p.order_number == "72519662"
    assert p.purchased_at == "9/7/2025"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "Silent Hill 2 (PS5)"
    assert item.platform_hint == "ps5"
    assert item.price == "$29.99"
    assert p.subtotal == "$29.99"
    assert p.tax == "$2.92"
    assert p.total == "$35.89"
    assert classify_item(item.title, platform_hint=item.platform_hint).classification == "playstation_game"


def test_atomfall_ps5():
    p = parse_gamefly_receipt(CONFIRM_ATOMFALL, message_id="18295")
    assert p is not None
    assert p.order_number == "72511160"
    assert p.items[0].title == "Atomfall (PS5)"


def test_ps4_title():
    p = parse_gamefly_receipt(CONFIRM_PS4, message_id="15931")
    assert p is not None
    item = p.items[0]
    assert item.title == "Where the Heart Leads (PS4)"
    assert item.platform_hint == "ps4"
    assert p.total == "$8.70"


def test_status_update_returns_none():
    assert parse_gamefly_receipt(STATUS_UPDATE, message_id="15912") is None


def test_rental_survey_returns_none():
    assert parse_gamefly_receipt(RENTAL_SURVEY, message_id="r1") is None


def test_unrelated_body_returns_none():
    assert parse_gamefly_receipt("some random email") is None
