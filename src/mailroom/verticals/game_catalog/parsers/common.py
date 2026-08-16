"""Shared purchase dataclasses and small helpers for receipt parsers.

Every per-source parser returns a `Purchase` (or None when the email carries no
new facts), so the assets can treat digital and physical sources uniformly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PurchaseItem:
    title: str
    price: str | None = None
    platform_hint: str | None = None
    qty: int = 1
    condition: str | None = None


@dataclass
class Purchase:
    order_number: str
    purchased_at: str | None
    items: list[PurchaseItem] = field(default_factory=list)
    subtotal: str | None = None
    tax: str | None = None
    total: str | None = None
    message_id: str | None = None
    source: str = "receipt"
    # Marketplace purchases key on the item ID (Mercari 'm…', eBay listing id).
    item_id: str | None = None

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
            "item_id": self.item_id,
        }


_PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")


def money(text: str) -> str | None:
    """Return the first normalized $ amount in `text`, or None."""
    m = _PRICE_RE.search(text)
    return m.group(0).replace(" ", "") if m else None


# Platform tokens recognized on standalone variant lines / inline suffixes.
PLATFORM_TOKENS = {
    "ps4",
    "ps5",
    "psvita",
    "vita",
    "ps3",
    "playstation 4",
    "playstation 5",
    "playstation",
    "switch",
    "nintendo switch",
    "xbox",
    "xbox series x",
    "xbox series s",
    "pc",
    "steam",
    "atari 2600",
}


def is_platform_token(line: str) -> str | None:
    """Return the lowercased token if `line` is exactly a platform token."""
    token = line.strip().lower()
    return token if token in PLATFORM_TOKENS else None


def strip_html(html: str) -> str:
    """Convert HTML to text lines (best effort): drop scripts/styles/tags,
    unescape entities, keep line breaks."""
    import html as _html

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(
        r"<br\s*/?>|<p[^>]*>|</p>|<tr[^>]*>|</tr>|<td[^>]*>|</td>|<th[^>]*>|</th>|<div[^>]*>|</div>|<li[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text)


def clean_whitespace(text: str) -> str:
    """Strip HTML-entity artifacts (&zwnj;, &nbsp;, zero-width space) and
    collapse horizontal whitespace. Newlines are preserved — parsers split
    the body line-by-line."""
    text = text.replace("&zwnj;", "").replace("&nbsp;", " ").replace("\u200b", "")
    return re.sub(r"[ \t]+", " ", text)
