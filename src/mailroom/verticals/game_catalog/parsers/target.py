"""Target order-confirmation parser (text body).

Verified (msgvault 2026-08-16): senders orders@oe.target.com /
orders@oe1.target.com / orders@target.com; subject "Thanks for shopping with
us! Here's your order #: <n>." Confirmations have:

    Order #912003531191358
    Thanks for your order, Nick!
    Placed June 26, 2026

    Order total
    $20.68

    ...
    Delivers to: Nick Brett, ...

    [url]
    The Last of Us Part 1 - PlayStation 5
    Qty: 1
    $39.99 / ea
    Arriving by Wed, Nov 29

    Order Summary
    Subtotal (1 item) ... $19.99
    Estimated taxes ... $1.69
    Total ... $20.68

Only confirmations create facts (arrived / ready for pickup / refund / status
emails -> None). ~1,206 emails, mostly status -> the asset layer dedupes by
(order #, item) and the classifier gates the mixed catalog.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import Purchase, PurchaseItem, money

_ORDER_RE = re.compile(r"Order\s*#\s*:?\s*(?:https?://\S+\s*)?(\d{6,})")
_DATE_RE = re.compile(r"Placed\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})")
_QTY_RE = re.compile(r"Qty:\s*(\d+)", re.IGNORECASE)
_PRICE_PER_EA_RE = re.compile(r"(\$\d[\d,]*\.\d{2})\s*/?\s*ea", re.IGNORECASE)
_SUBTOTAL_RE = re.compile(r"Subtotal[^$]*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)
_TAX_RE = re.compile(r"Estimated\s+taxes[^$]*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)
_TOTAL_RE = re.compile(r"\bTotal\b\s*\n?\s*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)
_SKIP_PREFIXES = (
    "delivers to",
    "arrives",
    "arriving",
    "visit order details",
    "order total",
    "you saved",
    "shipping",
    "up next",
    "just for you",
    "rate & review",
    "write a review",
    "need to make changes",
    "https://",
    "http://",
    "thanks for your order",
    "placed ",
    "now just sit back",
    "we'll send you",
    "target circle",
)
_SEPARATOR_RE = re.compile(r"^[\s\u2007\u034f]+$")


def _parse_items(region: str) -> list[PurchaseItem]:
    items: list[PurchaseItem] = []
    cur: PurchaseItem | None = None
    for raw in region.splitlines():
        line = raw.strip().replace("&#8199;", " ").replace("&#847;", " ").replace("\u2007", " ").replace("\u034f", " ")
        if not line:
            continue
        if _SEPARATOR_RE.fullmatch(line):
            continue
        low = line.lower()
        if low.startswith(_SKIP_PREFIXES):
            continue
        qty_match = _QTY_RE.search(line)
        if qty_match and cur is not None:
            cur.qty = int(qty_match.group(1))
            continue
        per_ea = _PRICE_PER_EA_RE.search(line)
        if per_ea and cur is not None:
            cur.price = per_ea.group(1)
            items.append(cur)
            cur = None
            continue
        price = money(line)
        if price is not None and cur is not None and cur.price is None:
            cur.price = price
            items.append(cur)
            cur = None
            continue
        if price is not None:
            continue  # totals amount with no open item
        # New title.
        cur = PurchaseItem(title=line)
    return items


def parse_target_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a Target order confirmation, or None if not one.

    Gate: only emails with an 'Order Summary' + a placed date are
    confirmations; status/arrival/pickup/refund notices lack the summary.
    """
    low = body.lower()
    if "order summary" not in low:
        return None
    order_match = _ORDER_RE.search(body)
    date_match = _DATE_RE.search(body)
    if not order_match or not date_match:
        return None

    # Items live between 'Delivers to:' and 'Order Summary'.
    start = body.find("Delivers to:")
    end = body.find("Order Summary", start)
    region = body[start:end] if start != -1 and end > start else body
    items = _parse_items(region)
    if not items:
        return None

    subtotal = _SUBTOTAL_RE.search(body)
    tax = _TAX_RE.search(body)
    total = _TOTAL_RE.search(body)
    return Purchase(
        order_number=order_match.group(1),
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        subtotal=subtotal.group(1) if subtotal else None,
        tax=tax.group(1) if tax else None,
        total=total.group(1) if total else None,
        message_id=message_id,
        source="target",
    )
