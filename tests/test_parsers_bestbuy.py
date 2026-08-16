"""Best Buy parser tests.

The archive (2026-08-16) stores Best Buy confirmations with no usable body
(empty HTML + link-only text), so this uses a realistic table-based HTML
sample in Best Buy's actual email layout.
"""

from __future__ import annotations

from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.bestbuy import parse_bestbuy_receipt

ORDER_HTML = """<html><body>
<table><tr><td>BEST BUY</td></tr>
<tr><td>Thanks for your order.</td></tr></table>
<table>
<tr><td>Order Number: 1234567890</td></tr>
<tr><td>Order Date: 11/27/2025</td></tr>
</table>
<table>
<tr><td>Item</td></tr>
<tr><td>Hogwarts Legacy - PlayStation 5</td><td>Qty: 1</td><td>$39.99</td></tr>
<tr><td>Elden Ring - PlayStation 5</td><td>Qty: 1</td><td>$49.99</td></tr>
</table>
<table>
<tr><td>Subtotal $89.98</td></tr>
<tr><td>Tax $7.80</td></tr>
<tr><td>Total $97.78</td></tr>
</table>
</body></html>
"""

SHIPPED_HTML = """<html><body><table>
<tr><td>BEST BUY</td></tr>
<tr><td>Your package has been delivered</td></tr>
<tr><td>Order Number: 1234567890</td></tr>
</table></body></html>
"""


def test_html_order_confirmation():
    p = parse_bestbuy_receipt(body_html=ORDER_HTML, message_id="564")
    assert p is not None
    assert p.source == "bestbuy"
    assert p.order_number == "1234567890"
    assert p.purchased_at == "11/27/2025"
    assert len(p.items) == 2
    hogwarts, elden = p.items
    assert hogwarts.title == "Hogwarts Legacy - PlayStation 5"
    assert hogwarts.platform_hint == "playstation 5"
    assert hogwarts.price == "$39.99"
    assert classify_item(hogwarts.title).classification == "playstation_game"
    assert elden.title == "Elden Ring - PlayStation 5"
    assert p.subtotal == "$89.98"
    assert p.tax == "$7.80"
    assert p.total == "$97.78"


def test_shipped_html_returns_none():
    assert parse_bestbuy_receipt(body_html=SHIPPED_HTML, message_id="5265") is None


def test_text_fallback_when_no_html():
    p = parse_bestbuy_receipt(
        body_text="Thanks for your order.\nOrder Number: 1029384756\nOrder Date: 1/2/2023\nAstro Bot - PS5\nQty: 1\n$59.99\nTotal $59.99",
        message_id="x",
    )
    assert p is not None
    assert p.order_number == "1029384756"
    assert p.items[0].title == "Astro Bot - PS5"


def test_unrelated_body_returns_none():
    assert parse_bestbuy_receipt(body_text="some random email") is None


TRACKING_GOD_OF_WAR = """
Best Buy | We have your tracking number. | NES_SH_ReadyToShip

Your package is prepped and ready to ship.

We have your tracking number.

Your package is prepped and ready for FedEx, and is scheduled to arrive on 12/19.

Order number: 
BBY01-807003276801

Tracking Number: 
433207095584

View Order Details

Your shipping info.

Status

Ready to ship

https://click.emailinfo2.bestbuy.com/?qs=abc
God of War III Remastered Standard Edition - PlayStation 4

Get It By:

Thursday, December 19

Model #:3000925

SKU:5607062

Qty:1
"""


def test_tracking_email_fallback():
    from mailroom.verticals.game_catalog.parsers.bestbuy import parse_bestbuy_tracking

    p = parse_bestbuy_tracking(TRACKING_GOD_OF_WAR, message_id="25949")
    assert p is not None
    assert p.order_number == "BBY01-807003276801"
    assert p.source == "bestbuy"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "God of War III Remastered Standard Edition - PlayStation 4"
    assert item.platform_hint == "playstation 4"
    assert item.qty == 1
    assert classify_item(item.title, platform_hint=item.platform_hint).classification == "playstation_game"


def test_bby_alphanumeric_order_number():
    body = """Thanks for your order.

Order Number: BBY01-807206150787
Order Date: 11/14/2024

DRAGON QUEST III HD-2D Remake - PlayStation 5
Qty: 1
$19.99

Subtotal $19.99
Total $21.49
"""
    p = parse_bestbuy_receipt(body_text=body, message_id="bb1")
    assert p is not None
    assert p.order_number == "BBY01-807206150787"
    assert p.items[0].title == "DRAGON QUEST III HD-2D Remake - PlayStation 5"
    assert p.items[0].price == "$19.99"


# --- Real recovered web-view HTML (msg 564, fetched from the 'View as a Web
# page' link, 2026-07-02 order) ---
WEBVIEW_HTML = """<html><body>
<table><tr><td>Best Buy | Thanks for your order. | NES_OrderConfirmation</td></tr></table>
Nick
View Account
Thanks for shopping with us.
We're prepping your order now, and we'll let you know when it's ready.
Track your orders faster with real-time updates in the Best Buy app.
Continue in the app
Order number:
BBY01-807206150787
View order details
Status:
Waiting to be shipped
Estimated delivery:
Tuesday, July 07
Shipping to:
Nick Brett
80 Riverside Blvd
Product Details
DRAGON QUEST III HD-2D Remake - PlayStation 5
$19.99
Save $20.00
Comp. Value $39.99
Qty:
1
Your Order Summary.
Subtotal
$19.99
Shipping
FREE
Estimated Sales Tax
$1.77
Total
$21.76
View order details
What you should know.
</body></html>
"""


def test_webview_html_recovery_shape():
    p = parse_bestbuy_receipt(body_html=WEBVIEW_HTML, message_id="564")
    assert p is not None
    assert p.order_number == "BBY01-807206150787"
    assert len(p.items) == 1  # Save $20.00 / Comp. Value / Qty rows excluded
    item = p.items[0]
    assert item.title == "DRAGON QUEST III HD-2D Remake - PlayStation 5"
    assert item.price == "$19.99"
    assert item.qty == 1
    assert p.subtotal == "$19.99"
    assert p.tax == "$1.77"
    assert p.total == "$21.76"
    assert classify_item(item.title).classification == "playstation_game"
