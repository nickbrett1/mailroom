"""Retailer source registry + parser dispatch for physical purchases.

Each source knows how to find its emails in msgvault (senders + client-side
subject filter) and how to turn a stored raw receipt into normalized
Purchases. `parse_source` is the single dispatch point used by the
parsed_purchases_physical asset.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from mailroom.verticals.game_catalog.parsers.amazon import parse_amazon_receipt
from mailroom.verticals.game_catalog.parsers.bestbuy import (
    parse_bestbuy_receipt,
    parse_bestbuy_tracking,
)
from mailroom.verticals.game_catalog.parsers.cdkeys import parse_cdkeys_receipt
from mailroom.verticals.game_catalog.parsers.common import Purchase
from mailroom.verticals.game_catalog.parsers.ebay import parse_ebay_receipt
from mailroom.verticals.game_catalog.parsers.gameflip import parse_gameflip_receipt
from mailroom.verticals.game_catalog.parsers.gamefly import parse_gamefly_receipt
from mailroom.verticals.game_catalog.parsers.gamestop import parse_gamestop_receipt
from mailroom.verticals.game_catalog.parsers.larian import parse_larian_receipt
from mailroom.verticals.game_catalog.parsers.mercari import parse_mercari_receipt
from mailroom.verticals.game_catalog.parsers.shopify import parse_shopify_receipt
from mailroom.verticals.game_catalog.parsers.target import parse_target_receipt
from mailroom.verticals.game_catalog.parsers.walmart import parse_walmart_receipt
from mailroom.verticals.game_catalog.parsers.woot import parse_woot_receipt


@dataclass
class RetailerSource:
    name: str
    senders: list[str]
    parser: Callable[..., Purchase | list[Purchase] | None]
    subject_contains: list[str] = field(default_factory=list)  # any (case-insensitive) match keeps the email
    body: str = "text"  # "text" | "recover" (Best Buy: webview HTML + tracking fallback)


RETAILER_SOURCES: list[RetailerSource] = [
    RetailerSource(
        name="gamestop",
        # Order confirmations arrive from notifications@info.gamestop.com
        # (subject "Thank you for your order!", verified msg 42957 = order
        # 1100000059461018, 2023-06-22). The legacy orders@em.gamestop.com
        # sender used "Thanks for your Gamestop.com order" — keep both.
        senders=["orders@em.gamestop.com", "notifications@info.gamestop.com"],
        parser=parse_gamestop_receipt,
        subject_contains=["Thank you for your order", "Thanks for your Gamestop.com order"],
    ),
    RetailerSource(
        name="amazon",
        senders=["auto-confirm@amazon.com", "shipment-tracking@amazon.com", "order-update@amazon.com"],
        parser=parse_amazon_receipt,
    ),
    RetailerSource(
        name="shopify",
        senders=[
            "store+9127444@t.shopifyemail.com",  # Limited Run Games
            "store+59724660849@t.shopifyemail.com",  # LRG SOHO
            "store+60936585381@t.shopifyemail.com",  # Atari
        ],
        parser=parse_shopify_receipt,
    ),
    RetailerSource(
        name="larian",
        senders=["merchstore@larian.com"],
        parser=parse_larian_receipt,
    ),
    RetailerSource(
        name="bestbuy",
        senders=["bestbuyinfo@emailinfo.bestbuy.com"],
        parser=parse_bestbuy_receipt,
        subject_contains=["Thanks for your order.", "We have your tracking number."],
        body="recover",
    ),
    RetailerSource(
        name="gamefly",
        senders=["support@gamefly.com"],
        parser=parse_gamefly_receipt,
        subject_contains=["Your GameFly Order Has Been Confirmed"],
    ),
    RetailerSource(
        name="woot",
        senders=["no-reply@woot.com"],
        parser=parse_woot_receipt,
        subject_contains=["Woot says thanks for your order"],
    ),
    RetailerSource(
        name="target",
        senders=["orders@oe.target.com", "orders@oe1.target.com", "orders@target.com"],
        parser=parse_target_receipt,
        subject_contains=["Thanks for shopping with us"],
    ),
    RetailerSource(
        name="walmart",
        # Receipt facts come from order confirmations + arrivals (the item +
        # price are inline). Shipped/tracking emails carry no price -> ignored.
        senders=["help@walmart.com"],
        parser=parse_walmart_receipt,
        subject_contains=["thanks for your order", "items from your order", "your package arrived"],
    ),
    RetailerSource(
        name="mercari",
        senders=["no-reply@alerts.us.mercari.com"],
        parser=parse_mercari_receipt,
        subject_contains=["You purchased:", "You've made a purchase:"],
    ),
    RetailerSource(
        name="ebay",
        senders=["ebay@ebay.com"],
        parser=parse_ebay_receipt,
        subject_contains=["your order is confirmed"],
    ),
    RetailerSource(
        name="cdkeys",
        senders=["support@cdkeys.com"],
        parser=parse_cdkeys_receipt,
        subject_contains=["Order"],
    ),
    RetailerSource(
        name="gameflip",
        senders=["no-reply@gameflip.com"],
        parser=parse_gameflip_receipt,
        subject_contains=["purchase of"],
    ),
]

_SOURCES_BY_NAME = {s.name: s for s in RETAILER_SOURCES}


def source_by_name(name: str) -> RetailerSource | None:
    return _SOURCES_BY_NAME.get(name)


def parse_source(
    name: str,
    *,
    body: str,
    body_html: str = "",
    subject: str | None = None,
    message_id: str | None = None,
) -> list[Purchase]:
    """Dispatch a stored raw receipt to its source parser -> list[Purchase]."""
    if name == "amazon":
        return parse_amazon_receipt(body, message_id=message_id, subject=subject)
    if name == "cdkeys":
        p = parse_cdkeys_receipt(body, message_id=message_id)
        return [p] if p else []
    if name == "gameflip":
        p = parse_gameflip_receipt(body, message_id=message_id)
        return [p] if p else []
    if name == "larian":
        p = parse_larian_receipt(body, message_id=message_id)
        return [p] if p else []
    if name == "mercari":
        p = parse_mercari_receipt(body, message_id=message_id, subject=subject)
        return [p] if p else []
    if name == "bestbuy":
        p = parse_bestbuy_receipt(body_text=body, body_html=body_html, message_id=message_id)
        if p is None:
            p = parse_bestbuy_tracking(body, message_id=message_id)
        return [p] if p else []
    if name == "shopify":
        p = parse_shopify_receipt(body_text=body, body_html=body_html, message_id=message_id)
        return [p] if p else []
    source = source_by_name(name)
    if source is None:
        return []
    p = source.parser(body, message_id=message_id)
    return [p] if p else []
