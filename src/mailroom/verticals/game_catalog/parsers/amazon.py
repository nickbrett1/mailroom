"""Amazon order parser (text body).

Verified against the archive (msgvault, 2026-08-16): senders
auto-confirm@amazon.com (Ordered:), shipment-tracking@amazon.com (Shipped:),
order-update@amazon.com (Delivered:). All three share the same body shape:

    Thanks for your order, Nick!
    ...
    Order #
    114-1970161-5765038
    View or edit order
    https://www.amazon.com/...orderID=114-1970161-5765038...

    * Sonic Superstars - PlayStation 5
      Quantity: 1
      15.56 USD

    Total
    16.94 USD

One email can contain MULTIPLE order blocks (each with its own Order #, items
and Total) — so this parser returns a *list* of Purchases, one per order block.

Amazon sends ~3 emails per order (Ordered / Shipped / Delivered); each carries
the same (order #, item) facts, so the merge layer dedupes on
(order_number, item_key) — never double count. The 'Out for delivery' variant
may omit the price line; the parser tolerates that.
"""

from __future__ import annotations

import re

from mailroom.verticals.game_catalog.parsers.common import Purchase, PurchaseItem

_ORDER_RE = re.compile(r"Order #\s*(\d{3}-\d{7}-\d{7})", re.IGNORECASE)
_ITEM_RE = re.compile(r"(?m)^\* (.*)$")
_QTY_RE = re.compile(r"Quantity:\s*(\d+)", re.IGNORECASE)
_PRICE_RE = re.compile(r"^\s*([\d,]+\.\d{2})\s*USD\s*$", re.MULTILINE)
_TOTAL_RE = re.compile(r"(?:Grand\s+)?Total\s*:?\s*\n?\s*([\d,]+\.\d{2})\s*USD", re.IGNORECASE)
# Delivery-estimate template (order-update@amazon.com, subject "Delivery
# estimate update for your Amazon.com order #..."): items are NOT '*'-prefixed
# and carry no qty/price. Each item is an indented line directly above a
# "Sold by Amazon.com Services" line. Verified msg 65274 (Uncharted: Nathan
# Drake Collection), 39189 (Theatrhythm Final Bar Line), 64330 (Keurig).
_SOLD_BY_RE = re.compile(r"(?m)^\s*Sold by Amazon\.com Services[^\n]*$")
# 'Order details' / 'View your item' template: the item block sits ABOVE the
# 'Sold by: <seller>' line, which itself is ABOVE the 'Order #', so the
# order-block split (at 'Order #') never sees the item — the body only. The
# item title is the cleanest (shortest) line above 'Sold by:'. Any seller is
# matched ('Amazon.com', 'European Rarities', ...). Verified:
# 'Beyond A Steel Sky: Beyond A SteelBook Edition (PS5)' (order 113-7134038-7289042)
# and 'Evergate PS5 (PS5)' (order 111-5694915-7906620).
_ORDER_DETAILS_SOLD_BY_RE = re.compile(r"(?m)^\s*Sold by:\s*\S.*$", re.IGNORECASE)
# Item titles must not be order-block boilerplate lines.
_DELIVERY_ITEM_SKIP = ("order #", "placed on", "new estimated", "previous estimated", "sold by")


def _order_details_items(body: str) -> list[PurchaseItem]:
    """Extract item title(s) from the 'Order details / View your item'
    template, where the item sits above 'Sold by: Amazon.com' (above the
    'Order #', so the order-block split never sees it). Picks the shortest
    non-boilerplate line above 'Sold by:' — the clean title, even when the
    email renders it as a mangled duplicate ('.…' tail)."""
    items: list[PurchaseItem] = []
    for m in _ORDER_DETAILS_SOLD_BY_RE.finditer(body):
        header = body[: m.start()]
        lines = [ln.strip() for ln in header.splitlines() if ln.strip()]
        cands = [
            ln for ln in lines
            if not ln.lower().startswith(_DELIVERY_ITEM_SKIP)
            and ln.lower() not in ("order details", "order summary")
        ]
        if not cands:
            continue
        title = min(cands, key=len)
        if not title:
            continue
        items.append(PurchaseItem(title=title, price=None, qty=1))
    return items


def _delivery_estimate_items(block: str) -> list[PurchaseItem]:
    """Extract item titles from the Delivery-estimate template.

    Each "Sold by Amazon.com Services" line is preceded by the item title (the
    last non-blank line above it, e.g. 'Uncharted: Nathan Drake Collection
    Hits - PlayStation 4'). The classifier gate filters non-game items later.
    """
    items: list[PurchaseItem] = []
    for m in _SOLD_BY_RE.finditer(block):
        header = block[: m.start()]
        lines = [ln.strip() for ln in header.splitlines() if ln.strip()]
        if not lines:
            continue
        title = lines[-1]
        if not title or title.lower().startswith(_DELIVERY_ITEM_SKIP):
            continue
        items.append(PurchaseItem(title=title, price=None, qty=1))
    return items


