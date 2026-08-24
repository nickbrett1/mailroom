"""Amazon parser tests, based on real bodies from the archive (msgvault,
2026-08-16: order 114-1970161-5765038 Sonic Superstars; multi-order 19195)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.amazon import parse_amazon_receipt

ORDERED_SINGLE = """
Thanks for your order, Nick!
Ordered

Shipped

Out for delivery

Delivered

Arriving Sunday

Nick - NEW YORK, NY

Order #
114-1970161-5765038

View or edit order
https://www.amazon.com/your-orders/order-details?orderID=114-1970161-5765038&ref_=p_btn_fed_veo

* Sonic Superstars - PlayStation 5
  Quantity: 1
  15.56 USD


Grand Total:
16.94 USD


©2026 Amazon.com, Inc. or its affiliates.
"""

SHIPPED_SAME_ORDER = """
Your package was shipped!
Ordered

Shipped

Out for delivery

Delivered

Arriving Monday

Nick - NEW YORK, NY

Order #
114-1970161-5765038

Track package
https://www.amazon.com/progress-tracker/package?orderId=114-1970161-5765038

* Sonic Superstars - PlayStation 5
  Quantity: 1
  15.56 USD


Total
16.94 USD


©2026 Amazon.com, Inc.
"""

OUT_FOR_DELIVERY_NO_PRICE = """
Your package is out for delivery!
Ordered

Shipped

Out for delivery

Delivered

Arriving today

Nick - NEW YORK, NY

Order #
114-1970161-5765038

Track package
https://www.amazon.com/progress-tracker/package?orderId=114-1970161-5765038

* Sonic Superstars - PlayStation 5
  Quantity: 1


If you would like to tell us where to leave your package, please add delivery instructions
https://www.amazon.com/ma/deliveryProfilePage

©2026 Amazon.com, Inc.
"""

MULTI_ORDER_MIXED = """
Your Orders

Thanks for your order, Nick!
Ordered

Shipped

Arriving tomorrow

Nick - NEW YORK, NY

Order #
111-5372849-6289056

View or edit order
https://www.amazon.com/gp/css/order-details?orderID=111-5372849-6289056

* Tekken 8 (PS5)
  Quantity: 1
  24.99 USD

* Amazon Basics Razor 17 Piece Set, Black
  Quantity: 2
  12.74 USD

* Kingdom Come: Deliverance II - PlayStation 5
  Quantity: 1
  39.99 USD

Total
130.48 USD


Arriving overnight

Nick - NEW YORK, NY

Order #
111-4367032-4449826

View or edit order
https://www.amazon.com/gp/css/order-details?orderID=111-4367032-4449826

* Degree Men Deodorant 4 Pack
  Quantity: 1
  9.99 USD

Total
87.36 USD


