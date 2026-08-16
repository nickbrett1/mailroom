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
        body_text="Thanks for your order.\nOrder Number: 999\nOrder Date: 1/2/2023\nAstro Bot - PS5\nQty: 1\n$59.99\nTotal $59.99",
        message_id="x",
    )
    assert p is not None
    assert p.order_number == "999"
    assert p.items[0].title == "Astro Bot - PS5"


def test_unrelated_body_returns_none():
    assert parse_bestbuy_receipt(body_text="some random email") is None
