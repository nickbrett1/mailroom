"""Gameflip order parser — marketplace key/code purchases.

Verified (msgvault 2026-08-16): sender `no-reply@gameflip.com`. Purchase
emails ("Your purchase of \"<item>\" is complete!" /
"Thank you for your purchase of \"<item>\" on Gameflip"):

    Thank you for your purchase of "Marvel's Spider-Man 2" on Gameflip ...
    Order: Marvel's Spider-Man 2
    Order ID: 798cc5d1-de47-44fa-b6aa-1d65a628c063
    Seller: discountgamesdirect
    Price: $40.00
    Shipping Fee: $0.00

Multiple status emails per purchase (complete / key delivered / thank you)
dedupe on the Order ID. Seller-authored titles may lack a platform token →
classifier + review bucket. These are digital key purchases — format=digital
with retailer=gameflip at the merge layer.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import Purchase, PurchaseItem

_ORDER_ID_RE = re.compile(r"Order\s*ID:\s*([0-9a-f-]{8,})", re.IGNORECASE)
# Some purchase emails carry the UUID only in the order URL.
_URL_ID_RE = re.compile(r"exchange_buyer/([0-9a-f-]{8,})", re.IGNORECASE)
_ORDER_TITLE_RE = re.compile(r"^Order:\s*(.+)$", re.MULTILINE)
_PRICE_RE = re.compile(r"Price:\s*(\$\d[\d,]*\.\d{2})", re.IGNORECASE)


def parse_gameflip_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a Gameflip purchase email, or None if not one."""
    low = body.lower()
    if "gameflip" not in low and "gameflip.com" not in low:
        return None
    order_id = _ORDER_ID_RE.search(body) or _URL_ID_RE.search(body)
    title_match = _ORDER_TITLE_RE.search(body)
    if not order_id or not title_match:
        return None
    title = title_match.group(1).strip()
    price_match = _PRICE_RE.search(body)
    return Purchase(
        order_number=order_id.group(1),
        purchased_at=None,
        items=[PurchaseItem(title=title, price=price_match.group(1) if price_match else None)],
        total=price_match.group(1) if price_match else None,
        message_id=message_id,
        source="gameflip",
    )
