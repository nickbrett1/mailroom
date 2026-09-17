"""Retailer source registry + parse_source dispatch tests."""

from __future__ import annotations

from mailroom.verticals.game_catalog.sources import (
    RETAILER_SOURCES,
    parse_source,
    source_by_name,
)

GAMESTOP = """Thank you for your order, Nick!

Order Number: 1100000043740236

Order Date: 1/16/2022

SHIP TO HOME

Shipping to 80 Riverside Blvd

No Man's Sky - PlayStation 4

Platform: PlayStation 4

Condition: Pre-Owned

QTY: 1

$8.99

ORDER SUMMARY

Subtotal

$8.99

Estimated Total

$8.99
"""

AMAZON_TWO_ORDERS = """Thanks for your order, Nick!

Order #
114-1970161-5765038

* Sonic Superstars - PlayStation 5
  Quantity: 1
  15.56 USD

Total
16.94 USD

Order #
111-4367032-4449826

* Degree Men Deodorant 4 Pack
  Quantity: 1
  9.99 USD

Total
9.99 USD
"""

BESTBUY_TRACKING = """We have your tracking number.

Order number: 
BBY01-807003276801

Tracking Number: 433207095584

God of War III Remastered Standard Edition - PlayStation 4

Get It By:

Thursday, December 19

Model #:3000925

SKU:5607062

Qty:1
"""


def test_registry_has_all_sources():
    names = {s.name for s in RETAILER_SOURCES}
    assert names == {
        "gamestop", "amazon", "shopify", "bestbuy", "gamefly", "woot",
        "target", "walmart", "mercari", "ebay", "cdkeys", "gameflip", "larian",
        "pcrichard",
        "popmarket",
        "squarespace",
    }
    # Every source has at least one sender and a parser.
    for s in RETAILER_SOURCES:
        assert s.senders, s.name
        assert callable(s.parser), s.name


def test_parse_source_gamestop():
    ps = parse_source("gamestop", body=GAMESTOP, message_id="g1")
    assert len(ps) == 1
    assert ps[0].source == "gamestop"
    assert ps[0].order_number == "1100000043740236"
    assert ps[0].items[0].title == "No Man's Sky - PlayStation 4"


def test_parse_source_amazon_returns_one_per_order():
    ps = parse_source("amazon", body=AMAZON_TWO_ORDERS, message_id="a1")
    assert len(ps) == 2
    assert [p.order_number for p in ps] == ["114-1970161-5765038", "111-4367032-4449826"]


def test_parse_source_bestbuy_tracking_fallback():
    ps = parse_source("bestbuy", body=BESTBUY_TRACKING, message_id="b1")
    assert len(ps) == 1
    assert ps[0].order_number == "BBY01-807003276801"
    assert "God of War III" in ps[0].items[0].title


def test_parse_source_mercari_uses_subject():
    body = "ID: m50029403165\nItem price\nBuyer protection fee\nTax\nCredits\n$19.00\n$0.68\n$1.75\n-$10.00\nTotal amount paid\n$11.43\nPayment Method\napplepay\n"
    ps = parse_source("mercari", body=body, subject="You purchased: Void Terrarium: Deluxe Edition For Playstation 5", message_id="m1")
    assert len(ps) == 1
    assert ps[0].order_number == "m50029403165"
    assert ps[0].items[0].title == "Void Terrarium: Deluxe Edition For Playstation 5"


def test_parse_source_unknown_returns_empty():
    assert parse_source("nope", body="x") == []
    assert source_by_name("nope") is None


def test_squarespace_source_covers_shared_storefront_sender():
    """Every Squarespace-hosted shop shares no-reply@squarespace.info; the
    subject filter keeps order confirmations (shipped emails have no prices)."""
    sq = source_by_name("squarespace")
    assert sq.senders == ["no-reply@squarespace.info"]
    assert any("order confirmed" in s.lower() for s in sq.subject_contains)


