from mailroom.verticals.game_catalog.parsers.psn import (
    normalize_title,
    parse_psn_receipt,
)

# Post-2022 sender (reply@txn-email.playstation.com): same Template B layout but
# the "™" is dropped ("Your PlayStation Store transaction was successful"), and
# items may be "(Downloadable Game)" as well as "(Game)"; "(Add-On)" stays out.
TEMPLATE_B_NEW_SENDER = """
Thank You For Your Purchase

Your PlayStation Store transaction was successful. Thanks!

Order Number: 787094772850142

Name: Nick Brett
Online ID: nbrett3
Date Purchased: 08/12/2026

Details 
 Price

 Burly Men at Sea Maestro Beard Edition (Game)
 $2.99

 Armello™ (Game)
 $7.49

 Atari 50: THE NAMCO LEGENDARY PACK (Add-On)
 $5.19

 Nidhogg (Downloadable Game)
 $4.49

Subtotal: 
$40.97
 
Tax:  $3.63

Total: $44.60
"""

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
    assert p.source == "psn_receipt"
    assert p.order_number == "264460326864"
    assert p.purchased_at == "05/08/2021"
    assert len(p.items) == 1
    assert p.items[0].title == "Wreckfest PlayStation®5 Version (Game)"
    assert p.items[0].price == "$0.00"


def test_parse_template_b():
    p = parse_psn_receipt(TEMPLATE_B, message_id="m2")
    assert p is not None
    assert p.source == "psn_receipt"
    assert p.order_number == "384720390939"
    assert p.purchased_at == "12/14/2022"
    assert len(p.items) == 1
    assert p.items[0].title == "The Witcher 3: Wild Hunt – Complete Edition (Game)"
    assert p.items[0].price == "$0.00"


def test_parse_template_b_new_sender_no_trademark():
    p = parse_psn_receipt(TEMPLATE_B_NEW_SENDER, message_id="m3")
    assert p is not None
    assert p.order_number == "787094772850142"
    assert p.purchased_at == "08/12/2026"
    # (Game) and (Downloadable Game) included; (Add-On) excluded.
    assert [i.title for i in p.items] == [
        "Burly Men at Sea Maestro Beard Edition (Game)",
        "Armello™ (Game)",
        "Nidhogg (Downloadable Game)",
    ]
    assert p.subtotal == "40.97"
    assert p.tax == "3.63"
    assert p.total == "44.60"


def test_unrecognized_template_returns_none():
    assert parse_psn_receipt("some random email") is None


def test_normalize_title():
    assert normalize_title("Wreckfest PlayStation®5 Version (Game)") == "wreckfest version"
    assert normalize_title("The Witcher 3: Wild Hunt – Complete Edition (Game)") == (
        "the witcher 3: wild hunt – complete edition"
    )
