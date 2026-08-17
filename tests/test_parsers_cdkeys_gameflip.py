"""CDKeys + Gameflip parser tests, from real archive bodies
(msgvault 2026-08-16: ASTRO BOT / God of War Ragnarök orders, Spider-Man 2 /
Cat's Request Gameflip purchases)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.cdkeys import parse_cdkeys_receipt
from mailroom.verticals.game_catalog.parsers.gameflip import parse_gameflip_receipt

CDKEYS_2025 = """
PAY LESS. GAME MORE.

Hi Nicholas Brett,
Thank you for your purchase!
Click on get your key to access your code.

Order #0127233883

Subtotal $40.19 Order Total *$40.19*

ASTRO BOT PS5 (US)

$40.19
x1

Get Your Key ( https://www.cdkeys.com/downloadable/... )

Subtotal:
$40.19
Total
$40.19
"""

CDKEYS_2023 = """
*YOUR ORDER IS COMPLETE*

Order Number: 0151509331

Hi Guest, your order been completed and is ready for collection.

Subtotal $47.99 Total *$47.99*

God of War Ragnarök PS5 (US)

Click here to download ( https://www.cdkeys.com/downloadable/... )

1x $47.99

Order Number 0151509331 Customer Name Guest
"""

GAMEFLIP_THANKYOU = """Hi Nick B,

Thank you for your purchase of "Marvel's Spider-Man 2" on Gameflip and below you will find the details of your order.

Order: Marvel's Spider-Man 2
Order ID: 798cc5d1-de47-44fa-b6aa-1d65a628c063
URL: https://gameflip.com/exchange_buyer/798cc5d1-de47-44fa-b6aa-1d65a628c063
Seller: discountgamesdirect

Price: $40.00
Shipping Fee: $0.00

Thanks,
Team Gameflip
"""

GAMEFLIP_COMPLETE = """Hello Nick B,

Thank you for purchasing Cat's Request using Gameflip.

Order: Cat's Request
URL: https://gameflip.com/exchange_buyer/824bda09-87df-40b4-9914-d6cde5e57874
Seller: Rodrigo Diniz
Price: $1.99

Thanks,
Team Gameflip
"""


def test_cdkeys_2025_astro_bot():
    p = parse_cdkeys_receipt(CDKEYS_2025, message_id="21236")
    assert p is not None
    assert p.source == "cdkeys"
    assert p.order_number == "0127233883"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "ASTRO BOT PS5 (US)"
    assert item.platform_hint == "ps5"
    assert item.price == "$40.19"
    assert p.total == "$40.19"
    assert classify_item(item.title, platform_hint=item.platform_hint).classification == "playstation_game"


def test_cdkeys_2023_god_of_war():
    p = parse_cdkeys_receipt(CDKEYS_2023, message_id="51232")
    assert p is not None
    assert p.order_number == "0151509331"
    assert p.items[0].title == "God of War Ragnarök PS5 (US)"
    assert p.items[0].platform_hint == "ps5"


def test_gameflip_spiderman2():
    p = parse_gameflip_receipt(GAMEFLIP_THANKYOU, message_id="28132")
    assert p is not None
    assert p.source == "gameflip"
    assert p.order_number == "798cc5d1-de47-44fa-b6aa-1d65a628c063"
    assert p.items[0].title == "Marvel's Spider-Man 2"
    assert p.items[0].price == "$40.00"
    assert p.total == "$40.00"
    # seller title has no platform token -> review bucket
    assert classify_item(p.items[0].title).classification == "needs_review"


def test_gameflip_complete_email():
    p = parse_gameflip_receipt(GAMEFLIP_COMPLETE, message_id="28897")
    assert p is not None
    assert p.order_number == "824bda09-87df-40b4-9914-d6cde5e57874"
    assert p.items[0].title == "Cat's Request"


def test_unrelated_returns_none():
    assert parse_cdkeys_receipt("some random email") is None
    assert parse_gameflip_receipt("some random email") is None
