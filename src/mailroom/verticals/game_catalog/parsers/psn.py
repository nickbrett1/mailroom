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

from mailroom.verticals.game_catalog.parsers.common import Purchase, PurchaseItem

_TEMPLATE_A = "A receipt of your purchase is below"
# Post-2022 sender (reply@txn-email.playstation.com) drops the ™: "Your
# PlayStation Store transaction was successful". Accept both.
_TEMPLATE_B_RE = re.compile(r"Your PlayStation(?:™)? ?Store transaction was successful", re.IGNORECASE)
# A parenthetical indicating a catalog game: "(Game)", "(Downloadable Game)",
# "(Full Game)", PS1 Classics ("(PSOne Classic)", "(PS1 Emulation)"),
# platform parens ("(PS3™/PSP®/PS Vita)", "(PS4 & PS5)") and download-size
# parens ("(117 MB)", "(785 MB Required)"). Add-Ons / wallet / subscriptions
# are skipped.
_GAME_PAREN_RE = re.compile(r"\([^)]*(?:game|ps1|psone|ps2|ps3|psp|vita|ps4|ps5|mb|gb)[^)]*\)", re.IGNORECASE)

_PRICE_RE = re.compile(r"\$\d[\d,]*(?:\.\d{2})?")
_ORDER_RE = re.compile(r"Order Number:\s*([\w-]+)", re.IGNORECASE)
# Tolerant: 'Date Purchased: 12/14/2022' or 2012-era table layout
# ('Date Purchased\nTotal\n12/25/2012 @ 10:07 AM').
_DATE_RE = re.compile(r"Date\s*Purchased[^0-9]{0,40}?(\d{1,2}/\d{1,2}/\d{4})")
_SUBTOTAL_RE = re.compile(r"Subtotal:\s*\$([\d,.]+)")
_TAX_RE = re.compile(r"Tax:\s*\$([\d,.]+)")
_TOTAL_RE = re.compile(r"\bTotal:\s*\$([\d,.]+)", re.IGNORECASE)


def _strip_price(text: str) -> str:
    """Remove trailing price from an item title."""
    return re.sub(r"\s*\$[\d,.]*\s*$", "", text).strip()


# Template B / fallback: 'Title (Game/platform parens) $price' pairs, possibly
# on one line after the 'Details Price' header (also used for early Template A
# receipts that list bare items without asterisks, e.g. PS1 Classics).
_PAIR_RE = re.compile(
    r"(?P<title>[^$\n]+?\([^)]*(?:game|ps1|psone|ps2|ps3|psp|vita|ps4|ps5|mb|gb)[^)]*\)(?:\s*\([^)]*\))*)"
    r"\s*(?P<price>\$\d[\d,]*\.\d{2})",
    re.IGNORECASE,
)


def _extract_items(section: str, template: str) -> list[PurchaseItem]:
    items: list[PurchaseItem] = []
    if template == "A":
        titles = re.findall(r"\*([^*]+)\*", section)
        prices = _PRICE_RE.findall(section)
        for i, title in enumerate(titles):
            title = title.strip().strip("*")
            if not _GAME_PAREN_RE.search(title):
                # wallet top-ups / subscriptions / add-ons — not catalog games
                continue
            price = prices[i] if i < len(prices) else None
            items.append(PurchaseItem(title=title.strip(), price=price))
        if items:
            return items
        # Early Template A receipts (2012-era) list bare 'Title (…parens) $price'
        # without asterisks — fall through to the pair regex.
    section = re.sub(r"^Details\s*Price\s*", "", section, flags=re.IGNORECASE)
    for m in _PAIR_RE.finditer(section):
        title = _strip_price(m.group("title")).strip()
        items.append(PurchaseItem(title=title, price=m.group("price")))
    return items


def parse_psn_receipt(body: str, message_id: str | None = None) -> Purchase | None:
    """Parse a PSN receipt text body into a Purchase, or None if unrecognized."""
    if _TEMPLATE_A in body:
        template = "A"
    elif _TEMPLATE_B_RE.search(body):
        template = "B"
    else:
        return None

    order_match = _ORDER_RE.search(body)
    if not order_match:
        return None
    order_number = order_match.group(1)
    date_match = _DATE_RE.search(body)

    # Items live between 'Details' and 'Subtotal:' (or 'Total:' in early
    # 2012-era receipts that lack a Subtotal line).
    start = body.find("Details")
    end = body.find("Subtotal:", start)
    if end == -1:
        end = body.find("Total:", start)
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
        source="psn_receipt",
    )


def normalize_title(title: str) -> str:
    """Best-effort title normalization for dedupe (editions/regions)."""
    t = title.strip()
    t = re.sub(r"\(Game\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"PlayStation®?\s*[45]", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[–—]", "-", t)  # unify en/em dashes so titles match across sources
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()
