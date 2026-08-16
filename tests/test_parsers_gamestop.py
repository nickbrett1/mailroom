"""GameStop parser tests, based on real confirmation bodies from the archive
(msgvault, 2026-08-16: orders 1100000043740236, 1100000027339767, ...)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.gamestop import parse_gamestop_receipt

# --- Real format: 2022 template, multi-item, explicit Platform lines ---
MULTI_ITEM_2022 = """
GameStop, Inc.
&zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj;
https://www.gamestop.com

Thank you for your order, Nick!

Order Number: 1100000043740236

Order Date: 1/16/2022

Order Total

 $39.16

https://www.gamestop.com/order/search/?orderID=1100000043740236&email=nick.brett1@gmail.com
VIEW ORDER DETAILS

SHIP TO HOME

Shipping to 80 Riverside Blvd

No Man's Sky - PlayStation 4

Platform: PlayStation 4

Edition: Standard

Condition: Pre-Owned

QTY: 1

$8.99

Slay the Spire - PlayStation 4

Platform: PlayStation 4

Edition: Standard

Condition: Pre-Owned

QTY: 1

$6.99

Watch Dogs: Legion - PlayStation 4

Platform: PlayStation 4

Edition: Standard

Condition: Pre-Owned

QTY: 1

$9.99

Wolfenstein II: The New Colossus - PlayStation 4

Platform: PlayStation 4

Edition: Standard

Condition: Pre-Owned

QTY: 1

$9.99

We will send you tracking information when your order has shipped.

ORDER SUMMARY

Subtotal

$35.96

Shipping & Handling

$0.00

Estimated Tax

$3.20

Estimated Total

$39.16

Payment Method

Credit Card
"""

# --- Real format: 2021 pre-order console bundle (msg 66674, order 1100000027339767) ---
PREORDER_BUNDLE_2021 = """
GameStop, Inc.
&zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj; &zwnj;
https://www.gamestop.com

Thank you for your order, Nick!

Order Number: 1100000027339767

Order Date: 2/23/2021

Order Total

 $718.56

VIEW ORDER DETAILS

SHIP TO HOME

Shipping to 80 Riverside Blvd

PRE-ORDER

PlayStation 5 Spider-Man Ultimate Edition Bundle with $20 GameStop Gift Card

Condition: New

Release Date: 3/5/2021

Delivers 2-4 business days after release date.

QTY: 1

$659.99

We will send you a reminder when it's close to the release date.

ORDER SUMMARY

Subtotal

$659.99

Shipping & Handling

$0.00

Estimated Tax

$58.57

Estimated Total

$718.56

Payment Method

Credit Card
"""

# --- Real format: 2021 accessory + game without platform suffix (msg 65281) ---
ACCESSORY_2021 = """
GameStop, Inc.
https://www.gamestop.com

Thank you for your order, Nick!

Order Number: 1100000030570466

Order Date: 5/1/2021

Order Total

 $54.41

Total Savings $4.99

VIEW ORDER DETAILS

SHIP TO HOME

Shipping to 80 Riverside Blvd

PlayStation 5 DualSense Charging Station

Platform: PlayStation 5

Edition: White/Black

Condition: New

QTY: 1

$29.99

Shadow of the Colossus

Platform: PlayStation 4

Edition: Standard

Condition: Pre-Owned

QTY: 1

$19.99

ORDER SUMMARY

Subtotal

$49.98

Estimated Tax

$4.43

Estimated Total

$54.41
"""

# --- Real format: shipped email = no new facts (msg 51696) ---
SHIPPED_2023 = """
GameStop, Inc.
https://www.gamestop.com

Nick, your package is on the way!

Ship to: 80 RIVERSIDE BLVD

Tracking Number

9205599993025800006867

Track your package

Order Number:

1100000055868592

Order Date: 01/18/2023

View Order Details

Shipping Now

The Pathless - PlayStation 5

QTY: 1

$19.99

Assassin's Creed Valhalla - PlayStation 4

QTY: 1

$19.99

Untitled Goose Game - PlayStation 4

QTY: 1

$18.99
"""


def test_multi_item_2022_template():
    p = parse_gamestop_receipt(MULTI_ITEM_2022, message_id="60608")
    assert p is not None
    assert p.source == "gamestop"
    assert p.order_number == "1100000043740236"
    assert p.purchased_at == "1/16/2022"
    assert len(p.items) == 4
    assert p.items[0].title == "No Man's Sky - PlayStation 4"
    assert p.items[0].platform_hint == "PlayStation 4"
    assert p.items[0].price == "$8.99"
    assert p.items[0].qty == 1
    assert p.items[0].condition == "Pre-Owned"
    assert p.items[3].title == "Wolfenstein II: The New Colossus - PlayStation 4"
    assert p.subtotal == "$35.96"
    assert p.tax == "$3.20"
    assert p.total == "$39.16"


def test_preorder_console_bundle_extracted_as_item():
    p = parse_gamestop_receipt(PREORDER_BUNDLE_2021, message_id="66674")
    assert p is not None
    assert p.order_number == "1100000027339767"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "PlayStation 5 Spider-Man Ultimate Edition Bundle with $20 GameStop Gift Card"
    assert item.price == "$659.99"
    assert item.condition == "New"
    # Console bundle must NOT enter the catalog (platform gate).
    c = classify_item(item.title, platform_hint=item.platform_hint)
    assert c.classification == "accessory_hardware"


def test_accessory_and_game_without_suffix():
    p = parse_gamestop_receipt(ACCESSORY_2021, message_id="65281")
    assert p is not None
    assert p.order_number == "1100000030570466"
    assert len(p.items) == 2
    station, game = p.items
    assert station.title == "PlayStation 5 DualSense Charging Station"
    assert station.platform_hint == "PlayStation 5"
    assert station.price == "$29.99"
    assert classify_item(station.title, platform_hint=station.platform_hint).classification == "accessory_hardware"
    # Title has no ' - PlayStation 4' suffix; platform comes from the Platform: line.
    assert game.title == "Shadow of the Colossus"
    assert game.platform_hint == "PlayStation 4"
    assert classify_item(game.title, platform_hint=game.platform_hint).classification == "playstation_game"
    assert p.total == "$54.41"


def test_shipped_email_returns_none():
    assert parse_gamestop_receipt(SHIPPED_2023, message_id="51696") is None


def test_unrelated_body_returns_none():
    assert parse_gamestop_receipt("some random email about nothing") is None


def test_gamestop_confirm_without_order_number_returns_none():
    assert parse_gamestop_receipt("Thank you for your order, Nick!\nno order here") is None
