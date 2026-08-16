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
