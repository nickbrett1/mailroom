"""Walmart order/arrival parser (text body).

Verified against the archive (msgvault 2026-08-25): sender
`help@walmart.com`. Purchase facts come from the ORDER CONFIRMATION
("Nick, thanks for your order") and the ARRIVAL ("Your package arrived /
items from your order were delivered") emails, which list the item title,
platform and price inline:

    Order date: Sun, Nov 26, 2023
    Order number: 2000114-00769572
    ...
    Sold and shipped by Walmart
    Star Wars Jedi: Survivor - PlayStation 5 $30.00/EA Qty: 1
    $30.00

Shipped/tracking emails ("Shipped: items from order #…", "Your package is in
transit") carry no price and are ignored (return None). Walmart sells a lot of
non-game stuff; the classifier's platform gate keeps the catalog to
PlayStation games.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import (
    Purchase,
    PurchaseItem,
)

_ORDER_RE = re.compile(r"Order\s*number:?\s*([\d-]+)", re.IGNORECASE)
# Capture "Nov 26, 2023" (drop the leading weekday, which game_groups'
# sortable_date can't parse) — "Order date: Sun, Nov 26, 2023" -> "Nov 26, 2023".
_DATE_RE = re.compile(r"Order\s*date:?\s*(?:[A-Za-z]+,\s*)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE)
# Purchase-fact markers: order confirmations and arrivals list item + price.
_FACT_MARKERS = (
    "thanks for your order",
    "your package arrived",
    "items from your order were delivered",
    "items from your order were",
)
# An item line: "Title - Platform $30.00/EA Qty: 1" (price inline, Walmart's
# arrival/order format). Platform is the '- PlayStation 5' title suffix (the
# classifier's platform gate reads it).
_ITEM_LINE_RE = re.compile(
    r"^(?P<title>.+?)\s+(?P<price>\$\d[\d,]*\.\d{2})\s*(?:/EA|/each)?\s*Qty:\s*(?P<qty>\d+)",
    re.IGNORECASE,
)
_SOLD_BY_RE = re.compile(r"^sold\s+and\s+shipped\s+by\s+walmart", re.IGNORECASE)
_RECS_AFTER = ("you might also like", "explore more savings")


def parse_walmart_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a Walmart order/arrival receipt, or None if not one."""
    low = body.lower()
    if not any(m in low for m in _FACT_MARKERS) or "walmart" not in low:
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None
    date_match = _DATE_RE.search(body)

    items: list[PurchaseItem] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _ITEM_LINE_RE.match(s)
        if not m:
            continue
        title = m.group("title").strip()
        if _SOLD_BY_RE.search(title):
            continue
        items.append(
            PurchaseItem(
                title=title,
                price=m.group("price"),
                qty=int(m.group("qty")),
            )
        )
    if not items:
        return None

    # Order total (the 'Order total' amount, or the item's standalone $ line).
    total = None
    m = re.search(r"Order\s*total[:\s]*(\$\d[\d,]*\.\d{2})", body, re.IGNORECASE)
    if m:
        total = m.group(1)
    elif len(items) == 1:
        total = items[0].price

    return Purchase(
        order_number=order_match.group(1),
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        total=total,
        message_id=message_id,
        source="walmart",
    )
