"""eBay parser tests, from real confirmation bodies in the archive
(msgvault 2026-08-16: order 12-14766-51548 Wildermyth, etc.)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.ebay import parse_ebay_receipt

WILDERMYTH = """
Thanks for making another purchase.

Welcome back Nick! Thanks for another purchase.

View order details ( https://www.ebay.com/vod/FetchOrderDetails?itemId=376627255605&transactionId=10082668625512 )

Your order will ship to:
------------------------
Nick Brett
80 Riverside Blvd
Apt 10C
New York, 10069-0311 NY
United States

Estimated delivery:
-------------------
Wed, Jun 17 - Mon, Jun 22

------------------
Your order details
------------------

We'll let you know when your order has shipped.

SEALED Wildermyth for Sony PlayStation 5 (PS5) w/ Monster Compendium
--------------------------------------------------------------------

( https://www.ebay.com/itm/376627255605 )

Price:

*$9.99*

*$9.99*

Item ID:

376627255605

376627255605

Order number:

12-14766-51548

12-14766-51548

Seller:

glenn_87 ( https://www.ebay.com/ulk/usr/glenn_87 )
"""

DVD_TITLE = """
Thanks for making another purchase.

Welcome back Nick! Thanks for another purchase.

View order details ( https://www.ebay.com/vod/FetchOrderDetails?itemId=175467895293 )

Your order details

We'll let you know when your order has shipped.

Thank You For Playing [DVD] [*READ* Ex-Lib. DISC-ONLY]
------------------------------------------------------

( https://www.ebay.com/vod/FetchOrderDetails?itemId=175467895293 )

Price:

*$14.99*

Item ID:

175467895293

Order number:

11-34567-89101

Seller:

some_seller ( https://www.ebay.com/ulk/usr/some_seller )

eBay Money Back Guarantee
"""

STOKKE_CHAIR = """
Thanks for making another purchase.

Your order details

We'll let you know when your order has shipped.

Stokke Tripp Trapp High Chair Complete Hardware Set (... ( https://www.ebay.com/vod/FetchOrderDetails?itemId=145274245568 )

Price:

*$25.00*

Item ID:

145274245568

Order number:

12-22222-33333

Seller:

seller_x ( https://www.ebay.com/ulk/usr/seller_x )
"""

STATUS_UPDATE = """
🚚 Order update: SEALED Wildermyth for Sony PlayStation 5 (PS5) w/ Monster Compendium

Your order is on its way!

Order number: 12-14766-51548

Track package
"""


def test_wildermyth_ps5():
    p = parse_ebay_receipt(WILDERMYTH, message_id="1593")
    assert p is not None
    assert p.source == "ebay"
    assert p.order_number == "12-14766-51548"
    assert p.item_id == "376627255605"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "SEALED Wildermyth for Sony PlayStation 5 (PS5) w/ Monster Compendium"
    assert item.price == "$9.99"
    # Seller-authored 'for Sony PlayStation 5' -> platform keyword classifies it.
    assert classify_item(item.title).classification == "playstation_game"


def test_dvd_title_is_not_a_game():
    p = parse_ebay_receipt(DVD_TITLE, message_id="20866")
    assert p is not None
    assert p.order_number == "11-34567-89101"
    assert p.items[0].title == "Thank You For Playing [DVD] [*READ* Ex-Lib. DISC-ONLY]"
    assert classify_item(p.items[0].title).classification == "needs_review"


def test_url_suffix_stripped_from_title():
    p = parse_ebay_receipt(STOKKE_CHAIR, message_id="35425")
    assert p is not None
    assert p.items[0].title == "Stokke Tripp Trapp High Chair Complete Hardware Set (..."
    assert p.order_number == "12-22222-33333"


def test_status_update_returns_none():
    assert parse_ebay_receipt(STATUS_UPDATE, message_id="s1") is None


def test_unrelated_body_returns_none():
    assert parse_ebay_receipt("some random email") is None
