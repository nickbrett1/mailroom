"""GameStop order-confirmation parser (text body).

Verified against the archive (msgvault, 2026-08-16): sender
`orders@em.gamestop.com`, subject "Thanks for your Gamestop.com order! Order #…".
The confirmation text body is the purchase-fact source:

    Thank you for your order, Nick!
    Order Number: 1100000027339767
    Order Date: 2/23/2021
    ...
    SHIP TO HOME / PRE-ORDER
    Shipping to 80 Riverside Blvd
      <Title> [- <Platform>]
      Platform: <X>              (explicit platform on the 2021-2022 template)
      Edition: <Y>
      Condition: <New|Pre-Owned>
      [Release Date: <d>]
      QTY: N
      $price
    ORDER SUMMARY
      Subtotal / Shipping & Handling / Estimated Tax / Estimated Total

Only confirmations create facts. "Your GameStop order has shipped" emails are
ignored (return None). Consoles/bundles/gift cards are extracted as line items
and excluded later by the classifier (platform gate).
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import (
    Purchase,
    PurchaseItem,
    money,
)

_ORDER_RE = re.compile(r"Order Number:\s*([\w-]+)", re.IGNORECASE)
_DATE_RE = re.compile(r"Order Date:\s*([\d/]+)")
_CONFIRM_MARKERS = ("Thank you for your order", "Thanks for your Gamestop.com order")
_ITEM_LABELS = ("Platform:", "Edition:", "Condition:", "Release Date:", "QTY:")
_SKIP_LINES = (
    "SHIP TO HOME",
    "PRE-ORDER",
    "VIEW ORDER DETAILS",
    "Shipping to",
    "We will send",
    "Delivers",
)
_TOTAL_LABELS = {"Subtotal": "subtotal", "Estimated Tax": "tax", "Estimated Total": "total"}


def _parse_items(region: str) -> list[PurchaseItem]:
    """Line-state parse of the item region into (title, platform, qty, price)."""
    items: list[PurchaseItem] = []
    cur: PurchaseItem | None = None
    for raw in region.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(_ITEM_LABELS):
            if cur is None:
                continue
            key, _, value = line.partition(":")
            value = value.strip()
            if key.lower() == "platform":
                cur.platform_hint = value
            elif key.lower() == "condition":
                cur.condition = value
            elif key.lower() == "qty":
                m = re.search(r"\d+", value)
                if m:
                    cur.qty = int(m.group(0))
            continue
        if line.startswith(_SKIP_LINES) or line.startswith(("https://", "http://")):
            continue
        price = money(line)
        if price is not None and cur is not None:
            cur.price = price
            items.append(cur)
            cur = None
            continue
        # New item title (may carry a ' - <Platform>' suffix; the explicit
        # Platform: line is the stronger hint for the classifier).
        cur = PurchaseItem(title=line)
    return items


def _parse_totals(summary: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for label, key in _TOTAL_LABELS.items():
        m = re.search(re.escape(label) + r"\s*(?:\n\s*)*(\$\d[\d,]*\.\d{2})", summary)
        if m:
            out[key] = m.group(1)
    return out


def parse_gamestop_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a GameStop order-confirmation text body, or None if not one."""
    if not any(marker in body for marker in _CONFIRM_MARKERS):
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None
    date_match = _DATE_RE.search(body)

    # Items live between 'SHIP TO HOME'/'PRE-ORDER'/'Shipping to' and the summary.
    start = -1
    for marker in ("SHIP TO HOME", "PRE-ORDER", "Shipping to"):
        idx = body.find(marker)
        if idx != -1:
            start = idx
            break
    end = body.find("ORDER SUMMARY", start)
    if end == -1:
        end = body.find("Payment Method", start)
    if start == -1 or end == -1 or end <= start:
        return None

    items = _parse_items(body[start:end])
    if not items:
        return None
    totals = _parse_totals(body[end:]) if "ORDER SUMMARY" in body[end:] else {}

    return Purchase(
        order_number=order_match.group(1),
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        subtotal=totals.get("subtotal"),
        tax=totals.get("tax"),
        total=totals.get("total"),
        message_id=message_id,
        source="gamestop",
    )
