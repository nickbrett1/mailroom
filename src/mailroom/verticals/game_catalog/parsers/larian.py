"""Larian merch-store order parser (Baldur's Gate 3 Deluxe Edition and other
Larian store purchases).

Verified format (live archive, msg 32465, 2024-04-22):
  "Order confirmation" / "Thanks for ordering Larian swag from our store!"
  ...
  "Item: Qty: Price:" / "Baldur's Gate 3 - Deluxe Edition PS5" / "1 $79.99"
  "Items total: $79.99 / Taxes total: $0.00 / Shipping total: $20.00 / Total: $99.99"
  "View order ( http://us.merch.larian.com/en/order/_MNXwH-R2B )"

The confirmation is the purchase fact (physical order shipped to home; the
later "Your ... PS5 Key is Here!" email is fulfillment — no new facts, the
parser returns None for it).
"""

from __future__ import annotations

import re

from .common import Purchase, PurchaseItem, clean_whitespace, money

_GATE = re.compile(r"order confirmation", re.IGNORECASE)
_GATE2 = re.compile(r"thanks for ordering larian", re.IGNORECASE)
_ORDER_URL_RE = re.compile(r"/order/([A-Za-z0-9_-]+)")
# The real bodies put the price on the SAME line as the totals:
# "... PS5\n\n1 $79.99 Items total: $79.99 ... Total: $99.99"
_PRICE_RE = re.compile(r"\b1\s+\$(?P<price>[\d,]+\.\d{2})", re.IGNORECASE)
_TOTAL_RE = re.compile(r"(?<!\w)Total:\s+\$(?P<total>[\d,]+\.\d{2})", re.IGNORECASE)


def parse_larian_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a Larian merch-store order confirmation into a Purchase."""
    lines = [clean_whitespace(l) for l in (body or "").splitlines()]
    if not any(_GATE.search(l) for l in lines) or not any(_GATE2.search(l) for l in lines):
        return None  # shipping updates / key-fulfillment emails carry no new facts

    order_number = None
    for l in lines:
        m = _ORDER_URL_RE.search(l)
        if m:
            order_number = m.group(1)
            break

    # Items live after the "Item: Qty: Price:" header; the price line also
    # carries "Items total:" / "Total:" (e.g. "1 $79.99 Items total: … Total: $99.99").
    items: list[PurchaseItem] = []
    try:
        start = next(i for i, l in enumerate(lines) if "Item: Qty: Price:" in l)
        end = next(i for i, l in enumerate(lines) if "Items total:" in l)
        section = lines[start + 1 : end + 1]
    except StopIteration:
        section = lines
    def _hint(title: str) -> str | None:
        if re.search(r"\bps5\b", title, re.IGNORECASE):
            return "ps5"
        if re.search(r"\bps4\b", title, re.IGNORECASE):
            return "ps4"
        return None

    pending_title: str | None = None
    for l in section:
        m = _PRICE_RE.search(l)
        if m and pending_title:
            items.append(PurchaseItem(title=pending_title, price=money("$" + m.group("price")), qty=1, platform_hint=_hint(pending_title)))
            pending_title = None
        elif l and ":" not in l and pending_title is None:
            pending_title = l.strip(" -*")  # title sits on its own line (no colon)
    if pending_title:
        items.append(PurchaseItem(title=pending_title, price=None, qty=1, platform_hint=_hint(pending_title)))

    # The summary line lists Items/Taxes/Shipping total then the real Total last.
    matches = list(_TOTAL_RE.finditer("\n".join(lines)))
    total = money("$" + matches[-1].group("total")) if matches else None
    if not items:
        return None
    return Purchase(
        source="larian",
        order_number=order_number,
        purchased_at=None,  # use the email received date (acquisition date)
        items=items,
        subtotal=None,
        tax=None,
        total=total,
        message_id=str(message_id) if message_id else None,
    )
