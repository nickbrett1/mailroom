"""eBay order-confirmation parser (text body).

Verified (msgvault 2026-08-16): sender `ebay@ebay.com`; ONLY "Nick, your
order is confirmed" emails create facts (subject carries the same title).
Body:

    Thanks for making another purchase.
    ...
    ------------------
    Your order details
    ------------------
    We'll let you know when your order has shipped.

    SEALED Wildermyth for Sony PlayStation 5 (PS5) w/ Monster Compendium
    --------------------------------------------------------------------
    ( url )
    Price:
    *$9.99*
    Item ID:
    376627255605
    Order number:
    12-14766-51548
    Seller:
    glenn_87 ( url )

Status emails ("Order update: …", "OUT FOR DELIVERY: …", "ORDER DELIVERED")
embed the title in the subject only and return None. Titles are
seller-authored -> platform-keyword classifier + review bucket.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import Purchase, PurchaseItem

_ORDER_RE = re.compile(r"Order number:\s*\n?\s*([\d-]+)", re.IGNORECASE)
_ITEM_ID_RE = re.compile(r"Item ID:\s*\n?\s*(\d+)", re.IGNORECASE)
_PRICE_RE = re.compile(r"\*(\$\d[\d,]*\.\d{2})\*")
_SEPARATOR_RE = re.compile(r"^[-=—\s]+$")


def _first_title(region: str) -> str | None:
    """Title = first meaningful line after the 'shipped' notice."""
    lines = [l.strip() for l in region.splitlines()]
    for i, line in enumerate(lines):
        if "we'll let you know when your order has shipped" in line.lower():
            for t in lines[i + 1 :]:
                if not t:
                    continue
                if t.startswith(("(", "http")):
                    continue
                if _SEPARATOR_RE.fullmatch(t):
                    continue
                # Some templates append the listing URL to the title line.
                return re.sub(r"\s*\(\s*https?://[^)]*\)\s*$", "", t).strip() or None
    return None


def parse_ebay_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse an eBay order-confirmation body, or None if not one."""
    low = body.lower()
    if "thanks for making another purchase" not in low or "your order details" not in low:
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None
    title = _first_title(body)
    if not title:
        return None

    item_id_match = _ITEM_ID_RE.search(body)
    prices = _PRICE_RE.findall(body)
    price = prices[0] if prices else None

    return Purchase(
        order_number=order_match.group(1),
        purchased_at=None,  # not in the body; the asset uses received_at
        items=[PurchaseItem(title=title, price=price)],
        total=price,
        message_id=message_id,
        source="ebay",
        # item ID rides along for dedupe at the merge layer.
        item_id=item_id_match.group(1) if item_id_match else None,
    )