def test_parse_source_squarespace():
    body = (
        "Lost In Cult Order #59041 Confirmed\n"
        "Order Summary\n"
        "*Order #59041*\n"
        "Placed on May 21, 2025 at 2:25 PM GMT+1\n"
        "Thank Goodness You're Here! ( https://comms-sl-events.squarespace.info/?ref=AAA ) "
        "£59.99 TGYH-PS Platform: Playstation 5\n"
        "Qty: 1 £59.99 / Item\n"
        "Subtotal £59.99 Shipping £7.00 Sales Tax £0.00 Total £66.99\n"
    )
    ps = parse_source("squarespace", body=body, message_id="20805")
    assert len(ps) == 1
    assert ps[0].order_number == "59041"
    assert ps[0].items[0].title == "Thank Goodness You're Here!"
    """Order confirmations arrive from notifications@info.gamestop.com with
    subject 'Thank you for your order!' (msg 42957) — not the legacy
    orders@em.gamestop.com / 'Thanks for your Gamestop.com order' combo."""
    gs = source_by_name("gamestop")
    assert "notifications@info.gamestop.com" in gs.senders
    assert "orders@em.gamestop.com" in gs.senders
    assert any("Thank you for your order" in s for s in gs.subject_contains)
    assert any("Thanks for your Gamestop.com order" in s for s in gs.subject_contains)


# Real GameStop order confirmation (msgvault 42957, 2023-06-22, order
# 1100000059461018) — the four PlayStation games that were missed because the
# sender wasn't configured.
GAMESTOP_2023 = """Thank you for your order, Nicholas

Order Number: 1100000059461018

Order Date: 6/22/23

Order Total
$52.25

Total Savings $28.96

SHIP TO HOME

Shipping to 80 RIVERSIDE BLVD

13 Sentinels: Aegis Rim - PlayStation 4

QTY: 1

$14.99

Returnal - PlayStation 5

QTY: 1

$19.99

Deathloop - PlayStation 5

QTY: 1

$14.99

Ghostwire: Tokyo Standard Edition - PlayStation 5

QTY: 1

$18.99

ORDER SUMMARY

Subtotal

$68.96

Estimated Tax

$4.26

Estimated Total

$52.25
"""


def test_parse_source_gamestop_2023_confirmation():
    ps = parse_source("gamestop", body=GAMESTOP_2023, message_id="42957")
    assert len(ps) == 1
    assert ps[0].order_number == "1100000059461018"
    titles = [i.title for i in ps[0].items]
    assert titles == [
        "13 Sentinels: Aegis Rim - PlayStation 4",
        "Returnal - PlayStation 5",
        "Deathloop - PlayStation 5",
        "Ghostwire: Tokyo Standard Edition - PlayStation 5",
    ]
    assert ps[0].total == "$52.25"


def test_gamestop_source_watches_shipment_notices():
    """Shipment notices must be ingested: a bundle confirmation hides its games
    (msg 66500 itemizes the bundled Spider-Man: Miles Morales)."""
    gs = source_by_name("gamestop")
    assert any("has shipped" in s for s in gs.subject_contains)


# Real GameStop shipment notice (msgvault 66500, 2021-03-01, order
# 1100000027339767) — the console bundle was one line in the confirmation; the
# shipment itemizes the bundled game.
GAMESTOP_SHIPMENT = """Your package is on the way!
Ship to: 80 RIVERSIDE BLVD

Order Number:
1100000027339767

Order Date: 02/23/2021

View Order Details

Your Item(s)

Sony DualSense Wireless Controller

QTY: 1

$69.99

PlayStation 5

QTY: 1

$500.01

Marvel's Spider-Man: Miles Morales Ultimate Launch Edition

QTY: 1

$69.99

Gift Card, $20 (Web Only)

QTY: 1

$20.00

ORDER SUMMARY

Subtotal

$659.99

Shipping & Handling

FREE

Estimated Tax

$56.80

Estimated Total

$716.79
"""


def test_parse_source_gamestop_shipment_fallback():
    """The gamestop dispatch falls back to the shipment parser when the body is
    not an order confirmation, recovering the itemized bundle contents."""
    ps = parse_source("gamestop", body=GAMESTOP_SHIPMENT, message_id="66500")
    assert len(ps) == 1
    assert ps[0].order_number == "1100000027339767"
    titles = [i.title for i in ps[0].items]
    assert "Marvel's Spider-Man: Miles Morales Ultimate Launch Edition" in titles
    assert ps[0].total == "$716.79"
