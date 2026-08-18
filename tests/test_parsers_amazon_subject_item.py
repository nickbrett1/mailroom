"""Amazon subject-item template test (the confirmation body carries no item
lines — the item lives only in the subject; verified msg 32705, 2024-04-12)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.amazon import parse_amazon_receipt

SUBJECT_ITEM_ONLY_BODY = """Amazon.com Order Confirmation

Hello Nick,

Thank you for shopping with us.

We'll send a confirmation when your item ships.

View or manage your orders in Your Orders:
https://www.amazon.com/gp/css/order-details?orderId=114-0868192-0225841&ref_=TE_simp_od

Details
Order #114-0868192-0225841

    Arriving:
    Tuesday, April 16

    Ship to:
    Nick
    NEW YORK, NY

    Order Total: $31.31
"""


def test_subject_item_fallback_parses():
    p = parse_amazon_receipt(
        SUBJECT_ITEM_ONLY_BODY,
        message_id="32705",
        subject='Your Amazon.com order of "Resident Evil 4 - PS5".',
    )
    assert len(p) == 1
    order = p[0]
    assert order.order_number == "114-0868192-0225841"
    assert len(order.items) == 1
    item = order.items[0]
    assert item.title == "Resident Evil 4 - PS5"
    c = classify_item(item.title, platform_hint=item.platform_hint)
    assert c.classification == "playstation_game"


def test_no_subject_item_no_fabrication():
    # a body-only email with no subject item must stay unparsed
    assert parse_amazon_receipt(SUBJECT_ITEM_ONLY_BODY, message_id="32705", subject="Some other subject") == []
