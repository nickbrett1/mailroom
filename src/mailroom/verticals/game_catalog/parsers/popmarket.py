"""PopMarket (popmarket.com) order-confirmation parser (text body).

Verified against the archive (msgvault 2026-09-14): sender
`noreply@popmarket.com`, subject "Your PopMarket order #<n>". The
confirmation's text body carries the purchase facts:

    Thank you for your order!
    Your order confirmation number is: 0121-2412-1125SA
    ...
    1/12/2025 12:41:00 PM

    -------------
    Today's Order
    -------------

    Product Qty Cost Total

    *Shin Megami Tensei V: Vengeance Steelbook Edition for Playstation 5*
    Playstation 5
    730865220786
    Video Game
    Availability: In Stock

    Get it between Fri. Jan 17 - Mon. Jan 20 to New York 1 $24.99 $24.99
    *Subtotal*
    ...
    Order Summary Today's Order $27.21

Each item is a bold/asterisk-wrapped title line followed by its platform
line, a UPC, "Video Game" and a shipping ETA line carrying the qty + unit
cost + line total. The platform (PlayStation 5 here) is also embedded in the
title ("... for Playstation 5"), which the classifier already picks up.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import (
    Purchase,
    PurchaseItem,
)

_ORDER_RE = re.compile(r"order confirmation number is:\s*([A-Za-z0-9-]+)", re.IGNORECASE)
# "1/12/2025 12:41:00 PM" — the only date in the body is the order timestamp.
_DATE_RE = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{4})\s+\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)", re.IGNORECASE
)
# Bold/asterisk-wrapped product title ("*<title>*").
_TITLE_RE = re.compile(r"^\*([^*]+)\*$")
# "… to New York 1 $24.99 $24.99" — qty, unit cost and line total.
_QTY_RE = re.compile(r"\b(?P<qty>\d+)\s+\$(?P<price>[\d,]+\.\d{2})\s+\$[\d,]+\.\d{2}\b")
_TOTAL_RE = re.compile(r"Order Summary\s+Today's Order\s+\$([\d,]+\.\d{2})", re.IGNORECASE)

# The standalone platform line that follows each title.
_PLATFORM_LINE_TOKENS = {
    "playstation 5",
    "playstation 4",
    "playstation 3",
    "ps5",
    "ps4",
    "ps3",
    "nintendo switch",
    "switch",
    "xbox one",
    "xbox series x",
    "pc",
}

# Section headers wrapped in the same asterisks as product titles.
_NOT_A_TITLE = {"subtotal", "total", "today's order", "bill-to", "ship-to"}


def _is_popmarket(body: str) -> bool:
    return "popmarket" in body.lower()


def parse_popmarket_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a PopMarket order confirmation, or None if not one.

    Gate: the email must mention PopMarket and carry the confirmation number;
    only item lines that expose a qty + price become purchases (a shipping /
    status email carries no price, so it yields no facts).
    """
    if not _is_popmarket(body):
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None

    lines = [re.sub(r"\s+", " ", ln).strip() for ln in body.splitlines()]
    # Only the order table (after the standalone "Today's Order" header) holds
    # items. The header repeats in the trailing "Order Summary Today's Order
    # $…" line, so match the header itself, falling back to the first mention.
    start = next(
        (i for i, ln in enumerate(lines) if ln.lower().strip(" -") == "today's order"),
        next((i for i, ln in enumerate(lines) if "today's order" in ln.lower()), 0),
    )
    section = lines[start:]

    # Collect (title -> following lines) blocks.
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for ln in section:
        if not ln:
            continue
        m = _TITLE_RE.match(ln)
        if m:
            candidate = m.group(1).strip()
            if candidate.lower() in _NOT_A_TITLE:
                current = None
                continue
            current = (candidate, [])
            blocks.append(current)
            continue
        if current is not None:
            current[1].append(ln)

    items: list[PurchaseItem] = []
    for title, tail in blocks:
        price = None
        qty = 1
        platform_hint = None
        for ln in tail:
            if platform_hint is None and ln.lower() in _PLATFORM_LINE_TOKENS:
                platform_hint = ln
            qm = _QTY_RE.search(ln)
            if qm and price is None:
                price = f"${qm.group('price')}"
                qty = int(qm.group("qty"))
        if price is None or not title:
            continue
        items.append(PurchaseItem(title=title, price=price, qty=qty, platform_hint=platform_hint))
    if not items:
        return None

    date_match = _DATE_RE.search(body)
    total = _TOTAL_RE.search(body)
    return Purchase(
        order_number=order_match.group(1),
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        total=f"${total.group(1)}" if total else None,
        message_id=message_id,
        source="popmarket",
    )