©2025 Amazon.com, Inc.
"""


def test_ordered_email_single_order():
    ps = parse_amazon_receipt(ORDERED_SINGLE, message_id="7845")
    assert len(ps) == 1
    p = ps[0]
    assert p.source == "amazon"
    assert p.order_number == "114-1970161-5765038"
    assert p.total == "$16.94"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "Sonic Superstars - PlayStation 5"
    assert item.price == "$15.56"
    assert item.qty == 1
    assert classify_item(item.title).classification == "playstation_game"


def test_shipped_email_dedupes_by_order_item():
    ordered = parse_amazon_receipt(ORDERED_SINGLE, message_id="7845")
    shipped = parse_amazon_receipt(SHIPPED_SAME_ORDER, message_id="7742")
    assert len(shipped) == 1
    # Same (order_number, item) as the Ordered email — merge layer must dedupe.
    o = ordered[0]
    s = shipped[0]
    assert o.order_number == s.order_number
    assert [(i.title, i.price) for i in o.items] == [(i.title, i.price) for i in s.items]


def test_out_for_delivery_tolerates_missing_price():
    ps = parse_amazon_receipt(OUT_FOR_DELIVERY_NO_PRICE, message_id="7451")
    assert len(ps) == 1
    item = ps[0].items[0]
    assert item.title == "Sonic Superstars - PlayStation 5"
    assert item.price is None
    assert item.qty == 1


def test_multi_order_email_returns_one_purchase_per_order():
    ps = parse_amazon_receipt(MULTI_ORDER_MIXED, message_id="19195")
    assert len(ps) == 2
    first, second = ps
    assert first.order_number == "111-5372849-6289056"
    assert len(first.items) == 3
    assert first.total == "$130.48"
    # Mixed catalog: games classify PS, non-games excluded by the gate.
    tekken = classify_item(first.items[0].title)
    assert tekken.classification == "playstation_game"
    assert classify_item(first.items[1].title).classification == "needs_review"  # razor: no platform
    assert second.order_number == "111-4367032-4449826"
    assert len(second.items) == 1


def test_unrelated_body_returns_empty_list():
    assert parse_amazon_receipt("some random email") == []


def test_subject_fallback_extracts_confirmation_item():
    """Newer Amazon template: the body has the order # but no item lines —
    the item fact lives in the subject ('Your Amazon.com order of "X".')."""
    body = """
Thanks for your order, Nick!

Order #
114-1970161-5765038

View or edit order
https://www.amazon.com/your-orders/order-details?orderID=114-1970161-5765038

Total
16.94 USD
"""
    ps = parse_amazon_receipt(body, message_id="32705", subject='Your Amazon.com order of "Resident Evil 4 - PS5".')
    assert len(ps) == 1
    assert ps[0].items[0].title == "Resident Evil 4 - PS5"


def test_order_details_template_parses_item_from_body():
    """'Order details / View your item' template puts the item ABOVE the
    'Sold by: Amazon.com' line (itself above 'Order #'), so the block split
    never sees it. The parser must extract the clean title from the body —
    even when the subject is a mangled duplicate."""
    body = """Order details
Beyond A Steel Sky: Beyond A SteelBook Edition (PS5)
Beyond A Steel Sky: Beyond A SteelBook Edition (PS5)Beyond A Steel Sky: Beyond A Stee…
Sold by: Amazon.com
$19.99
View your item
Order summary
Order placed December 26, 2022
Order # 113-7134038-7289042
Grand Total:
$21.76
"""
    ps = parse_amazon_receipt(
        body,
        message_id="m1",
        subject='Your Amazon.com order of "Beyond A Steel Sky: Beyond A SteelBook Edition (PS5)Beyond A Steel Sky: Beyond A Stee…".',
    )
    assert len(ps) == 1
    assert ps[0].order_number == "113-7134038-7289042"
    assert ps[0].items[0].title == "Beyond A Steel Sky: Beyond A SteelBook Edition (PS5)"


def test_order_details_template_third_party_seller():
    """The 'Order details' template also ships with third-party sellers
    ('Sold by: European Rarities') — the item must still be extracted from the
    body, and a duplicate title collapses to the clean one."""
    body = """Order details
Evergate PS5 (PS5)
Evergate PS5 (PS5)Evergate PS5 (PS5)
Sold by: European Rarities
$12.99
View your item
Order summary
Order placed June 28, 2023
Order # 111-5694915-7906620
Grand Total:
$14.14
"""
    ps = parse_amazon_receipt(body, message_id="m2")
    assert len(ps) == 1
    assert ps[0].order_number == "111-5694915-7906620"
    assert ps[0].items[0].title == "Evergate PS5 (PS5)"


def test_cancelled_order_never_becomes_a_purchase():
    """A cancellation email must not produce a catalog fact — the subject
    fallback used to swallow the whole tail ('...has been canceled') and the
    'MPS4' substring classified a plunger as a PlayStation game."""
    body = """
Order #
111-8374686-1690617

