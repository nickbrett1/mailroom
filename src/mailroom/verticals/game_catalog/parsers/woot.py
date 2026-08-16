"""Woot order-confirmation parser (text body).

Verified (msgvault 2026-08-16): sender `no-reply@woot.com`; ONLY "Woot says
thanks for your order #…" confirmations create facts:

    Order Confirmation: #212839495 | Woot says thank you for your order
    ...
    ORDER DETAILS
    -----------------------------------
    Order Date: Wednesday, August 5, 2026
    Order Number: 212839495

    Estimated delivery date: ...
    8BitDo 64 Bluetooth Controller
    $34.99  $44.99 22% off Reference Price
    Sold by Woot LLC
    Condition: New
    Model: Playstation 5
    Quantity: 1
    Item Subtotal $34.99

    Subtotal: $34.99
    Shipping: $0.00
    Tax: $3.11
    Total: $38.10

The first $ amount on the price line is the item price (second is the
reference price). Platform comes from 'Model: …' or the '(PS5)' title suffix.
"Woo hoo! Your Woot order's a-comin'!" (shipped) and "Rejoice! Your Package
Has Been Delivered!" = no new facts. Heavily mixed catalog -> classifier gate.
"""

from __future__ import annotations

import html as _html
import re

from mailroom.verticals.game_catalog.parsers.common import (
    PLATFORM_TOKENS,
    Purchase,
    PurchaseItem,
    money,
)

_ORDER_RE = re.compile(r"Order\s*Number:\s*(\d+)", re.IGNORECASE)
_HEADER_ORDER_RE = re.compile(r"#(\d{6,})")
_DATE_RE = re.compile(r"Order\s*Date:\s*([A-Za-z]+,?\s+[A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE)
_QTY_RE = re.compile(r"Quantity:\s*(\d+)", re.IGNORECASE)
_MODEL_RE = re.compile(r"^\s*Model:\s*(.+)$", re.IGNORECASE)
_CONDITION_RE = re.compile(r"^\s*Condition:\s*(.+)$", re.IGNORECASE)
_TOTAL_LABEL_RE = re.compile(r"(Subtotal|Shipping|Tax|Total):\s*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)
_SKIP_PREFIXES = (
    "sold by",
    "estimated delivery date",
    "order date",
    "order number",
    "shipping address",
    "payment method",
    "if you placed",
    "missed it",
    "thanks for your purchase",
    "thanks again",
    "hooray",
    "hey wooter",
    "woot",
    "reference price",
    "color:",
    "size:",
    "style:",
    "variation:",
    "choose:",
    "option:",
)
_SEPARATOR_RE = re.compile(r"^[-=\s]+$")


def _parse_items(region: str) -> list[PurchaseItem]:
    items: list[PurchaseItem] = []
    cur: PurchaseItem | None = None
    for raw in region.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _SEPARATOR_RE.fullmatch(line):
            continue
        low = line.lower()
        if low.startswith("item subtotal"):
            # Item block terminator: capture the definitive amount, close item.
            if cur is not None:
                if cur.price is None:
                    cur.price = money(line)
                items.append(cur)
                cur = None
            continue
        if low.startswith(_SKIP_PREFIXES):
            continue
        qty_match = _QTY_RE.search(line)
        if qty_match and cur is not None:
            cur.qty = int(qty_match.group(1))
            continue
        model = _MODEL_RE.match(line)
        if model and cur is not None:
            if cur.platform_hint is None:
                cur.platform_hint = model.group(1).strip()
            continue
        cond = _CONDITION_RE.match(line)
        if cond and cur is not None:
            cur.condition = cond.group(1).strip()
            continue
        price = money(line)
        if price is not None:
            if cur is None:
                continue  # totals amount with no open item
            if cur.price is None:
                cur.price = price
            continue
        # New title line (may carry '(PS5)' suffix). Items close at the
        # 'Item Subtotal' line; if a priced item is still open here it means
        # the block lacked a terminator, so close it now.
        if cur is not None and cur.price is not None:
            items.append(cur)
        title = _html.unescape(line)
        hint = None
        paren = re.search(r"\(([^)]+)\)\s*$", title)
        if paren and paren.group(1).strip().lower() in PLATFORM_TOKENS:
            hint = paren.group(1).strip().lower()
        cur = PurchaseItem(title=title, platform_hint=hint)
    if cur is not None and cur.price is not None:
        items.append(cur)
    return items


def parse_woot_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a Woot order confirmation, or None if not one."""
    if "order confirmation" not in body.lower():
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        header = _HEADER_ORDER_RE.search(body)
        if header:
            order_match = header
    if not order_match:
        return None
    date_match = _DATE_RE.search(body)

    details_at = body.upper().find("ORDER DETAILS")
    start = details_at if details_at != -1 else 0
    end = body.upper().find("SHIPPING & PAYMENT", start)
    region = body[start:end] if end != -1 else body[start:]
    items = _parse_items(region)
    if not items:
        return None

    totals: dict[str, str] = {}
    for m in _TOTAL_LABEL_RE.finditer(body):
        totals[m.group(1).lower()] = m.group(2)

    return Purchase(
        order_number=order_match.group(1),
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        subtotal=totals.get("subtotal"),
        tax=totals.get("tax"),
        total=totals.get("total"),
        message_id=message_id,
        source="woot",
    )
