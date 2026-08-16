from mailroom.verticals.game_catalog.parsers.psn import (
    normalize_title,
    parse_psn_receipt,
)


TEMPLATE_A = """
Transaction Receipt

Dear Nick,

Thank you for your PlayStation®Store purchase.

A receipt of your purchase is below. Be sure to keep it in a safe place for future reference.

Order Number:  264460326864

Name: Nick Brett

Online ID: nbrett3

Date Purchased: 05/08/2021

Details

Price

*Wreckfest PlayStation®5 Version (Game)*

$0.00

Subtotal: $0.00

Tax: $0.00

Total:  $0.00
"""

TEMPLATE_B = """
Your PlayStation™Store transaction was successful. Thanks!

You will find a copy of your transaction details below.

Order Number: 384720390939

Name: Nick Brett
Online ID: nbrett3
Date Purchased: 12/14/2022

Details Price The Witcher 3: Wild Hunt – Complete Edition (Game) $0.00

Subtotal: $0.00
Tax: $0.00
Total: $0.00
"""


def test_parse_template_a():
    p = parse_psn_receipt(TEMPLATE_A, message_id="m1")
    assert p is not None
    assert p.order_number == "264460326864"
    assert p.purchased_at == "05/08/2021"
    assert len(p.items) == 1
    assert p.items[0].title == "Wreckfest PlayStation®5 Version (Game)"
    assert p.items[0].price == "$0.00"


def test_parse_template_b():
    p = parse_psn_receipt(TEMPLATE_B, message_id="m2")
    assert p is not None
    assert p.order_number == "384720390939"
    assert p.purchased_at == "12/14/2022"
    assert len(p.items) == 1
    assert p.items[0].title == "The Witcher 3: Wild Hunt – Complete Edition (Game)"
    assert p.items[0].price == "$0.00"


def test_unrecognized_template_returns_none():
    assert parse_psn_receipt("some random email") is None


def test_normalize_title():
    assert normalize_title("Wreckfest PlayStation®5 Version (Game)") == "wreckfest version"
    assert normalize_title("The Witcher 3: Wild Hunt – Complete Edition (Game)") == (
        "the witcher 3: wild hunt – complete edition"
    )
