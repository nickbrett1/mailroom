"""P.C. Richard & Son order-confirmation parser (text body).

Verified against the archive (msgvault 2026-09-14): sender
`support@pcrichard.com`, subject "Thank You for your Order #: <n>". The
confirmation's text body carries the purchase facts:

    Thank you for shopping at
    P.C. Richard & Son! Order # 012-7135059
    ...
    Order Details *Order Number* Order Date 012-7135059 10/26/2025

    Items

    <Title - Platform> ( <link> )

    <Title - Platform> ( <link> ) Model: <model> Qty: 1 $14.00

    ...
    Tax: $1.69 Shipping: $4.99 Total: $20.68

The item appears twice — once as a bare title with a link, once with the
model/qty/price inline — so only the line carrying `Model:`/`Qty:` creates an
item. The companion "Your Order Has Shipped!" email lists the same item but
carries no price, so it yields no facts (return None): only confirmations
create purchases.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import (
    Purchase,
    PurchaseItem,
)

# Order number is hyphenated ("012-7135059"); subject and body both carry it.
_ORDER_RE = re.compile(r"Order\s*#\s*:?\s*([\d-]{6,})", re.IGNORECASE)
# "Order Details *Order Number* Order Date 012-7135059 10/26/2025"
_DATE_RE = re.compile(r"Order\s*Date\s+[\d-]+\s+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
# Inline "View item" links: "( https://click.e.pcrichard.com/?qs=… )".
_LINK_RE = re.compile(r"\(\s*https?://\S+\s*\)")
# A purchase line: "<Title - Platform> Model: <model> Qty: <n> $<price>".
_ITEM_RE = re.compile(
    r"^(?P<title>.+?)\s+Model:\s*\S+\s+Qty:\s*(?P<qty>\d+)\s+(?P<price>\$\d[\d,]*\.\d{2})\s*$",
    re.IGNORECASE,
)
_TAX_RE = re.compile(r"Tax:\s*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)
_TOTAL_RE = re.compile(r"Total:\s*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)


def _is_pcrichard(body: str) -> bool:
    low = body.lower()
    return "p.c. richard" in low or "pc richard" in low or "pcrichard" in low


def parse_pcrichard_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a P.C. Richard & Son order confirmation, or None if not one.

    Gate: the email must be from P.C. Richard & Son and carry at least one item
    line WITH a price — the shipping notification lists the item but no price,
    so it returns None.
    """
    if not _is_pcrichard(body):
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None

    items: list[PurchaseItem] = []
    for raw in body.splitlines():
        line = re.sub(r"\s+", " ", _LINK_RE.sub(" ", raw)).strip()
        if "model:" not in line.lower() or "qty:" not in line.lower():
            continue
        m = _ITEM_RE.match(line)
        if not m:
            continue
        title = m.group("title").strip(" -")
        if not title:
            continue
        items.append(
            PurchaseItem(title=title, price=m.group("price"), qty=int(m.group("qty")))
        )
    if not items:
        return None

    date_match = _DATE_RE.search(body)
    tax = _TAX_RE.search(body)
    total = _TOTAL_RE.search(body)
    return Purchase(
        order_number=order_match.group(1),
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        tax=tax.group(1) if tax else None,
        total=total.group(1) if total else None,
        message_id=message_id,
        source="pcrichard",
    )
