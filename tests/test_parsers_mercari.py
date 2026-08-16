"""Mercari parser tests, from real purchase emails in the archive
(msgvault 2026-08-16: item IDs m50029403165 / m32817008673)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.mercari import parse_mercari_receipt

VOID_TERRARIUM = """
The seller confirmed your order.

... URL noise ...

ID: m50029403165

( https://ablink... )

Item price

Buyer protection fee

Tax

Credits

$19.00

$0.68

$1.75

-$10.00

Total amount paid
$11.43

Payment Method

applepay
"""

RETRO_VIDEO_GAMES = """
Next steps

... URL noise ...

ID: m32817008673

( https://ablink... )

Item price

Buyer protection fee

Tax

Credits

$24.00

$1.20

$2.18

$0.00

Total amount paid
$27.38

Payment Method

visa
"""

SHIPPED = """
Your item has shipped: Void Terrarium: Deluxe Edition For Playstation 5

ID: m50029403165

Tracking: 9400111202552014046464
"""


def test_purchase_keys_on_item_id():
    p = parse_mercari_receipt(VOID_TERRARIUM, message_id="20938", subject="You purchased: Void Terrarium: Deluxe Edition For Playstation 5")
    assert p is not None
    assert p.source == "mercari"
    assert p.order_number == "m50029403165"  # no order number — item ID keys it
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "Void Terrarium: Deluxe Edition For Playstation 5"
    assert item.price == "$19.00"
    assert p.tax == "$1.75"
    assert p.total == "$11.43"
    # Seller-authored "For Playstation 5" -> platform keyword classifies it.
    assert classify_item(item.title).classification == "playstation_game"


def test_older_template_title_from_html():
    p = parse_mercari_receipt(RETRO_VIDEO_GAMES, message_id="27787", subject="You've made a purchase: The 100 Greatest Retro Video Games")
    assert p is not None
    assert p.order_number == "m32817008673"
    # Title extracted from the <strong> HTML in the text body.
    assert p.items[0].title == "The 100 Greatest Retro Video Games"
    # It's a book, not a PlayStation game — excluded from the catalog.
    assert classify_item(p.items[0].title).classification == "needs_review"


def test_shipped_email_returns_none():
    assert parse_mercari_receipt(SHIPPED, message_id="20937", subject="Your item has shipped: Void Terrarium: Deluxe Edition For Playstation 5") is None


def test_unrelated_body_returns_none():
    assert parse_mercari_receipt("some random email") is None
