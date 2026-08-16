"""Best Buy order-confirmation parser (HTML body).

Per memo: sender `bestbuyinfo@emailinfo.bestbuy.com`, subject "Thanks for
your order."; order numbers like `BBY01-807206150787`.

IMPORTANT (verified 2026-08-16): msgvault archives Best Buy confirmations
with an EMPTY body — body_text is only "View as a Web page" / "View full
message" click-tracking links and body_html is empty. The real content lives
behind the "View as a Web page" URL (fetched via
`clients.recover_webview_html`). The web-view HTML (this parser's target)
looks like:

    Order number:
    BBY01-807206150787
    ...
    Product Details
    DRAGON QUEST III HD-2D Remake - PlayStation 5
    $19.99
    Save $20.00
    Comp. Value $39.99
    Qty:
    1
    Your Order Summary.
    Subtotal          $19.99
    Shipping          FREE
    Estimated Sales Tax  $1.77
    Total             $21.76

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

# Best Buy order numbers: 'BBY01-807206150787' or plain digits.
_ORDER_RE = re.compile(r"Order\s*(?:number|#)\s*:?\s*#?\s*([A-Z]{2,4}\d{2,3}-\d+|\d{6,})", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:Order\s*Date|Placed)[^:\n]*:\s*([A-Za-z]{3,9} \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{4})"
)
_QTY_RE = re.compile(r"(?:Qty|Quantity)\s*:?\s*(\d+)", re.IGNORECASE)
_LABEL_LINES = (
    "subtotal",
    "shipping",
    "estimated sales tax",
    "sales tax",
    "tax",
    "total",
    "discount",
    "order number",
    "order date",
    "status:",
    "estimated delivery",
    "shipping to",
    "view order details",
    "product details",
    "your order summary",
    "order summary",
    "see inside",
    "view account",
    "continue in the app",
    "thanks for shopping",
    "we're prepping",
    "track your orders",
    "waiting to be shipped",
    "when your order ships",
    "we'll send a separate",
    "don't miss",
    "shop now",
    "clearance",
    "appliances",
    "computers",
    "cell phones",
    "tvs",
    "need help",
    "follow us",
    "your privacy",
    "view email in browser",
    "what you should know",
    "for shipping",
    "save $",
    "comp. value",
    "sku",
    "item",
    "store",
    "best buy",
    "hi ",
    "hello",
    "nick",
    "qty",
)
_SKIP_LINES = ("http://", "https://", "www.", "tel:", "mailto:")
_SEPARATOR_RE = re.compile(r"^[-=—\s]+$")


def _parse_items(text: str) -> list[PurchaseItem]:
    items: list[PurchaseItem] = []
    cur: PurchaseItem | None = None
    awaiting_qty = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _SEPARATOR_RE.fullmatch(line):
            continue
        low = line.lower()
        if low.startswith(_LABEL_LINES) or low.startswith(_SKIP_LINES):
            continue
        if re.fullmatch(r"qty:?", line, re.IGNORECASE):
            awaiting_qty = True
            continue
        if awaiting_qty and line.isdigit() and cur is not None:
            cur.qty = int(line)
            awaiting_qty = False
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
            if cur.price is None:
                cur.price = price
            continue
        if price is not None:
            continue  # totals amount with no open item
        # New title line (may carry a ' - <Platform>' suffix): close a priced
        # item first (block terminator in table layouts without Qty rows).
        if cur is not None and cur.price is not None:
            items.append(cur)
        title = line
        hint = None
        if " - " in title:
            maybe = title.rsplit(" - ", 1)[1].strip().lower()
            if maybe in PLATFORM_TOKENS:
                hint = maybe
        cur = PurchaseItem(title=title, platform_hint=hint)
    if cur is not None and cur.price is not None:
        items.append(cur)
    return items


def parse_bestbuy_receipt(
    body_text: str = "",
    body_html: str = "",
    message_id: str | None = None,
) -> Purchase | None:
    """Parse a Best Buy order confirmation. Prefer the HTML body."""
    source = body_html if body_html else body_text
    text = strip_html(source) if body_html else source
    if "thanks for your order" not in text.lower() and "thanks for shopping" not in text.lower():
        return None
    order_match = _ORDER_RE.search(text)
    if not order_match:
        return None
    date_match = _DATE_RE.search(text)

    # Items live between 'Product Details' and the summary (web-view layout).
    start = text.find("Product Details")
    if start == -1:
        start = 0
    end = text.find("Order Summary", start)
    region = text[start:end] if end > start else text[start:]
    items = _parse_items(region)
    if not items:
        return None
    total_m = re.search(r"\bTotal\b(?! paid)\s*\n?\s*(\$\d[\d,]*\.\d{2})", text, re.IGNORECASE)
    subtotal_m = re.search(r"Subtotal\s*\n?\s*(\$\d[\d,]*\.\d{2})", text, re.IGNORECASE)
    tax_m = re.search(r"(?:Sales\s*)?Tax\s*\n?\s*(\$\d[\d,]*\.\d{2})", text, re.IGNORECASE)
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