View or edit order
https://www.amazon.com/your-orders/order-details?orderID=111-8374686-1690617
"""
    ps = parse_amazon_receipt(
        body,
        message_id="9988",
        subject='Your Amazon.com order of "Master Plunger MPS4 Sink ..." has been canceled',
    )
    assert ps == []


def test_cancellation_subject_with_item_lines_also_skipped():
    """Even when the body carries order/item lines, a cancel subject wins."""
    body = """
Order #
111-1234567-7654321

* Master Plunger MPS4 Sink
  Quantity: 1
  12.34 USD

Total
12.34 USD
"""
    ps = parse_amazon_receipt(body, message_id="9989", subject="Order canceled")
    assert ps == []


# Real delivery-estimate email (msgvault 65274, 2021-05-02) — the template
# where the item is an indented line above "Sold by Amazon.com Services" with
# no '*' prefix, no qty and no price. This was the gap that missed
# "Uncharted: Nathan Drake Collection" from the owned catalog.
DELIVERY_ESTIMATE_UNCHARTED = """
Delivery Estimate Update
www.amazon.com/ref=fxm_4_0_tex_h
_________________________________________________________________________________________________

Hello,
We have an updated delivery estimate for your Amazon order. As soon as your items ship, we'll send you an email confirmation. To view the status of your order or make changes, please go to <a href="https://www.amazon.com/yourorders?ref_=fxm_3_0_yo_tn">Your Orders</a>. 

=================================================================================================

Order #111-7703809-7385008
Placed on  Thursday, April 29, 2021

        Uncharted: Nathan Drake Collection Hits - PlayStation 4
        Sold by Amazon.com Services LLC

            New estimated delivery date: Monday, May 10, 2021 - Tuesday, May 11, 2021
            Previous estimated delivery date: Monday, May 17, 2021 - Wednesday, May 19, 2021

=================================================================================================

If you need further assistance with your order, please visit Help & Customer Service:<br />http://www.amazon.com/help?ref=fxm_4_0_tex_cs

We hope to see you again soon.<br />Amazon.com
_________________________________________________________________________________________________

This email was sent from a notification-only address that cannot accept incoming email. Please do not reply to this message. 
"""


def test_delivery_estimate_email_captures_item():
    """A Delivery-estimate update (no '*' lines, no qty/price) must still
    yield the item so it can enter the owned catalog via the receipt chain."""
    ps = parse_amazon_receipt(
        DELIVERY_ESTIMATE_UNCHARTED,
        message_id="65274",
        subject="Delivery estimate update for your Amazon.com order #111-7703809-7385008",
    )
    assert len(ps) == 1
    p = ps[0]
    assert p.order_number == "111-7703809-7385008"
    assert p.total is None  # delivery-estimate emails carry no total
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "Uncharted: Nathan Drake Collection Hits - PlayStation 4"
    assert item.price is None
    assert item.qty == 1
    # The item flows through the platform gate as a PlayStation game.
    assert classify_item(item.title).classification == "playstation_game"


def test_delivery_estimate_skips_boilerplate_lines():
    """The item-title fallback must not swallow the 'Order #'/'Placed on'
    header as an item title."""
    ps = parse_amazon_receipt(
        DELIVERY_ESTIMATE_UNCHARTED,
        message_id="65274",
        subject="Delivery estimate update for your Amazon.com order #111-7703809-7385008",
    )
    assert all(not i.title.lower().startswith(("order #", "placed on")) for p in ps for i in p.items)


def test_standard_template_unaffected_by_delivery_fallback():
    """The fallback only runs when there are no '*'-prefixed items; a normal
    Ordered body must still parse exactly as before (one item, price + total)."""
    ps = parse_amazon_receipt(ORDERED_SINGLE, message_id="7845")
    assert len(ps) == 1
    item = ps[0].items[0]
    assert item.title == "Sonic Superstars - PlayStation 5"
    assert item.price == "$15.56"
    assert item.qty == 1
    assert ps[0].total == "$16.94"
