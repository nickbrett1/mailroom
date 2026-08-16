"""Best Buy order-confirmation parser (HTML body).

Per memo: sender orders@emailinfo.bestbuy.com, subject "Thanks for your order."
Body is HTML (body_html) — the text body is unusable (only "View as a Web
page" / "View full message" links). This parser therefore prefers `body_html`,
falls back to `body_text`, and extracts items from the tag-stripped lines:
title (+ platform suffix), Qty, $price, plus order number/date.

Reality check (2026-08-16): in THIS archive every modern Best Buy
confirmation (bestbuyinfo@emailinfo.bestbuy.com, "Thanks for your order.")
was archived with NO usable body — body_text is just tracking links and
body_html is empty, so there are currently zero extractable Best Buy facts.
The parser is built to the memo's template and validated with a realistic
HTML sample; the asset wiring will simply yield no purchases until bodies
with content are archived.

Only order confirmations create facts; shipped/delivered/pickup notices ->
None.
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

_ORDER_RE = re.compile(r"Order\s*(?:Number|#)?\s*:?\s*#?(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:Order\s*Date|Date)[^:\n]*:\s*([A-Za-z]{3,9} \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{4})"
)
_QTY_RE = re.compile(r"(?:Qty|Quantity)\s*:?\s*(\d+)", re.IGNORECASE)
_LABEL_LINES = (
    "subtotal",
    "tax",
    "total",
    "shipping",
    "discount",
    "order number",
    "order date",
    "sku",
    "item",
    "store",
    "best buy",
    "status",
    "view",
    "thanks for your order",
    "estimated",
    "delivery",
    "payment",
    "billing",
    "pickup",
    "your order",
    "hi ",
    "hello",
    "price",
    "qty",
)
_SKIP_LINES = ("http://", "https://", "www.", "tel:", "mailto:")


def _parse_items(text: str) -> list[PurchaseItem]:
    items: list[PurchaseItem] = []
    cur: PurchaseItem | None = None
    for raw in text.splitlines():
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
        title = line
        hint = None
        if " - " in title:
            maybe = title.rsplit(" - ", 1)[1].strip().lower()
            if maybe in PLATFORM_TOKENS:
                hint = maybe
        cur = PurchaseItem(title=title, platform_hint=hint)
    return items


def parse_bestbuy_receipt(
    body_text: str = "",
    body_html: str = "",
    message_id: str | None = None,
) -> Purchase | None:
    """Parse a Best Buy order confirmation. Prefer the HTML body."""
    source = body_html if body_html else body_text
    text = strip_html(source) if body_html else source
    if "thanks for your order" not in text.lower():
        return None
    order_match = _ORDER_RE.search(text)
    if not order_match:
        return None
    date_match = _DATE_RE.search(text)
    items = _parse_items(text)
    if not items:
        return None
    total_m = re.search(r"\bTotal\b[^\$\n]*(\$\d[\d,]*\.\d{2})", text, re.IGNORECASE)
    subtotal_m = re.search(r"Subtotal[^\$\n]*(\$\d[\d,]*\.\d{2})", text, re.IGNORECASE)
    tax_m = re.search(r"\bTax\b[^\$\n]*(\$\d[\d,]*\.\d{2})", text, re.IGNORECASE)
    return Purchase(
        order_number=order_match.group(1),
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        subtotal=subtotal_m.group(1) if subtotal_m else None,
        tax=tax_m.group(1) if tax_m else None,
        total=total_m.group(1) if total_m else None,
        message_id=message_id,
        source="bestbuy",
    )
