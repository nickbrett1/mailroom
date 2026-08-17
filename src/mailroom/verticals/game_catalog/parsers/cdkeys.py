"""CDKeys order parser — PSN code purchases (digital keys).

Verified (msgvault 2026-08-16): sender `support@cdkeys.com`. Two templates:

  2023 "Your CDKeys.com Order confirmation <n>":
    *YOUR ORDER IS COMPLETE*
    Order Number: 0151509331
    Subtotal $47.99 Total *$47.99*
    God of War Ragnarök PS5 (US)
    1x $47.99

  2025 "Order #0127233883 - From CDKeys.com":
    Thank you for your purchase!
    Order #0127233883
    Subtotal $40.19 Order Total *$40.19*
    ASTRO BOT PS5 (US)
    $40.19
    x1

The item title carries the platform token ("PS5"/"PS4"). These are DIGITAL
key purchases — format=digital with retailer=cdkeys at the merge layer.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import Purchase, PurchaseItem, money

_ORDER_RE = re.compile(r"Order\s*(?:Number|#)?\s*:?\s*#?(\d+)", re.IGNORECASE)
_ITEM_RE = re.compile(
    r"([A-Za-z0-9][^\n]*?\b(?:PS4|PS5|PS Vita|PS3|PSVita)\b[^\n]*)",
    re.IGNORECASE,
)
_PLATFORM_RE = re.compile(r"\b(ps4|ps5|ps vita|ps3|psvita)\b", re.IGNORECASE)
_QTY_RE = re.compile(r"x?\s*(\d+)\s*$", re.IGNORECASE)
_LABELS = (
    "subtotal",
    "total",
    "order total",
    "click here",
    "get your key",
    "have any questions",
    "browse our latest",
    "pay less",
    "hi ",
    "hello",
    "order number",
    "customer name",
    "billing address",
    "payment method",
    "payer email",
    "my profile",
    "my orders",
    "cdkoins",
    "thank you for your purchase",
    "your order is complete",
    "please click the download",
    "if you have any queries",
    "1x",
    "x1",
)


def parse_cdkeys_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a CDKeys order email, or None if not one."""
    low = body.lower()
    if "cdkeys" not in low and "cd keys" not in low:
        return None
    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None

    item_match = _ITEM_RE.search(body)
    if not item_match:
        return None
    title = item_match.group(1).strip()
    platform_match = _PLATFORM_RE.search(title)
    hint = platform_match.group(1).lower() if platform_match else None

    # price: first $ amount on the item line / nearby ('1x $47.99' style lines)
    price = money(title)
    if price is None:
        seg = body[item_match.end() : item_match.end() + 400]
        price = money(seg)

    qty = 1
    # 'x1'/'1x' lines are separate; qty lives on its own line after the price.
    q = re.search(r"(?:^|\n)\s*x?\s*(\d+)\s*\n", body[item_match.end() : item_match.end() + 120])
    if q:
        qty = int(q.group(1))

    subtotal_m = re.search(r"Subtotal:?\s*\$([\d,.]+)", body, re.IGNORECASE)
    total_m = re.search(r"\bTotal:?\s*\$([\d,.]+)", body, re.IGNORECASE)
    return Purchase(
        order_number=order_match.group(1),
        purchased_at=None,
        items=[PurchaseItem(title=title, platform_hint=hint, price=price, qty=qty)],
        subtotal=f"${subtotal_m.group(1)}" if subtotal_m else None,
        total=f"${total_m.group(1)}" if total_m else None,
        message_id=message_id,
        source="cdkeys",
    )