def _parse_order_block(block: str, message_id: str | None) -> Purchase | None:
    order_match = _ORDER_RE.search(block)
    if not order_match:
        return None
    items: list[PurchaseItem] = []
    for m in _ITEM_RE.finditer(block):
        title = m.group(1).strip()
        if not title:
            continue
        tail = block[m.end() :]
        qty = 1
        price: str | None = None
        qty_match = _QTY_RE.search(tail)
        if qty_match:
            qty = int(qty_match.group(1))
            price_match = _PRICE_RE.search(tail[qty_match.end() :])
            if price_match:
                price = f"${price_match.group(1)}"
        items.append(PurchaseItem(title=title, price=price, qty=qty))
    # Delivery-estimate template: no '*'-prefixed lines, so fall back to the
    # item(s) above each "Sold by Amazon.com Services" line. Only used when
    # the standard parse found nothing, so Ordered/Shipped/Delivered bodies
    # are unaffected.
    if not items:
        items = _delivery_estimate_items(block)
    if not items:
        return None
    total_match = _TOTAL_RE.search(block)
    return Purchase(
        order_number=order_match.group(1),
        purchased_at=None,  # not present in the email body; asset uses sent_at
        items=items,
        total=f"${total_match.group(1)}" if total_match else None,
        message_id=message_id,
        source="amazon",
    )


# The item must stop at a closing quote — a lazy .+? would swallow the whole
# tail of a cancellation subject ("...Master Plunger MPS4 Sink...\" has been
# canceled" -> a bogus 'game' titled with the cancel text).
_SUBJECT_ITEM_RE = re.compile(r"order of [\"']?(?P<item>[^\"']+?)[\"']?\.?\s*$", re.IGNORECASE)
# Shipped/delivered subjects end with " has shipped!" / " is out for delivery!" —
# only the CONFIRMATION ("Your Amazon.com order of ...") carries the item fact.
_SHIPPED_SUBJECT_RE = re.compile(r"shipped|out for delivery|delivered|arriving|will arrive", re.IGNORECASE)
# Order-update cancellations/refunds are NOT purchase facts: the item never
# (or no longer) becomes owned. Amazon subjects: "Your Amazon.com order has
# been canceled", "Order canceled", "We canceled your order...".
_CANCEL_SUBJECT_RE = re.compile(r"\bcancel", re.IGNORECASE)


def parse_amazon_receipt(body: str, message_id: str | None = None, subject: str | None = None) -> list[Purchase]:
    """Parse an Amazon order email into one Purchase per order block."""
    # A cancellation email must never become a catalog fact — even though its
    # body can carry the Order # and item lines (which the block parser would
    # otherwise turn into an owned 'game').
    if subject and _CANCEL_SUBJECT_RE.search(subject):
        return []
    # Split the body at each 'Order #' occurrence; each block is one order.
    starts = [m.start() for m in _ORDER_RE.finditer(body)]
    if not starts:
        return []
    purchases: list[Purchase] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        purchase = _parse_order_block(body[start:end], message_id)
        if purchase:
            purchases.append(purchase)
    # 'Order details / View your item' template: the item block is above the
    # 'Order #', so the order-block split above found nothing. Parse it from
    # the body's 'Sold by: Amazon.com' section (independent of the subject).
    if not purchases:
        items = _order_details_items(body)
        if items:
            purchases.append(
                Purchase(
                    source="amazon",
                    order_number=_ORDER_RE.search(body).group(1) if _ORDER_RE.search(body) else None,
                    purchased_at=None,  # email received date (acquisition date)
                    items=items,
                    subtotal=None,
                    tax=None,
                    total=None,
                    message_id=str(message_id) if message_id else None,
                )
            )
    # Newer Amazon template: the confirmation body has the order # + total but
    # NO item lines — the item lives only in the subject:
    # "Your Amazon.com order of \"Resident Evil 4 - PS5\"." (verified msg 32705).
    if not purchases and subject and not _SHIPPED_SUBJECT_RE.search(subject):
        m = _SUBJECT_ITEM_RE.search(subject)
        if m and m.group("item") and "order of" in subject.lower():
            purchases.append(
                Purchase(
                    source="amazon",
                    order_number=_ORDER_RE.search(body).group(1) if _ORDER_RE.search(body) else None,
                    purchased_at=None,  # email received date (acquisition date)
                    items=[PurchaseItem(title=m.group("item").strip(), price=None, qty=1)],
                    subtotal=None,
                    tax=None,
                    total=None,
                    message_id=str(message_id) if message_id else None,
                )
            )
    return purchases
