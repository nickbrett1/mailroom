"""PSN receipt parser (both templates, from the text body).

Template A (pre-2022): 'Transaction Receipt ... A receipt of your purchase is below'
  - items wrapped in asterisks: *Wreckfest PlayStation®5 Version (Game)* ... $0.00
Template B (2022+): 'Your PlayStation™Store transaction was successful. Thanks!'
  - items as 'Title (Game) $price' lines under 'Details'

Both expose: Order Number, Online ID (nbrett3), Date Purchased, line items,
Subtotal/Tax/Total. Wallet top-ups / non-(Game) items are skipped but logged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PurchaseItem:
    title: str
    price: Optional[str] = None
    platform_hint: Optional[str] = None


@dataclass
class Purchase:
    order_number: str
    purchased_at: Optional[str]
    items: list[PurchaseItem] = field(default_factory=list)
    subtotal: Optional[str] = None
    tax: Optional[str] = None
    total: Optional[str] = None
    message_id: Optional[str] = None
    source: str = "psn_receipt"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "order_number": self.order_number,
            "purchased_at": self.purchased_at,
            "items": [item.__dict__ for item in self.items],
            "subtotal": self.subtotal,
            "tax": self.tax,
            "total": self.total,
            "message_id": self.message_id,
        }


_TEMPLATE_A = "A receipt of your purchase is below"
_TEMPLATE_B = "Your PlayStation™Store transaction was successful"


_PRICE_RE = re.compile(r"\$\d[\d,]*(?:\.\d{2})?")
_ORDER_RE = re.compile(r"Order Number:\s*([\w-]+)", re.IGNORECASE)
_DATE_RE = re.compile(r"Date Purchased:\s*([\d/]+)")
_SUBTOTAL_RE = re.compile(r"Subtotal:\s*\$([\d,.]+)")
_TAX_RE = re.compile(r"Tax:\s*\$([\d,.]+)")
_TOTAL_RE = re.compile(r"Total:\s*\$([\d,.]+)", re.IGNORECASE)


def _strip_price(text: str) -> str:
    """Remove trailing price from an item title."""
    return re.sub(r"\s*\$[\d,.]*\s*$", "", text).strip()


def _extract_items(section: str, template: str) -> list[PurchaseItem]:
    items: list[PurchaseItem] = []
    if template == "A":
        titles = re.findall(r"\*([^*]+)\*", section)
        prices = _PRICE_RE.findall(section)
        for i, title in enumerate(titles):
            title = title.strip().strip("*")
            if "(Game)" not in title:
                # wallet top-ups / subscriptions — not catalog games
                continue
            price = prices[i] if i < len(prices) else None
            items.append(PurchaseItem(title=title.strip(), price=price))
    else:
        # Template B: items are 'Title (Game) $price' pairs, possibly on one
        # line after 'Details Price'. Match non-greedily up to each price.
        pair_re = re.compile(
            r"(?P<title>[^$\n]+?\(Game\))\s*(?P<price>\$\d[\d,]*\.\d{2})"
        )
        for m in pair_re.finditer(section):
            title = _strip_price(m.group("title")).strip()
            items.append(PurchaseItem(title=title, price=m.group("price")))
    return items


def parse_psn_receipt(body: str, message_id: Optional[str] = None) -> Optional[Purchase]:
    """Parse a PSN receipt text body into a Purchase, or None if unrecognized."""
    if _TEMPLATE_A in body:
        template = "A"
    elif _TEMPLATE_B in body:
        template = "B"
    else:
        return None

    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None
    order_number = order_match.group(1)
    date_match = _DATE_RE.search(body)

    # Items live between 'Details' and 'Subtotal:'.
    start = body.find("Details")
    end = body.find("Subtotal:", start)
    if start == -1 or end == -1:
        return None
    section = body[start:end]

    items = _extract_items(section, template)
    if not items:
        return None

    return Purchase(
        order_number=order_number,
        purchased_at=date_match.group(1) if date_match else None,
        items=items,
        subtotal=_SUBTOTAL_RE.search(body).group(1) if _SUBTOTAL_RE.search(body) else None,
        tax=_TAX_RE.search(body).group(1) if _TAX_RE.search(body) else None,
        total=_TOTAL_RE.search(body).group(1) if _TOTAL_RE.search(body) else None,
        message_id=message_id,
    )


def normalize_title(title: str) -> str:
    """Best-effort title normalization for dedupe (editions/regions)."""
    t = title.strip()
    t = re.sub(r"\(Game\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"PlayStation®?\s*[45]", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()
