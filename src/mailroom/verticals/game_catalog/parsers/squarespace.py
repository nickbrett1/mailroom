"""Squarespace storefront order confirmations (text body).

Indie game shops hosted on Squarespace all email from the *same* sender,
`no-reply@squarespace.info` — the storefront only appears in the From display
name ("Lost In Cult", "2 Old 4 Gaming") — so this parser is deliberately
storefront-agnostic: it reads the generic "Order Summary" block every
Squarespace order confirmation renders.

Verified against the archive (2026-09-14):

    Lost In Cult #59041 (2025-05-21) — the missed purchase
      Thank Goodness You're Here! ( <link> ) £59.99 TGYH-PS Platform: Playstation 5
      Qty: 1 £59.99 / Item
      Subtotal £59.99 Shipping £7.00 Sales Tax £0.00 Total £66.99

    2 Old 4 Gaming #2662 (2026-04-25) — 10 unofficial instruction manuals
      <title> ( <link> ) £5.00 SQ6899346 Qty: 1 £5.00 / Item

Two item shapes appear: theme A keeps the title + price on one line and puts
the qty on the next ("Qty: 1 £59.99 / Item"); theme B keeps title + price + SKU
+ qty all on one line. Both are handled by the same line walk. Prices carry the
storefront's own currency (£ here, hence not the `$`-only `money()` helper) and
are preserved verbatim; the platform arrives as a "Platform: <name>" hint for
the classifier. Printed matter (the manual order) is never catalogued — the
classifier routes non-game merch to review.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import Purchase, PurchaseItem

# "Order #59041" (first occurrence is the subject line echoed at the top).
_ORDER_RE = re.compile(r"Order\s*#\s*([A-Za-z0-9-]+)", re.IGNORECASE)
# "Placed on May 21, 2025 at 2:25 PM GMT+1".
_DATE_RE = re.compile(r"Placed on\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", re.IGNORECASE)
# Squarespace wraps every link in "( <url> )" — strip them before reading the
# line, they'd otherwise land inside the product title.
_LINK_RE = re.compile(r"\(\s*https?://\S+\s*\)")
# Storefront currency, not just '$' (Lost In Cult bills in £).
_AMOUNT = r"(?:£|\$|€)\s?\d[\d,]*(?:\.\d{2})?"
_AMOUNT_RE = re.compile(_AMOUNT)
_QTY_RE = re.compile(r"Qty:\s*(\d+)", re.IGNORECASE)
_PLATFORM_RE = re.compile(r"Platform:\s*(.+?)\s*$", re.IGNORECASE)
# "Subtotal £59.99 Shipping £7.00 Sales Tax £0.00 Total £66.99".
_TOTALS_LINE_RE = re.compile(r"^Subtotal\b", re.IGNORECASE)
_SUBTOTAL_RE = re.compile(r"Subtotal\s+(" + _AMOUNT + r")", re.IGNORECASE)
_TAX_RE = re.compile(r"(?:Sales\s+)?Tax\s+(" + _AMOUNT + r")", re.IGNORECASE)
_TOTAL_RE = re.compile(r"\bTotal\s+(" + _AMOUNT + r")", re.IGNORECASE)
# "Placed on May 21, 2025 at 2:25 PM GMT+1" -> ("May", "21", "2025").
_DATE_PARTS_RE = re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date(text: str) -> str | None:
    """"May 21, 2025" -> "5/21/2025" (the house m/d/yyyy receipt format)."""
    m = _DATE_PARTS_RE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m.group(1)[:3].lower())
    if not month:
        return None
    return f"{month}/{int(m.group(2))}/{m.group(3)}"


def _order_summary_block(lines: list[str]) -> list[str]:
    """The item lines of the "Order Summary" section (totals line excluded)."""
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower() == "order summary"),
        None,
    )
    if start is None:
        return []
    block: list[str] = []
    for ln in lines[start + 1 :]:
        if _TOTALS_LINE_RE.match(ln):
            break
        block.append(ln)
    return block


def parse_squarespace_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a Squarespace storefront order confirmation, or None if not one.

    Gate: an order number plus a "Placed on <date>" line plus at least one
    priced item in the Order Summary block. Shipping/status emails carry no
    prices, so they yield no facts (return None).
    """
    order_match = _ORDER_RE.search(body)
    if not order_match or "Placed on" not in body:
        return None

    lines = [
        re.sub(r"\s+", " ", _LINK_RE.sub(" ", ln)).strip() for ln in body.splitlines()
    ]
    block = _order_summary_block(lines)
    if not block:
        return None

    items: list[PurchaseItem] = []
    pending_title: str | None = None
    pending_price: str | None = None
    pending_platform: str | None = None
    for ln in block:
        if not ln:
            continue
        amount = _AMOUNT_RE.search(ln)
        title_part = ln[: amount.start()].strip() if amount else ""
        qty_match = _QTY_RE.search(ln)
        platform_match = _PLATFORM_RE.search(ln)
        platform = platform_match.group(1).strip() if platform_match else None

        if qty_match:
            # The qty closes an item: theme B carries its own title, theme A
            # ("Qty: 1 £59.99 / Item") continues the preceding title line.
            if re.fullmatch(r"Qty:\s*\d+", title_part, re.IGNORECASE):
                title_part = ""
            title = title_part or pending_title
            price = amount.group(0) if amount else pending_price
            if title and price:
                items.append(
                    PurchaseItem(
                        title=title,
                        price=price,
                        qty=int(qty_match.group(1)),
                        platform_hint=platform or pending_platform,
                    )
                )
            pending_title = pending_price = pending_platform = None
            continue

        if amount and title_part:
            # Title + price line. In theme B the qty was on this same line (and
            # is handled above); here it awaits the next line.
            pending_title = title_part
            pending_price = amount.group(0)
            pending_platform = platform

    if pending_title and pending_price:
        items.append(
            PurchaseItem(
                title=pending_title, price=pending_price, platform_hint=pending_platform
            )
        )
    if not items:
        return None

    totals = next((ln for ln in lines if _TOTALS_LINE_RE.match(ln)), "")
    subtotal = _SUBTOTAL_RE.search(totals)
    tax = _TAX_RE.search(totals)
    total = _TOTAL_RE.search(totals)
    date_match = _DATE_RE.search(body)
    return Purchase(
        order_number=order_match.group(1),
        purchased_at=_parse_date(date_match.group(1)) if date_match else None,
        items=items,
        subtotal=subtotal.group(1) if subtotal else None,
        tax=tax.group(1) if tax else None,
        total=total.group(1) if total else None,
        message_id=message_id,
        source="squarespace",
    )
