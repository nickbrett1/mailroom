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

Order confirmations are the primary fact source. The companion SHIPMENT notice
("Your item(s) has shipped and is on its way to you", also the 2023 "Your
GameStop order has shipped" template) is parsed too: when a confirmation lists a
console BUNDLE as a single line, the bundled games are only itemized in the
shipment email. For ordinary orders the shipment titles match the confirmation
titles, so the (order_number, title) key dedupes them and nothing is double
counted. Consoles/bundles/gift cards are extracted as line items and excluded
later by the classifier (platform gate).
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

# Shipment / fulfillment notice templates. The 2021 template is "Your item(s)
# has shipped and is on its way to you" (item section headed "Your Item(s)");
# the 2023 template is "Your GameStop order has shipped" (item section headed
# "Shipping Now"). Items are title / QTY / $price with no Platform line.
_SHIPMENT_START_MARKERS = ("Your Item(s)", "Shipping Now")
_SHIPMENT_END_MARKERS = (
    "ORDER SUMMARY",
    "When Will My Order Deliver?",
    "Can I Make A Return?",
    "Don’t See Your Questions?",
    "Don't See Your Questions?",
)


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


def parse_gamestop_shipment(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a GameStop shipment notice into its itemized line items.

    Exists because a bundle confirmation hides the games: GameStop's
    order-confirmation template lists a console bundle as ONE line (e.g.
    "PlayStation 5 Spider-Man Ultimate Edition Bundle with $20 GameStop Gift
    Card"), which the classifier excludes as hardware, while the shipment
    notice itemizes the bundle contents. Verified against the archive
    (msgvault msg 66500, orders@em.gamestop.com, 2021-03-01, order
    1100000027339767): it itemizes "Marvel's Spider-Man: Miles Morales Ultimate
    Launch Edition" ($69.99) alongside the console / controller / gift card.
    Returns None when the body is not a shipment notice or carries no items.
    """
    start = -1
    for marker in _SHIPMENT_START_MARKERS:
        idx = body.find(marker)
        if idx != -1:
            start = idx
            break
    if start == -1:
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None

    end = -1
    for marker in _SHIPMENT_END_MARKERS:
        idx = body.find(marker, start)
        if idx != -1 and (end == -1 or idx < end):
            end = idx
    region = body[start:end] if end != -1 else body[start:]
    items = _parse_items(region)
    if not items:
        return None

    date_match = _DATE_RE.search(body)
    totals = _parse_totals(body[end:]) if end != -1 and "ORDER SUMMARY" in body[end:] else {}
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
