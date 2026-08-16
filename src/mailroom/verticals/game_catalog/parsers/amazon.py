"""Amazon order parser (text body).

Verified against the archive (msgvault, 2026-08-16): senders
auto-confirm@amazon.com (Ordered:), shipment-tracking@amazon.com (Shipped:),
order-update@amazon.com (Delivered:). All three share the same body shape:

    Thanks for your order, Nick!
    ...
    Order #
    114-1970161-5765038
    View or edit order
    https://www.amazon.com/...orderID=114-1970161-5765038...

    * Sonic Superstars - PlayStation 5
      Quantity: 1
      15.56 USD

    Total
    16.94 USD

One email can contain MULTIPLE order blocks (each with its own Order #, items
and Total) — so this parser returns a *list* of Purchases, one per order block.

Amazon sends ~3 emails per order (Ordered / Shipped / Delivered); each carries
the same (order #, item) facts, so the merge layer dedupes on
(order_number, item_key) — never double count. The 'Out for delivery' variant
may omit the price line; the parser tolerates that.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import Purchase, PurchaseItem

_ORDER_RE = re.compile(r"Order #\s*(\d{3}-\d{7}-\d{7})", re.IGNORECASE)
_ITEM_RE = re.compile(r"(?m)^\* (.*)$")
_QTY_RE = re.compile(r"Quantity:\s*(\d+)", re.IGNORECASE)
_PRICE_RE = re.compile(r"^\s*([\d,]+\.\d{2})\s*USD\s*$", re.MULTILINE)
_TOTAL_RE = re.compile(r"(?:Grand\s+)?Total\s*:?\s*\n?\s*([\d,]+\.\d{2})\s*USD", re.IGNORECASE)


def _parse_order_block(block: str, message_id: str | None) -> Purchase | None:
    order_match = _ORDER_RE.search(block)
    if not order_match:
        return None
    items: list[PurchaseItem] = []
    for m in _ITEM_RE.finditer(block):
        title = m.group(1).strip()
        if not title:
            continue
        tail = block[m.end() :]
        qty = 1
        price: str | None = None
        qty_match = _QTY_RE.search(tail)
        if qty_match:
            qty = int(qty_match.group(1))
            price_match = _PRICE_RE.search(tail[qty_match.end() :])
            if price_match:
                price = f"${price_match.group(1)}"
        items.append(PurchaseItem(title=title, price=price, qty=qty))
    if not items:
        return None
    total_match = _TOTAL_RE.search(block)
    return Purchase(
        order_number=order_match.group(1),
        purchased_at=None,  # not present in the email body; asset uses sent_at
        items=items,
        total=f"${total_match.group(1)}" if total_match else None,
        message_id=message_id,
        source="amazon",
    )


def parse_amazon_receipt(body: str, message_id: str | None = None) -> list[Purchase]:
    """Parse an Amazon order email into one Purchase per order block."""
    # Split the body at each 'Order #' occurrence; each block is one order.
    starts = [m.start() for m in _ORDER_RE.finditer(body)]
    if not starts:
        return []
    purchases: list[Purchase] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        purchase = _parse_order_block(body[start:end], message_id)
        if purchase:
            purchases.append(purchase)
    return purchases
