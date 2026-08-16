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

NOTE (2026-08-16): the Shopify emails live under the generic Shopify sender
`store+<shop-id>@t.shopifyemail.com` — Limited Run Games
(store+9127444@…), LRG SOHO (store+59724660849@…, "Receipt for order #1005"),
Atari® (store+60936585381@…, "Order #165370 confirmed"). Live-validated
against those real bodies; other stores (books, non-game shops) parse the
same way and are excluded by the classifier.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import (
    PLATFORM_TOKENS,
    Purchase,
    PurchaseItem,
    is_platform_token,
    money,
    strip_html,
)

_CONFIRM_MARKERS = (
    "thanks for your order",
    "thank you for your purchase",
    "order confirmed",
    "receipt for order",
)
_ORDER_RE = re.compile(r"Order\s*(?:Number|#)?\s*:?\s*#?(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:Order|Placed on|Date)[^:\n]*:\s*([A-Za-z]{3,9} \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{4})"
)
_QTY_RE = re.compile(r"(?:Qty|Quantity)\s*:?\s*(\d+)", re.IGNORECASE)
_INLINE_QTY_RE = re.compile(r"\s*[×x]\s*(\d+)\s*$")

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
    "thank you for your purchase",
    "order confirmed",
    "order #",
    "order summary",
    "visit our store",
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
_BARE_PRICE_RE = re.compile(r"^\d[\d,]*\.\d{2}$")
_SEPARATOR_RE = re.compile(r"^[-*=—_·\s]+$")


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
        if low.startswith(_LABEL_LINES) or low.startswith(_SKIP_LINES):
            continue
        qty_match = _QTY_RE.search(line)
        if qty_match and cur is not None:
            cur.qty = int(qty_match.group(1))
            continue
        standalone_qty = re.fullmatch(r"[×x]\s*(\d+)", line)
        if standalone_qty and cur is not None:
            cur.qty = int(standalone_qty.group(1))
            continue
        platform = is_platform_token(line)
        if platform and cur is not None:
            cur.platform_hint = platform
            continue
        price = money(line)
        if price is None and cur is not None and _BARE_PRICE_RE.fullmatch(line):
            price = f"${line}"  # Shopify themes sometimes omit the '$'
        if price is not None and cur is not None:
            cur.price = price
            items.append(cur)
            cur = None
            continue
        if price is not None:
            continue  # bare total with no open item — ignore
        if _BARE_PRICE_RE.fullmatch(line):
            continue  # bare amount (e.g. '39.99' under a totals label) — never a title
        # Non-platform line while an item is awaiting a price = variant
        # continuation (Shopify sub-variants, e.g. Atari watch-band designs).
        if cur is not None and cur.price is None:
            cur.title = f"{cur.title} - {line}"
            continue
        # Title line (possibly with inline ' - PS5' variant suffix or ' × N' qty).
        title = line
        if title.startswith(_BULLETS):
            title = title[2:].strip()
        hint = None
        if " - " in title:
            maybe = title.rsplit(" - ", 1)[1].strip().lower()
            if maybe in PLATFORM_TOKENS:
                hint = maybe
        inline_qty = _INLINE_QTY_RE.search(title)
        qty = 1
        if inline_qty:
            qty = int(inline_qty.group(1))
            title = title[: inline_qty.start()].rstrip()
        cur = PurchaseItem(title=title, platform_hint=hint, qty=qty)
    return items


def parse_shopify_receipt(
    body_text: str = "",
    body_html: str = "",
    message_id: str | None = None,
) -> Purchase | None:
    """Parse a Shopify order confirmation. Prefer the HTML body when present."""
    body = strip_html(body_html) if body_html else body_text
    if not body:
        return None
    low = body.lower()
    if not any(marker in low for marker in _CONFIRM_MARKERS):
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None
    date_match = _DATE_RE.search(body)

    # Items live under the explicit "Order summary" header when present;
    # otherwise fall back to the whole body (canonical minimal layouts).
    summary_at = body.lower().find("order summary")
    region = body[summary_at:] if summary_at != -1 else body
    items = _parse_items(region)
    if not items:
        return None

    total_m = re.search(r"\bTotal\b(?! paid)\s*\n?\s*(\$?\d[\d,]*\.\d{2})", body, re.IGNORECASE)
    subtotal_m = re.search(r"Subtotal\s*\n?\s*(\$?\d[\d,]*\.\d{2})", body, re.IGNORECASE)
    tax_m = re.search(r"Taxe?s?\s*\n?\s*(\$?\d[\d,]*\.\d{2})", body, re.IGNORECASE)
    total = f"${total_m.group(1).lstrip('$')}" if total_m else None
    subtotal = f"${subtotal_m.group(1).lstrip('$')}" if subtotal_m else None
    tax = f"${tax_m.group(1).lstrip('$')}" if tax_m else None

    return Purchase(
        order_number=order_match.group(1),
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        subtotal=subtotal,
        tax=tax,
        total=total,
        message_id=message_id,
        source="shopify",
    )
