"""Mercari purchase parser (text body).

Verified (msgvault 2026-08-16): sender `no-reply@alerts.us.mercari.com`.
ONLY purchase emails ("You purchased: <item>" / "You've made a purchase:
<item>") create facts; shipped / in-transit / delivered / rate-reminder
emails have no price breakdown and return None.

The purchase email carries NO order number — the item ID keys the purchase:
    ID: m50029403165
    Item price                 $19.00
    Buyer protection fee       $0.68
    Tax                        $1.75
    Credits                    -$10.00
    Total amount paid          $11.43

The item title lives in raw HTML in the text body (<strong>…</strong>);
the subject ("You purchased: <title>") is the fallback. Titles are
seller-authored ("For Playstation 5") -> platform-keyword classifier +
review bucket.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import Purchase, PurchaseItem

_ID_RE = re.compile(r"ID:\s*(m\d+)")
_TITLE_HTML_RE = re.compile(r"<strong[^>]*>(.*?)</strong>", re.IGNORECASE | re.DOTALL)
_SUBJECT_MARKERS = ("You purchased:", "You've made a purchase:")
_AMOUNT_RE = re.compile(r"-?\$\d[\d,]*\.\d{2}")


def _title_from_subject(subject: str | None) -> str | None:
    if not subject:
        return None
    for marker in _SUBJECT_MARKERS:
        if marker.lower() in subject.lower():
            title = subject.split(marker, 1)[1].strip()
            return title or None
    return None


def parse_mercari_receipt(
    body: str,
    message_id: str | None = None,
    subject: str | None = None,
) -> Purchase | None:
    """Parse a Mercari purchase email, or None if not one."""
    low = body.lower()
    if "item price" not in low or "total amount paid" not in low:
        return None
    id_match = _ID_RE.search(body)
    if not id_match:
        return None

    title: str | None = None
    html_match = _TITLE_HTML_RE.search(body)
    if html_match:
        title = html_match.group(1).strip()
    if not title:
        title = _title_from_subject(subject)
    if not title:
        return None

    # Price breakdown: amounts between 'Item price' and 'Payment Method'.
    start = body.find("Item price")
    end = body.find("Payment Method", start)
    seg = body[start:end] if end != -1 else body[start:]
    amounts = _AMOUNT_RE.findall(seg)
    price = amounts[0] if amounts else None
    tax = amounts[2] if len(amounts) > 2 else None

    total = None
    ti = body.find("Total amount paid")
    if ti != -1:
        total_m = re.search(r"Total amount paid[^$]*(-?\$\d[\d,]*\.\d{2})", body[ti:], re.IGNORECASE)
        total = total_m.group(1) if total_m else None

    return Purchase(
        order_number=id_match.group(1),  # item ID keys the purchase (no order number)
        purchased_at=None,  # not in the body; the asset uses received_at
        items=[PurchaseItem(title=title, price=price)],
        subtotal=price,
        tax=tax,
        total=total,
        message_id=message_id,
        source="mercari",
    )
