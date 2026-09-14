"""Squarespace storefront parser tests, based on the real confirmation bodies
from the archive (msgvault): Lost In Cult order #59041 (2025-05-21, Thank
Goodness You're Here! for PlayStation 5, £59.99) and 2 Old 4 Gaming order
#2662 (2026-04-25, ten unofficial instruction manuals).

Both storefronts share the sender no-reply@squarespace.info, which is why the
parser (and the registered source) is storefront-agnostic."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.squarespace import (
    parse_squarespace_receipt,
)

# Lost In Cult #59041 — theme A: the qty sits on the line below the price.
LOST_IN_CULT = """
Lost In Cult Order #59041 Confirmed Your order from Lost In Cult is confirmed. Once your package ships we will send you a notification email. VIEW YOUR ORDER Order Summary Order #59041 Confirmation Co

Lost In Cult ( https://comms-sl-events.squarespace.info/?ref=AAA )

Order #59041 Confirmed
----------------------

Your order from Lost In Cult is confirmed.

Once your package ships we will send you a notification email.

VIEW YOUR ORDER ( https://comms-sl-events.squarespace.info/?ref=BBB )

Order Summary

*Order #59041*
*Confirmation Code 11k0jRz3VQZgesfuB8LAFA*
Placed on May 21, 2025 at 2:25 PM GMT+1

( https://comms-sl-events.squarespace.info/?ref=CCC )

Thank Goodness You're Here! ( https://comms-sl-events.squarespace.info/?ref=CCC ) £59.99 TGYH-PS Platform: Playstation 5
Qty: 1 £59.99 / Item

Checkbox
Yes

Subtotal £59.99 Shipping £7.00 Sales Tax £0.00 Total £66.99

Apple Pay £66.99

Customer Information

Shipping Address Nick Brett 80 Riverside Blvd Apt 10C
New York NY 10069 US 9175107859

Payment Method Apple Pay
"""

# 2 Old 4 Gaming #2662 — theme B: title + price + SKU + qty on one line.
TWO_OLD_FOUR_GAMING = """
2 Old 4 Gaming Order #2662 Confirmed Your order from 2 Old 4 Gaming is confirmed. Once your package ships we will send you a notification email. VIEW YOUR ORDER Order Summary Order #2662 Confirmation

2 Old 4 Gaming ( https://comms-sl-events.squarespace.info/?ref=AAA )

Order Summary

*Order #2662*
*Confirmation Code I0Mbexh7Yvgc3GA6AacFlA*
Placed on April 25, 2026 at 6:17 PM GMT+1

Horizon Zero Dawn Manual PS4/PS5 - PlayStation Instruction Manual (Unofficial) - Perfect for video game collectors! ( https://comms-sl-events.squarespace.info/?ref=D1 ) £5.00 SQ6899346 Qty: 1 £5.00 / Item

Resident Evil 2, 3 and 4 Remake Manual Bundle PS5 / PS4 Instruction Manual (Unofficial) ( https://comms-sl-events.squarespace.info/?ref=D2 ) £13.00 SQ4674376 Qty: 1 £13.00 / Item

Subtotal £58.00 Shipping £18.20 Tax £0.00 Total £76.20

Amount Paid £76.20

Customer Information

Payment Method PayPal
"""

# A shipped/status email: same layout, no prices.
SHIPPED = """
Lost In Cult Order #59041 Shipped Your order from Lost In Cult is on the way. VIEW YOUR ORDER Order Summary Order #59041 Placed on May 21, 2025 at 2:25 PM GMT+1

Order Summary

*Order #59041*
Placed on May 21, 2025 at 2:25 PM GMT+1

Thank Goodness You're Here! ( https://comms-sl-events.squarespace.info/?ref=CCC ) TGYH-PS

Shipped with USPS
"""


def test_parses_lost_in_cult_game_confirmation():
    p = parse_squarespace_receipt(LOST_IN_CULT, message_id="20805")
    assert p is not None
    assert p.source == "squarespace"
    assert p.order_number == "59041"
    assert p.purchased_at == "5/21/2025"
    assert p.subtotal == "£59.99"
    assert p.total == "£66.99"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "Thank Goodness You're Here!"
    assert item.price == "£59.99"
    assert item.qty == 1
    assert item.platform_hint == "Playstation 5"


def test_thank_goodness_youre_here_classifies_as_playstation_5():
    p = parse_squarespace_receipt(LOST_IN_CULT, message_id="20805")
    c = classify_item(p.items[0].title, platform_hint=p.items[0].platform_hint)
    assert c.classification == "playstation_game"
    assert c.platform == "playstation 5"


def test_parses_multi_item_order_with_inline_qty():
    p = parse_squarespace_receipt(TWO_OLD_FOUR_GAMING, message_id="3780")
    assert p is not None
    assert p.order_number == "2662"
    assert p.purchased_at == "4/25/2026"
    assert p.total == "£76.20"
    titles = [i.title for i in p.items]
    assert titles == [
        "Horizon Zero Dawn Manual PS4/PS5 - PlayStation Instruction Manual (Unofficial) - Perfect for video game collectors!",
        "Resident Evil 2, 3 and 4 Remake Manual Bundle PS5 / PS4 Instruction Manual (Unofficial)",
    ]
    assert [i.price for i in p.items] == ["£5.00", "£13.00"]
    assert [i.qty for i in p.items] == [1, 1]


def test_instruction_manuals_are_not_catalogued_as_games():
    """The 2 Old 4 Gaming order is printed matter: it must never enter
    owned_games as PlayStation games."""
    p = parse_squarespace_receipt(TWO_OLD_FOUR_GAMING, message_id="3780")
    for item in p.items:
        c = classify_item(item.title, platform_hint=item.platform_hint)
        # 'instruction manual' -> needs_review (non-game merch); the "... Manual
        # Bundle ..." title also trips the 'bundle' accessory hint. Neither is
        # ever catalogued as a game.
        assert c.classification != "playstation_game", item.title
    assert classify_item(p.items[0].title).classification == "needs_review"


def test_shipped_email_yields_no_purchase():
    assert parse_squarespace_receipt(SHIPPED, message_id="3628") is None


def test_non_squarespace_body_returns_none():
    assert parse_squarespace_receipt("Thanks for your order!\nTotal $19.99\n") is None
