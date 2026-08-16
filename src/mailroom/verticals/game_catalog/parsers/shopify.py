"""Generic Shopify store order-confirmation parser (LRG / LRG SOHO / Atari).

Template (per memo + canonical Shopify confirmation emails):
  "Thanks for your order!" / "Order confirmed"
  Order #<n> (or Order Number: #<n>), order date
  one block per line item:
    <Title> [ - <Variant>]     variant line may be on its own line (platform)
    PS5                        <- variant line = platform
    Qty: 1                     (also 'Quantity: N')
    $34.99
  Subtotal / Tax / Total

Only confirmations create facts (shipped/fulfilled/cancelled -> None).
Books/magazines, trading cards and non-game merch are extracted as items and
excluded later by the classifier (platform gate / non-game hints).

NOTE (2026-08-16): the Gmail archive currently contains no LRG / Atari
Shopify confirmation emails (those orders arrive via the Shop app / Zendesk,
and the archive's Atari order email is a tracking notice with no item detail
in the text body). This parser is built to the memo's documented template and
tested against canonical Shopify layouts; wire senders
support@limitedrungames.com / shop@lrgsoho.com / support@atari.com as the
archive accumulates them.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import (
    PLATFORM_TOKENS,
    Purchase,
    PurchaseItem,
    is_platform_token,
    money,
)

_CONFIRM_MARKERS = ("thanks for your order", "order confirmed")
_ORDER_RE = re.compile(r"Order\s*(?:Number|#)?\s*:?\s*#?(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:Order|Placed on|Date)[^:\n]*:\s*([A-Za-z]{3,9} \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{4})"
)
_QTY_RE = re.compile(r"(?:Qty|Quantity)\s*:?\s*(\d+)", re.IGNORECASE)

_LABEL_LINES = (
    "subtotal",
    "tax",
    "total",
    "shipping",
    "discount",
    "view your order",
    "order details",
    "items",
    "your order",
    "thanks for your order",
    "order confirmed",
    "shipping address",
    "billing address",
    "payment method",
    "customer information",
    "store",
    "download",
    "fulfilled",
    "placed on",
    "order date",
    "order number",
    "hi ",
    "hello",
    "date",
    "email",
    "phone",
)
_SKIP_LINES = ("http://", "https://", "www.", "tel:", "mailto:")
_BULLETS = ("* ", "• ", "· ", "‣ ")


def _parse_items(region: str) -> list[PurchaseItem]:
    items: list[PurchaseItem] = []
    cur: PurchaseItem | None = None
    for raw in region.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(_LABEL_LINES) or low.startswith(_SKIP_LINES):
            continue
        qty_match = _QTY_RE.search(line)
        if qty_match and cur is not None:
            cur.qty = int(qty_match.group(1))
            continue
        platform = is_platform_token(line)
        if platform and cur is not None:
            cur.platform_hint = platform
            continue
        price = money(line)
        if price is not None and cur is not None:
            cur.price = price
            items.append(cur)
            cur = None
            continue
        # Title line (possibly with inline ' - PS5' variant suffix).
        title = line
        if title.startswith(_BULLETS):
            title = title[2:].strip()
        hint = None
        if " - " in title:
            maybe = title.rsplit(" - ", 1)[1].strip().lower()
            if maybe in PLATFORM_TOKENS:
                hint = maybe
        cur = PurchaseItem(title=title, platform_hint=hint)
    return items


def parse_shopify_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a Shopify order-confirmation body, or None if not one."""
    low = body.lower()
    if not any(marker in low for marker in _CONFIRM_MARKERS):
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None
    date_match = _DATE_RE.search(body)

    items = _parse_items(body)
    if not items:
        return None

    total_m = re.search(r"\bTotal\b[^\$\n]*(\$\d[\d,]*\.\d{2})", body, re.IGNORECASE)
    subtotal_m = re.search(r"Subtotal[^\$\n]*(\$\d[\d,]*\.\d{2})", body, re.IGNORECASE)
    tax_m = re.search(r"Tax[^\$\n]*(\$\d[\d,]*\.\d{2})", body, re.IGNORECASE)

    return Purchase(
        order_number=order_match.group(1),
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        subtotal=subtotal_m.group(1) if subtotal_m else None,
        tax=tax_m.group(1) if tax_m else None,
        total=total_m.group(1) if total_m else None,
        message_id=message_id,
        source="shopify",
    )
