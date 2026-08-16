"""GameFly purchase-confirmation parser (text body).

Verified (msgvault 2026-08-16): sender `support@gamefly.com`. ONLY "Your
GameFly Order Has Been Confirmed" emails create facts:

    ORDER CONFIRMATION

    Hi Nick,
    Thanks for shopping with GameFly! Your purchase details are listed below.
    ...
    Order Number:  72519662
    Order Submitted:  9/7/2025
    ...
    Order Summary

      Item:  Silent Hill 2 (PS5)
      Price:  $29.99
      Tracking Number:  N/A

    Subtotal:          $29.99
    Tax:               $2.92
    Shipping:          $2.98
    Order Total:       $35.89

"GameFly Order Status Update" (shipped) = no new facts. "GameFly - Game
Rental" Bizrate surveys are the rental flow — not purchases, ignored.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import (
    PLATFORM_TOKENS,
    Purchase,
    PurchaseItem,
)

_ORDER_RE = re.compile(r"Order Number:\s*(\d+)")
_DATE_RE = re.compile(r"Order Submitted:\s*([\d/]+)")
_ITEM_RE = re.compile(r"^\s*Item:\s*(.+)$", re.MULTILINE)
_PRICE_RE = re.compile(r"^\s*Price:\s*(\$\d[\d,]*\.\d{2})\s*$", re.MULTILINE)
_SUBTOTAL_RE = re.compile(r"Subtotal:\s*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)
_TAX_RE = re.compile(r"Tax:\s*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)
_TOTAL_RE = re.compile(r"Order Total:\s*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)


def parse_gamefly_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a GameFly purchase confirmation, or None if not one."""
    if "order confirmation" not in body.lower():
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None
    date_match = _DATE_RE.search(body)

    items: list[PurchaseItem] = []
    for m in _ITEM_RE.finditer(body):
        title = m.group(1).strip()
        if not title:
            continue
        price_match = _PRICE_RE.search(body[m.end() :])
        price = price_match.group(1) if price_match else None
        hint = None
        paren = re.search(r"\(([^)]+)\)\s*$", title)
        if paren and paren.group(1).strip().lower() in PLATFORM_TOKENS:
            hint = paren.group(1).strip().lower()
        items.append(PurchaseItem(title=title, price=price, platform_hint=hint))
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
        source="gamefly",
    )
